"""One-shot, anonymous probe of PumpDev's data WebSocket.

Throwaway Phase 1 measurement script. Never imported by src/. Mirrors the
structure and conventions of survey/probe_pumpportal.py so both probes'
outputs are comparable. Makes exactly one WebSocket connection attempt (no
retries, no reconnects) and treats all token-controlled event fields as
hostile, untrusted text: their VALUES are never printed or logged except the
narrow allow-listed categories defined below (per-shape enum values,
per-shape booleans, and conforming quote-mint addresses).
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
from typing import Any, Literal
from urllib.parse import urlparse

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

DEFAULT_URL = "wss://pumpdev.io/ws"
DEFAULT_DURATION = 90
HARD_MAX_DURATION = 600
DEFAULT_MAX_EVENTS = 300
DEFAULT_TRADE_SAMPLE = 0
HARD_MAX_TRADE_SAMPLE = 4
DEFAULT_TRADE_DURATION = 60
HARD_MAX_TRADE_DURATION = 180
DEFAULT_TRADE_MAX_MESSAGES = 150
HARD_MAX_TRADE_MAX_MESSAGES = 1000
DEFAULT_OUT_DIR = "data/survey"
OPEN_TIMEOUT_S = 15.0
CAPTURE_FLUSH_EVERY = 20
CONTROL_FRAME_TRUNCATE = 300

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

QUOTE_MINT_KEYS = {"quoteMint", "pairQuoteMint", "baseMint"}

_ALLOW_VALUE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_ALLOW_MAX_LEN = 24
_ALLOW_MAX_DISTINCT = 12

_BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_QUOTE_MINT_MAX_DISTINCT = 8

_SIGNATURE_RE = re.compile("signature", re.IGNORECASE)
_TIME_RE = re.compile("time|ts|timestamp", re.IGNORECASE)
_QUOTE_RE = re.compile("quote", re.IGNORECASE)
_PLATFORM_RE = re.compile("pool|platform|program|source|launchpad|dex", re.IGNORECASE)

Phase = Literal["A", "B"]


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

    shape_id: str
    count: int = 0
    count_by_phase: dict[str, int] = field(default_factory=dict)
    first_wall_ns: int = 0
    last_wall_ns: int = 0
    key_types: dict[str, set[str]] = field(default_factory=dict)
    nested: dict[str, dict[str, set[str]]] = field(default_factory=dict)
    null_counts: dict[str, int] = field(default_factory=dict)
    allow_candidates: dict[str, dict[str, int] | None] = field(default_factory=dict)
    bool_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    bool_disqualified: set[str] = field(default_factory=set)

    def observe(self, phase: Phase, wall_ns: int, parsed: dict[str, Any]) -> None:
        """Fold one parsed event of this shape into the aggregate."""
        if self.count == 0:
            self.first_wall_ns = wall_ns
        self.last_wall_ns = wall_ns
        self.count += 1
        self.count_by_phase[phase] = self.count_by_phase.get(phase, 0) + 1

        for key, value in parsed.items():
            type_name = _json_type_name(value)
            self.key_types.setdefault(key, set()).add(type_name)
            if value is None:
                self.null_counts[key] = self.null_counts.get(key, 0) + 1
            if isinstance(value, dict):
                nested_map = self.nested.setdefault(key, {})
                for nested_key, nested_value in value.items():
                    nested_map.setdefault(nested_key, set()).add(
                        _json_type_name(nested_value)
                    )

            if isinstance(value, bool):
                if key not in self.bool_disqualified:
                    counts = self.bool_counts.setdefault(key, {"true": 0, "false": 0})
                    counts["true" if value else "false"] += 1
            elif key in self.bool_counts or key not in self.bool_disqualified:
                self.bool_disqualified.add(key)
                self.bool_counts.pop(key, None)

            if key in UNTRUSTED_KEYS:
                continue
            bucket = self.allow_candidates.get(key, {})
            if bucket is None:
                continue
            ok = (
                isinstance(value, str)
                and len(value) <= _ALLOW_MAX_LEN
                and bool(_ALLOW_VALUE_RE.match(value))
            )
            if not ok:
                self.allow_candidates[key] = None
                continue
            bucket[value] = bucket.get(value, 0) + 1
            if len(bucket) > _ALLOW_MAX_DISTINCT:
                self.allow_candidates[key] = None
            else:
                self.allow_candidates[key] = bucket


@dataclass
class ControlInfo:
    """Aggregate stats for one control-frame type value."""

    count: int = 0
    example_truncated: str = ""


@dataclass
class QuoteMintInfo:
    """Aggregate stats for one quote-mint-like key."""

    values: dict[str, int] = field(default_factory=dict)
    nonconforming: int = 0


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
    control_frames: dict[str, ControlInfo] = field(default_factory=dict)
    quote_mints: dict[str, QuoteMintInfo] = field(default_factory=dict)
    all_top_keys: dict[str, set[str]] = field(default_factory=dict)
    bool_values_seen: dict[str, set[str]] = field(default_factory=dict)

    create_timestamps_a: list[int] = field(default_factory=list)
    recent_create_mints: list[str] = field(default_factory=list)

    create_events: int = 0
    trade_buy_count: int = 0
    trade_sell_count: int = 0
    trade_timestamps: list[int] = field(default_factory=list)
    trade_messages_received: int = 0

    subscribe_sent_at: float | None = None
    subscribe_ack_latency_ms: float | None = None

    capture_lines_written: int = 0
    messages_sent: int = 0
    phase_b_sampled_mints: list[str] = field(default_factory=list)
    phase_b_skipped_no_valid_mints: bool = False
    phase_b_ran: bool = False
    unsubscribe_sent: bool = False


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse and validate CLI arguments."""

    def bounded_int(name: str, hard_max: int) -> Any:
        def _inner(raw: str) -> int:
            value = int(raw)
            if value < 0 or value > hard_max:
                raise argparse.ArgumentTypeError(f"--{name} must be in [0, {hard_max}]")
            return value

        return _inner

    def duration_type(raw: str) -> int:
        value = int(raw)
        if value <= 0 or value > HARD_MAX_DURATION:
            raise argparse.ArgumentTypeError(f"--duration must be in (0, {HARD_MAX_DURATION}]")
        return value

    def trade_duration_type(raw: str) -> int:
        value = int(raw)
        if value <= 0 or value > HARD_MAX_TRADE_DURATION:
            raise argparse.ArgumentTypeError(
                f"--trade-duration must be in (0, {HARD_MAX_TRADE_DURATION}]"
            )
        return value

    def label_type(raw: str) -> str:
        if not re.fullmatch(r"[a-z0-9_-]{1,30}", raw):
            raise argparse.ArgumentTypeError("--label must match [a-z0-9_-]{1,30}")
        return raw

    parser = argparse.ArgumentParser(description="Anonymous PumpDev WS probe.")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--duration", type=duration_type, default=DEFAULT_DURATION)
    parser.add_argument("--max-events", type=int, default=DEFAULT_MAX_EVENTS)
    parser.add_argument(
        "--trade-sample",
        type=bounded_int("trade-sample", HARD_MAX_TRADE_SAMPLE),
        default=DEFAULT_TRADE_SAMPLE,
    )
    parser.add_argument("--trade-duration", type=trade_duration_type, default=DEFAULT_TRADE_DURATION)
    parser.add_argument(
        "--trade-max-messages",
        type=bounded_int("trade-max-messages", HARD_MAX_TRADE_MAX_MESSAGES),
        default=DEFAULT_TRADE_MAX_MESSAGES,
    )
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
        self._fh = path.open("a", encoding="utf-8")
        self._since_flush = 0

    def write(self, phase: Phase, wall_ns: int, mono_ns: int, raw: str) -> None:
        """Append one capture line for a received message."""
        line = json.dumps(
            {"phase": phase, "wall_ns": wall_ns, "mono_ns": mono_ns, "raw": raw}
        )
        self._fh.write(line + "\n")
        self._since_flush += 1
        if self._since_flush >= CAPTURE_FLUSH_EVERY:
            self._fh.flush()
            self._since_flush = 0

    def close(self) -> None:
        """Flush and close the capture file."""
        self._fh.flush()
        self._fh.close()


