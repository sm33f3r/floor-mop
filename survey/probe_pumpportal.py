"""One-shot, keyless probe of PumpPortal's free data WebSocket.

Throwaway Phase 1 measurement script. Never imported by src/. Makes exactly
one WebSocket connection attempt (no retries, no reconnects) and treats all
token-controlled event fields as hostile, untrusted text: their VALUES are
never printed or logged, with the narrow allow-listed exception defined by
``UNTRUSTED_KEYS`` and the enum-detection rule in ``_collect_allow_listed``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

DEFAULT_URL = "wss://pumpportal.fun/api/data"
DEFAULT_DURATION = 120
HARD_MAX_DURATION = 600
DEFAULT_MAX_EVENTS = 300
DEFAULT_OUT_DIR = "data/survey"
OPEN_TIMEOUT_S = 15.0
SUBSCRIBE_GAP_S = 0.2
CAPTURE_FLUSH_EVERY = 20
CONTROL_FRAME_TRUNCATE = 200

UNTRUSTED_KEYS = {
    "name",
    "symbol",
    "uri",
    "description",
    "image",
    "twitter",
    "telegram",
    "website",
    "metadata",
    "links",
}

_ALLOW_VALUE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_ALLOW_MAX_LEN = 24
_ALLOW_MAX_DISTINCT = 12

_SIGNATURE_RE = re.compile("signature", re.IGNORECASE)
_TIME_RE = re.compile("time|ts|timestamp", re.IGNORECASE)
_QUOTE_RE = re.compile("quote", re.IGNORECASE)
_PLATFORM_RE = re.compile(
    "pool|platform|program|source|launchpad|dex", re.IGNORECASE
)


def _json_type_name(value: object) -> str:
    """Map a parsed JSON value to one of str/int/float/bool/null/list/dict."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


@dataclass
class ShapeInfo:
    """Aggregate stats for one event "shape" (sorted top-level key set)."""

    count: int = 0
    first_wall_ns: int = 0
    last_wall_ns: int = 0
    key_types: dict[str, set[str]] = field(default_factory=dict)
    nested: dict[str, dict[str, set[str]]] = field(default_factory=dict)

    def observe(self, wall_ns: int, parsed: dict[str, Any]) -> None:
        """Fold one parsed event of this shape into the aggregate."""
        if self.count == 0:
            self.first_wall_ns = wall_ns
        self.last_wall_ns = wall_ns
        self.count += 1
        for key, value in parsed.items():
            self.key_types.setdefault(key, set()).add(_json_type_name(value))
            if isinstance(value, dict):
                nested_map = self.nested.setdefault(key, {})
                for nested_key, nested_value in value.items():
                    nested_map.setdefault(nested_key, set()).add(
                        _json_type_name(nested_value)
                    )


@dataclass
class ControlInfo:
    """Aggregate stats for one control-frame shape."""

    count: int = 0
    example_truncated: str = ""


@dataclass
class ConnectionResult:
    """Outcome of the single connection attempt."""

    outcome: str = "failed"
    connect_ms: float | None = None
    exception_class: str | None = None
    http_status: int | None = None
    close_code: int | None = None
    close_reason: str | None = None