def _shape_key(parsed: dict[str, Any]) -> str:
    """Compute the sorted-key-name shape string for a parsed event."""
    return ",".join(sorted(parsed.keys()))


def _get_or_assign_shape(state: ProbeState, shape: str) -> ShapeInfo:
    """Return the ShapeInfo for shape, assigning the next S<n> id if new."""
    info = state.event_shapes.get(shape)
    if info is None:
        shape_id = f"S{len(state.event_shapes) + 1}"
        info = ShapeInfo(shape_id=shape_id)
        state.event_shapes[shape] = info
    return info


def _record_quote_mints(state: ProbeState, parsed: dict[str, Any]) -> None:
    """Fold any quoteMint/pairQuoteMint/baseMint value into global tracking."""
    for key in QUOTE_MINT_KEYS:
        if key not in parsed:
            continue
        value = parsed[key]
        info = state.quote_mints.setdefault(key, QuoteMintInfo())
        if isinstance(value, str) and _BASE58_RE.match(value):
            info.values[value] = info.values.get(value, 0) + 1
        else:
            info.nonconforming += 1


def _record_event(state: ProbeState, phase: Phase, wall_ns: int, parsed: dict[str, Any]) -> None:
    """Fold a non-control event into shape, timing, and allow-list state."""
    shape = _shape_key(parsed)
    info = _get_or_assign_shape(state, shape)
    info.observe(phase, wall_ns, parsed)

    for key, value in parsed.items():
        state.all_top_keys.setdefault(key, set()).add(_json_type_name(value))
        if isinstance(value, bool):
            state.bool_values_seen.setdefault(key, set()).add("true" if value else "false")

    _record_quote_mints(state, parsed)

    tx_type = parsed.get("txType")
    if phase == "A" and tx_type == "create":
        state.create_events += 1
        state.create_timestamps_a.append(wall_ns)
        mint = parsed.get("mint")
        if isinstance(mint, str):
            state.recent_create_mints.append(mint)
    elif phase == "B" and tx_type in ("buy", "sell"):
        state.trade_messages_received += 1
        state.trade_timestamps.append(wall_ns)
        if tx_type == "buy":
            state.trade_buy_count += 1
        else:
            state.trade_sell_count += 1


def _record_control(state: ProbeState, parsed: dict[str, Any], raw: str) -> None:
    """Fold a control frame into control-type state and print it truncated."""
    type_value = parsed.get("type")
    key = type_value if isinstance(type_value, str) else "<non-str-type>"
    info = state.control_frames.setdefault(key, ControlInfo())
    if info.count == 0:
        info.example_truncated = raw[:CONTROL_FRAME_TRUNCATE]
    info.count += 1
    print(f"control-frame type={key!r} text={raw[:CONTROL_FRAME_TRUNCATE]!r}")

    if (
        key == "subscribed"
        and state.subscribe_sent_at is not None
        and state.subscribe_ack_latency_ms is None
    ):
        state.subscribe_ack_latency_ms = (time.perf_counter() - state.subscribe_sent_at) * 1000


async def _receive_message(ws: Any, timeout: float) -> tuple[str, int, int] | None:
    """Receive one message with a timeout, returning (text, wall_ns, mono_ns) or None."""
    message = await asyncio.wait_for(ws.recv(), timeout=timeout)
    wall_ns = time.time_ns()
    mono_ns = time.perf_counter_ns()
    raw_text = message if isinstance(message, str) else message.decode("utf-8", errors="replace")
    return raw_text, wall_ns, mono_ns


def _classify_and_record(
    state: ProbeState, phase: Phase, wall_ns: int, raw_text: str
) -> dict[str, Any] | None:
    """Parse one message, fold it into state, and return the parsed dict if any."""
    try:
        parsed = json.loads(raw_text)
    except ValueError:
        state.non_json += 1
        return None
    if not isinstance(parsed, dict):
        state.non_json += 1
        return None
    if "type" in parsed:
        _record_control(state, parsed, raw_text)
        return parsed
    _record_event(state, phase, wall_ns, parsed)
    return parsed