@dataclass
class ProbeState:
    """All mutable accumulators for the probe run, for interrupt-safety."""

    connection: ConnectionResult = field(default_factory=ConnectionResult)
    non_json: int = 0
    event_shapes: dict[str, ShapeInfo] = field(default_factory=dict)
    control_shapes: dict[str, ControlInfo] = field(default_factory=dict)
    allow_candidates: dict[str, dict[str, int] | None] = field(default_factory=dict)
    event_timestamps: list[int] = field(default_factory=list)
    all_top_keys: dict[str, set[str]] = field(default_factory=dict)
    events_received: int = 0
    capture_lines_written: int = 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse and validate CLI arguments."""

    def duration_type(raw: str) -> int:
        value = int(raw)
        if value <= 0 or value > HARD_MAX_DURATION:
            raise argparse.ArgumentTypeError(
                f"--duration must be in (0, {HARD_MAX_DURATION}]"
            )
        return value

    def label_type(raw: str) -> str:
        if not re.fullmatch(r"[a-z0-9_-]{1,30}", raw):
            raise argparse.ArgumentTypeError(
                "--label must match [a-z0-9_-]{1,30}"
            )
        return raw

    parser = argparse.ArgumentParser(description="Keyless PumpPortal WS probe.")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--duration", type=duration_type, default=DEFAULT_DURATION)
    parser.add_argument("--max-events", type=int, default=DEFAULT_MAX_EVENTS)
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--label", type=label_type, default=None)
    return parser.parse_args(argv)


def _resolve_out_dir(repo_root: Path, raw_out_dir: str) -> Path:
    """Resolve --out-dir and enforce it stays under <repo root>/data/."""
    candidate = Path(raw_out_dir)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    resolved = candidate.resolve()
    data_root = (repo_root / "data").resolve()
    if resolved != data_root and data_root not in resolved.parents:
        raise ValueError(f"--out-dir must resolve under {data_root}, got {resolved}")
    return resolved


def _utc_stamp(now: datetime) -> str:
    """Format a UTC timestamp as yyyymmddTHHMMSSZ."""
    return now.strftime("%Y%m%dT%H%M%SZ")


class CaptureWriter:
    """Appends one JSON line per received message, flushing periodically."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh = path.open("a", encoding="utf-8")
        self._since_flush = 0

    def write(self, wall_ns: int, mono_ns: int, raw: str) -> None:
        """Append one capture line for a received message."""
        line = json.dumps({"wall_ns": wall_ns, "mono_ns": mono_ns, "raw": raw})
        self._fh.write(line + "\n")
        self._since_flush += 1
        if self._since_flush >= CAPTURE_FLUSH_EVERY:
            self._fh.flush()
            self._since_flush = 0

    def close(self) -> None:
        """Flush and close the capture file."""
        self._fh.flush()
        self._fh.close()


def _record_event(state: ProbeState, wall_ns: int, parsed: dict[str, Any]) -> None:
    """Fold a non-control event into shape, timing, and allow-list state."""
    state.events_received += 1
    state.event_timestamps.append(wall_ns)

    shape = ",".join(sorted(parsed.keys()))
    shape_info = state.event_shapes.setdefault(shape, ShapeInfo())
    shape_info.observe(wall_ns, parsed)

    for key, value in parsed.items():
        state.all_top_keys.setdefault(key, set()).add(_json_type_name(value))
        if key in UNTRUSTED_KEYS:
            continue
        bucket = state.allow_candidates.get(key, {})
        if bucket is None:
            continue
        ok = (
            isinstance(value, str)
            and len(value) <= _ALLOW_MAX_LEN
            and bool(_ALLOW_VALUE_RE.match(value))
        )
        if not ok:
            state.allow_candidates[key] = None
            continue
        bucket[value] = bucket.get(value, 0) + 1
        if len(bucket) > _ALLOW_MAX_DISTINCT:
            state.allow_candidates[key] = None
        else:
            state.allow_candidates[key] = bucket


def _record_control(state: ProbeState, parsed: dict[str, Any], raw: str) -> None:
    """Fold a control frame into control-shape state and print it truncated."""
    shape = ",".join(sorted(parsed.keys()))
    info = state.control_shapes.setdefault(shape, ControlInfo())
    if info.count == 0:
        info.example_truncated = raw[:CONTROL_FRAME_TRUNCATE]
    info.count += 1
    print(f"control-frame shape={shape} text={raw[:CONTROL_FRAME_TRUNCATE]!r}")


async def _receive_loop(
    ws: Any, state: ProbeState, capture: CaptureWriter, duration: float, max_events: int
) -> None:
    """Receive messages until duration elapses, max_events is hit, or closed."""
    deadline = time.perf_counter() + duration
    non_control_count = 0
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0 or non_control_count >= max_events:
            return
        try:
            message = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except TimeoutError:
            return
        except ConnectionClosed as exc:
            if exc.rcvd is not None:
                state.connection.close_code = exc.rcvd.code
                state.connection.close_reason = (exc.rcvd.reason or "")[
                    :CONTROL_FRAME_TRUNCATE
                ]
            return

        wall_ns = time.time_ns()
        mono_ns = time.perf_counter_ns()
        raw_text = message if isinstance(message, str) else message.decode(
            "utf-8", errors="replace"
        )
        capture.write(wall_ns, mono_ns, raw_text)
        state.capture_lines_written += 1

        try:
            parsed = json.loads(raw_text)
        except ValueError:
            state.non_json += 1
            non_control_count += 1
            continue
        if not isinstance(parsed, dict):
            state.non_json += 1
            non_control_count += 1
            continue
        if "mint" not in parsed:
            _record_control(state, parsed, raw_text)
            continue

        non_control_count += 1
        _record_event(state, wall_ns, parsed)