async def _phase_a(
    ws: Any, state: ProbeState, capture: CaptureWriter, duration: float, max_events: int
) -> None:
    """Send subscribeNewToken and receive until duration/max_events/close."""
    state.subscribe_sent_at = time.perf_counter()
    await ws.send(json.dumps({"method": "subscribeNewToken"}))
    state.messages_sent += 1

    deadline = time.perf_counter() + duration
    non_control_count = 0
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0 or non_control_count >= max_events:
            return
        try:
            received = await _receive_message(ws, remaining)
        except TimeoutError:
            return
        except ConnectionClosed as exc:
            if exc.rcvd is not None:
                state.connection.close_code = exc.rcvd.code
                state.connection.close_reason = (exc.rcvd.reason or "")[:CONTROL_FRAME_TRUNCATE]
            return

        raw_text, wall_ns, mono_ns = received
        capture.write("A", wall_ns, mono_ns, raw_text)
        state.capture_lines_written += 1

        parsed = _classify_and_record(state, "A", wall_ns, raw_text)
        if parsed is None or "type" not in parsed:
            non_control_count += 1


def _select_trade_mints(state: ProbeState, trade_sample: int) -> list[str]:
    """Pick up to trade_sample distinct, base58-eligible mints, most recent first."""
    selected: list[str] = []
    seen: set[str] = set()
    for mint in reversed(state.recent_create_mints):
        if mint in seen:
            continue
        if not _BASE58_RE.match(mint):
            continue
        seen.add(mint)
        selected.append(mint)
        if len(selected) >= trade_sample:
            break
    return selected


async def _phase_b(
    ws: Any,
    state: ProbeState,
    capture: CaptureWriter,
    trade_sample: int,
    trade_duration: float,
    trade_max_messages: int,
) -> None:
    """Subscribe to a bounded mint sample, receive trades, then unsubscribe."""
    mints = _select_trade_mints(state, trade_sample)
    if not mints:
        state.phase_b_skipped_no_valid_mints = True
        return

    state.phase_b_ran = True
    state.phase_b_sampled_mints = mints

    await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": mints}))
    state.messages_sent += 1

    deadline = time.perf_counter() + trade_duration
    closed = False
    while state.trade_messages_received < trade_max_messages:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            break
        try:
            received = await _receive_message(ws, remaining)
        except TimeoutError:
            break
        except ConnectionClosed as exc:
            if exc.rcvd is not None:
                state.connection.close_code = exc.rcvd.code
                state.connection.close_reason = (exc.rcvd.reason or "")[:CONTROL_FRAME_TRUNCATE]
            closed = True
            break

        raw_text, wall_ns, mono_ns = received
        capture.write("B", wall_ns, mono_ns, raw_text)
        state.capture_lines_written += 1
        _classify_and_record(state, "B", wall_ns, raw_text)

    if closed:
        return

    try:
        await ws.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": mints}))
        state.messages_sent += 1
        state.unsubscribe_sent = True
        await ws.close()
    except ConnectionClosed:
        pass


async def _run_probe(
    url: str,
    duration: float,
    max_events: int,
    trade_sample: int,
    trade_duration: float,
    trade_max_messages: int,
    capture: CaptureWriter,
    state: ProbeState,
) -> None:
    """Make the single connection attempt and run phase A, then phase B."""
    connect_start = time.perf_counter()
    try:
        async with connect(url, open_timeout=OPEN_TIMEOUT_S) as ws:
            state.connection.connect_ms = (time.perf_counter() - connect_start) * 1000
            state.connection.outcome = "connected"

            await _phase_a(ws, state, capture, duration, max_events)

            if trade_sample > 0:
                await _phase_b(
                    ws, state, capture, trade_sample, trade_duration, trade_max_messages
                )
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
            state.connection.close_reason = (rcvd.reason or "")[:CONTROL_FRAME_TRUNCATE]