async def _run_probe(
    url: str, duration: float, max_events: int, capture: CaptureWriter, state: ProbeState
) -> None:
    """Make the single connection attempt and, on success, receive events."""
    connect_start = time.perf_counter()
    try:
        async with connect(url, open_timeout=OPEN_TIMEOUT_S) as ws:
            state.connection.connect_ms = (time.perf_counter() - connect_start) * 1000
            state.connection.outcome = "connected"

            await ws.send(json.dumps({"method": "subscribeNewToken"}))
            await asyncio.sleep(SUBSCRIBE_GAP_S)
            await ws.send(json.dumps({"method": "subscribeMigration"}))

            await _receive_loop(ws, state, capture, duration, max_events)
    except InvalidStatus as exc:
        state.connection.outcome = "failed"
        state.connection.exception_class = type(exc).__name__
        state.connection.http_status = exc.response.status_code
    except Exception as exc:  # noqa: BLE001 - must record and exit, not crash
        state.connection.outcome = "failed"
        state.connection.exception_class = type(exc).__name__
        rcvd = getattr(exc, "rcvd", None)
        if rcvd is not None:
            state.connection.close_code = rcvd.code
            state.connection.close_reason = (rcvd.reason or "")[
                :CONTROL_FRAME_TRUNCATE
            ]


def _finalize_allow_listed(state: ProbeState) -> dict[str, dict[str, int]]:
    """Return the allow-listed enum-like key/value/count table."""
    return {
        key: dict(sorted(bucket.items()))
        for key, bucket in state.allow_candidates.items()
        if bucket is not None
    }


def _finalize_findings(state: ProbeState) -> dict[str, Any]:
    """Build the key-name-only findings report."""
    signature_keys = sorted(k for k in state.all_top_keys if _SIGNATURE_RE.search(k))
    mint_present = any(k.lower() == "mint" for k in state.all_top_keys)
    time_like = {
        k: sorted(state.all_top_keys[k])
        for k in state.all_top_keys
        if _TIME_RE.search(k)
    }
    quote_keys = sorted(k for k in state.all_top_keys if _QUOTE_RE.search(k))
    platform_like = {
        k: sorted(state.all_top_keys[k])
        for k in state.all_top_keys
        if _PLATFORM_RE.search(k)
    }
    return {
        "signature_keys": signature_keys,
        "mint_present": mint_present,
        "time_like_keys": time_like,
        "quote_keys": quote_keys,
        "platform_like_keys": platform_like,
    }