def _finalize_findings(state: ProbeState) -> dict[str, Any]:
    """Build the key-name-only findings report."""
    signature_keys = sorted(k for k in state.all_top_keys if _SIGNATURE_RE.search(k))
    mint_present = any(k.lower() == "mint" for k in state.all_top_keys)
    time_like = {k: sorted(state.all_top_keys[k]) for k in state.all_top_keys if _TIME_RE.search(k)}
    quote_keys = sorted(k for k in state.all_top_keys if _QUOTE_RE.search(k))
    platform_like = {
        k: sorted(state.all_top_keys[k]) for k in state.all_top_keys if _PLATFORM_RE.search(k)
    }
    qcr_present = "quoteContextResolved" in state.all_top_keys
    qcr_values = sorted(state.bool_values_seen.get("quoteContextResolved", set()))
    return {
        "signature_keys": signature_keys,
        "mint_present": mint_present,
        "time_like_keys": time_like,
        "quote_keys": quote_keys,
        "platform_like_keys": platform_like,
        "quote_context_resolved_present": qcr_present,
        "quote_context_resolved_values": qcr_values,
    }


def _timing_stats(timestamps: list[int]) -> dict[str, Any]:
    """Compute inter-arrival timing stats and 10-second bucket counts."""
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


def _finalize_timing(state: ProbeState) -> dict[str, Any]:
    """Build the phase A and phase B timing reports."""
    phase_a = _timing_stats(state.create_timestamps_a)
    phase_b = _timing_stats(state.trade_timestamps)
    phase_b["buy_count"] = state.trade_buy_count
    phase_b["sell_count"] = state.trade_sell_count
    phase_b["trade_messages_received"] = state.trade_messages_received
    return {"phase_a": phase_a, "phase_b": phase_b}


def _shape_summary(state: ProbeState) -> dict[str, Any]:
    """Render event shape aggregates into JSON-serializable form, keyed by id."""
    result: dict[str, Any] = {}
    for shape, info in state.event_shapes.items():
        result[info.shape_id] = {
            "keys": shape,
            "count": info.count,
            "count_by_phase": dict(info.count_by_phase),
            "first_wall_ns": info.first_wall_ns,
            "last_wall_ns": info.last_wall_ns,
            "key_types": {k: sorted(v) for k, v in info.key_types.items()},
            "nested": {
                k: {nk: sorted(nv) for nk, nv in nested.items()}
                for k, nested in info.nested.items()
            },
            "null_counts": dict(info.null_counts),
            "allow_listed_values": {
                k: dict(sorted(v.items())) for k, v in info.allow_candidates.items() if v
            },
            "bool_counts": {k: dict(v) for k, v in info.bool_counts.items()},
        }
    return result


def _quote_mint_summary(state: ProbeState) -> dict[str, Any]:
    """Render quote-mint tracking into JSON-serializable form."""
    result: dict[str, Any] = {}
    for key, info in state.quote_mints.items():
        if len(info.values) <= _QUOTE_MINT_MAX_DISTINCT:
            values_report: dict[str, int] | int = dict(sorted(info.values.items()))
        else:
            values_report = len(info.values)
        result[key] = {"values": values_report, "nonconforming": info.nonconforming}
    return result


def _control_summary(state: ProbeState) -> dict[str, Any]:
    """Render control-frame aggregates into JSON-serializable form."""
    return {
        type_value: {"count": info.count, "example_truncated": info.example_truncated}
        for type_value, info in state.control_frames.items()
    }


def _verdict(state: ProbeState) -> str:
    """Classify the run outcome per the anon_ok contract."""
    if state.connection.outcome != "connected":
        return "false"
    if state.create_events == 0:
        return "unknown_no_events"
    return "true"