def _finalize_timing(state: ProbeState) -> dict[str, Any]:
    """Compute inter-arrival timing stats and 10-second bucket counts."""
    timestamps = state.event_timestamps
    if len(timestamps) < 2:
        return {
            "min_ms": None,
            "median_ms": None,
            "p95_ms": None,
            "max_ms": None,
            "rate_eps": float(len(timestamps)),
            "buckets_10s": [len(timestamps)] if timestamps else [],
        }
    deltas_ms = [
        (timestamps[i] - timestamps[i - 1]) / 1_000_000 for i in range(1, len(timestamps))
    ]
    deltas_sorted = sorted(deltas_ms)
    p95_index = min(len(deltas_sorted) - 1, round(0.95 * (len(deltas_sorted) - 1)))
    span_s = (timestamps[-1] - timestamps[0]) / 1_000_000_000
    rate_eps = len(timestamps) / span_s if span_s > 0 else float(len(timestamps))

    buckets: list[int] = []
    for ts in timestamps:
        idx = int((ts - timestamps[0]) / 1_000_000_000 // 10)
        while len(buckets) <= idx:
            buckets.append(0)
        buckets[idx] += 1

    return {
        "min_ms": deltas_sorted[0],
        "median_ms": statistics.median(deltas_ms),
        "p95_ms": deltas_sorted[p95_index],
        "max_ms": deltas_sorted[-1],
        "rate_eps": rate_eps,
        "buckets_10s": buckets,
    }


def _shape_summary(shapes: dict[str, ShapeInfo]) -> dict[str, Any]:
    """Render event shape aggregates into JSON-serializable form."""
    return {
        shape: {
            "count": info.count,
            "first_wall_ns": info.first_wall_ns,
            "last_wall_ns": info.last_wall_ns,
            "key_types": {k: sorted(v) for k, v in info.key_types.items()},
            "nested": {
                k: {nk: sorted(nv) for nk, nv in nested.items()}
                for k, nested in info.nested.items()
            },
        }
        for shape, info in shapes.items()
    }


def _control_summary(control: dict[str, ControlInfo]) -> dict[str, Any]:
    """Render control-frame aggregates into JSON-serializable form."""
    return {
        shape: {"count": info.count, "example_truncated": info.example_truncated}
        for shape, info in control.items()
    }


def _verdict(state: ProbeState) -> str:
    """Classify the run outcome per the keyless_ok contract."""
    if state.connection.outcome != "connected":
        return "false"
    if state.events_received == 0:
        return "unknown_no_events"
    return "true"


def _build_summary(
    state: ProbeState,
    started_utc: str,
    hostname: str,
    duration: int,
    max_events: int,
) -> dict[str, Any]:
    """Build the full summary dict written to the summary JSON file."""
    conn = state.connection
    return {
        "started_utc": started_utc,
        "url_hostname": hostname,
        "duration_s": duration,
        "max_events": max_events,
        "connection": {
            "outcome": conn.outcome,
            "connect_ms": conn.connect_ms,
            "exception_class": conn.exception_class,
            "http_status": conn.http_status,
            "close_code": conn.close_code,
            "close_reason": conn.close_reason,
        },
        "non_json": state.non_json,
        "control_frames": _control_summary(state.control_shapes),
        "event_shapes": _shape_summary(state.event_shapes),
        "allow_listed_values": _finalize_allow_listed(state),
        "findings": _finalize_findings(state),
        "timing": _finalize_timing(state),
        "events_received": state.events_received,
        "capture_lines_written": state.capture_lines_written,
        "verdict_keyless_ok": _verdict(state),
    }


def _stdout_report(summary: dict[str, Any]) -> dict[str, Any]:
    """Select the subset of the summary allowed on stdout."""
    return {
        "connection_outcome": summary["connection"]["outcome"],
        "event_shape_counts": {
            shape: info["count"] for shape, info in summary["event_shapes"].items()
        },
        "key_types": {
            shape: info["key_types"] for shape, info in summary["event_shapes"].items()
        },
        "allow_listed_values": summary["allow_listed_values"],
        "findings": summary["findings"],
        "timing": summary["timing"],
        "control_frames": summary["control_frames"],
    }


def main(argv: list[str] | None = None) -> int:
    """Entry point: run the single-attempt probe and write/print results."""
    args = _parse_args(argv)
    repo_root = Path(__file__).resolve().parent.parent

    try:
        out_dir = _resolve_out_dir(repo_root, args.out_dir)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)

    hostname = urlparse(args.url).hostname or ""
    now = datetime.now(UTC)
    stamp = _utc_stamp(now)
    stem = f"pumpportal_{stamp}" + (f"_{args.label}" if args.label else "")
    capture_path = out_dir / f"{stem}.raw.jsonl"
    summary_path = out_dir / f"{stem}.summary.json"

    print(
        f"{now.isoformat()} start host={hostname} "
        f"duration={args.duration} max_events={args.max_events}"
    )

    state = ProbeState()
    capture = CaptureWriter(capture_path)
    exit_code = 0
    try:
        try:
            asyncio.run(
                _run_probe(args.url, float(args.duration), args.max_events, capture, state)
            )
        except KeyboardInterrupt:
            exit_code = 130
    finally:
        capture.close()

    summary = _build_summary(state, now.isoformat(), hostname, args.duration, args.max_events)
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(json.dumps(_stdout_report(summary), indent=2))

    verdict = summary["verdict_keyless_ok"]
    print(f"VERDICT keyless_ok={verdict} events={state.events_received}")

    if exit_code == 130:
        return 130
    return 0 if state.connection.outcome == "connected" else 2


if __name__ == "__main__":
    sys.exit(main())