def _build_summary(
    state: ProbeState,
    started_utc: str,
    hostname: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Build the full summary dict written to the summary JSON file."""
    conn = state.connection
    return {
        "started_utc": started_utc,
        "url_hostname": hostname,
        "duration_s": args.duration,
        "max_events": args.max_events,
        "trade_sample": args.trade_sample,
        "trade_duration_s": args.trade_duration,
        "trade_max_messages": args.trade_max_messages,
        "connection": {
            "outcome": conn.outcome,
            "connect_ms": conn.connect_ms,
            "exception_class": conn.exception_class,
            "http_status": conn.http_status,
            "close_code": conn.close_code,
            "close_reason": conn.close_reason,
        },
        "subscribe_ack_latency_ms": state.subscribe_ack_latency_ms,
        "non_json": state.non_json,
        "control_frames": _control_summary(state),
        "shape_key_map": {info.shape_id: shape for shape, info in state.event_shapes.items()},
        "event_shapes": _shape_summary(state),
        "quote_mints": _quote_mint_summary(state),
        "findings": _finalize_findings(state),
        "timing": _finalize_timing(state),
        "create_events": state.create_events,
        "trade_messages_received": state.trade_messages_received,
        "capture_lines_written": state.capture_lines_written,
        "messages_sent": state.messages_sent,
        "phase_b_ran": state.phase_b_ran,
        "phase_b_skipped_no_valid_mints": state.phase_b_skipped_no_valid_mints,
        "phase_b_sampled_mint_count": len(state.phase_b_sampled_mints),
        "unsubscribe_sent": state.unsubscribe_sent,
        "verdict_anon_ok": _verdict(state),
    }


def _stdout_report(summary: dict[str, Any]) -> dict[str, Any]:
    """Select the subset of the summary allowed on stdout."""
    return {
        "connection_outcome": summary["connection"]["outcome"],
        "subscribe_ack_latency_ms": summary["subscribe_ack_latency_ms"],
        "shape_key_map": summary["shape_key_map"],
        "event_shapes": {
            shape_id: {
                "count": info["count"],
                "count_by_phase": info["count_by_phase"],
                "key_types": info["key_types"],
                "null_counts": info["null_counts"],
                "allow_listed_values": info["allow_listed_values"],
                "bool_counts": info["bool_counts"],
            }
            for shape_id, info in summary["event_shapes"].items()
        },
        "quote_mints": summary["quote_mints"],
        "findings": summary["findings"],
        "timing": summary["timing"],
        "control_frames": summary["control_frames"],
        "phase_b_ran": summary["phase_b_ran"],
        "phase_b_skipped_no_valid_mints": summary["phase_b_skipped_no_valid_mints"],
        "phase_b_sampled_mint_count": summary["phase_b_sampled_mint_count"],
        "unsubscribe_sent": summary["unsubscribe_sent"],
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
    stem = f"pumpdev_{stamp}" + (f"_{args.label}" if args.label else "")
    capture_path = out_dir / f"{stem}.raw.jsonl"
    summary_path = out_dir / f"{stem}.summary.json"

    print(
        f"{now.isoformat()} start host={hostname} duration={args.duration} "
        f"max_events={args.max_events} trade_sample={args.trade_sample} "
        f"trade_duration={args.trade_duration} trade_max_messages={args.trade_max_messages}"
    )

    state = ProbeState()
    capture = CaptureWriter(capture_path)
    exit_code = 0
    try:
        try:
            asyncio.run(
                _run_probe(
                    args.url,
                    float(args.duration),
                    args.max_events,
                    args.trade_sample,
                    float(args.trade_duration),
                    args.trade_max_messages,
                    capture,
                    state,
                )
            )
        except KeyboardInterrupt:
            exit_code = 130
    finally:
        capture.close()

    summary = _build_summary(state, now.isoformat(), hostname, args)
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(json.dumps(_stdout_report(summary), indent=2))

    print(f"trade_messages_received={state.trade_messages_received}")
    verdict = summary["verdict_anon_ok"]
    print(
        f"VERDICT anon_ok={verdict} create_events={state.create_events} "
        f"trade_events={state.trade_messages_received}"
    )

    if exit_code == 130:
        return 130
    return 0 if state.connection.outcome == "connected" else 2


if __name__ == "__main__":
    sys.exit(main())
