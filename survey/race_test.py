"""Four-way race test of new-Solana-token launch feeds.

Throwaway Phase 1 measurement script. Never imported by src/. Measures, from
THIS host, how quickly four keyless feeds (PumpPortal and PumpDev websockets;
RugCheck and Raydium LaunchLab HTTP polls) report brand-new token launches
and Pump.fun-to-AMM / LetsBonk migrations, so an operator can decide which
relay to treat as primary and which as fallback for Floor Mop's own
deployment. Every number produced here is true only for the machine and time
window of this one run -- the report says so up front.

Security posture, mirrored from the other survey/probe_*.py scripts: token
name/symbol/uri/description text is hostile, untrusted input. Only mint
addresses, signatures, pool/txType-derived labels and numeric timestamps --
each validated against a strict pattern -- are ever written to the capture,
the summary, the digest or stdout/stderr. No credentials are used; HTTP
requests send a fixed, minimal header set and are never altered to evade
blocking.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

FEEDS = ("pp", "pd", "rc", "ray")
FeedCode = Literal["pp", "pd", "rc", "ray"]

DEFAULT_PP_URL = "wss://pumpportal.fun/api/data"
DEFAULT_PD_URL = "wss://pumpdev.io/ws"
DEFAULT_RC_URL = "https://api.rugcheck.xyz"
DEFAULT_RAY_URL = "https://launch-mint-v1.raydium.io"

RC_PATH = "/v1/stats/new_tokens"
RAY_PATH = "/get/list?sort=new"

ALLOWED_WS_HOSTS = {"pumpportal.fun": DEFAULT_PP_URL, "pumpdev.io": DEFAULT_PD_URL}
ALLOWED_HTTP_HOSTS = {
    "api.rugcheck.xyz": DEFAULT_RC_URL,
    "launch-mint-v1.raydium.io": DEFAULT_RAY_URL,
}

DEFAULT_DURATION = 2700
MIN_DURATION = 60
HARD_MAX_DURATION = 10800
DEFAULT_WARMUP = 10
DEFAULT_COOLDOWN = 10
DEFAULT_RC_INTERVAL = 4
MIN_RC_INTERVAL = 3
DEFAULT_RAY_INTERVAL = 10
MIN_RAY_INTERVAL = 5
DEFAULT_RECONNECT_WAIT = 60
MIN_RECONNECT_WAIT = 1
DEFAULT_OUT_DIR = "data/survey"
OPEN_TIMEOUT_S = 15.0
SUBSCRIBE_GAP_S = 0.2
REQUEST_TIMEOUT_S = 15.0
READ_LIMIT_BYTES = 4_000_000
HARD_REQUEST_CAP = 3000
CONSECUTIVE_FAILURE_LIMIT = 5
CLOCK_JUMP_THRESHOLD_S = 3.0
CLOCK_JUMP_SUSPECT_S = 60.0
CAPTURE_FLUSH_EVERY = 20
DIGEST_MAX_LINES = 200
USER_AGENT = "floor-mop-survey/0.1"

MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}\Z")
SIG_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{64,100}\Z")
POOL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,24}\Z")
LABEL_RE = re.compile(r"^[a-z0-9_-]{1,30}\Z")
ISO_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

Kind = Literal["create", "migrate", "listed"]


def valid_mint(value: Any) -> bool:
    """Whether value is a syntactically valid base58 Solana mint address."""
    return isinstance(value, str) and bool(MINT_RE.match(value))


def valid_sig(value: Any) -> bool:
    """Whether value is a syntactically valid base58 transaction signature."""
    return isinstance(value, str) and bool(SIG_RE.match(value))


def valid_pool(value: Any) -> bool:
    """Whether value is a short identifier-like pool/platform label."""
    return isinstance(value, str) and bool(POOL_RE.match(value))


def parse_create_at(value: Any) -> int | None:
    """Parse a createAt value (epoch ms int, numeric string, or ISO 8601) to epoch ms."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        if re.fullmatch(r"[0-9]{10,16}", value):
            return int(value)
        if len(value) <= 40 and ISO_PREFIX_RE.match(value):
            try:
                dt = datetime.fromisoformat(value.strip())
            except ValueError:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return int(dt.timestamp() * 1000)
    return None


def find_item_list(data: Any) -> list[dict[str, Any]]:
    """Largest list of dicts found breadth-first within the first 3 levels of data."""
    best: list[dict[str, Any]] = []
    queue: list[tuple[Any, int]] = [(data, 0)]
    while queue:
        node, depth = queue.pop(0)
        if isinstance(node, list):
            dicts = [x for x in node if isinstance(x, dict)]
            if len(dicts) > len(best):
                best = dicts
        elif isinstance(node, dict) and depth < 3:
            queue.extend((v, depth + 1) for v in node.values())
    return best


def pctl(sorted_vals: list[float], q: float) -> float:
    """Nearest-rank percentile of an already-sorted, non-empty list."""
    idx = round(q * (len(sorted_vals) - 1))
    idx = min(max(idx, 0), len(sorted_vals) - 1)
    return sorted_vals[idx]


def delta_stats(deltas: list[float]) -> dict[str, float | None]:
    """min/p5/p25/median/p75/p95/max of a (possibly empty) list of deltas."""
    if not deltas:
        return {k: None for k in ("min", "p5", "p25", "median", "p75", "p95", "max")}
    s = sorted(deltas)
    return {
        "min": s[0],
        "p5": pctl(s, 0.05),
        "p25": pctl(s, 0.25),
        "median": statistics.median(deltas),
        "p75": pctl(s, 0.75),
        "p95": pctl(s, 0.95),
        "max": s[-1],
    }


def share(numerator: int, denominator: int) -> float | None:
    """numerator / denominator, or None if denominator is 0."""
    return numerator / denominator if denominator else None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _duration_type(raw: str) -> int:
    value = int(raw)
    if value < MIN_DURATION or value > HARD_MAX_DURATION:
        raise argparse.ArgumentTypeError(
            f"--duration must be in [{MIN_DURATION}, {HARD_MAX_DURATION}]"
        )
    return value


def _label_type(raw: str) -> str:
    if not LABEL_RE.match(raw):
        raise argparse.ArgumentTypeError("--label must match [a-z0-9_-]{1,30}")
    return raw


def _feeds_type(raw: str) -> list[str]:
    codes = [c.strip() for c in raw.split(",") if c.strip()]
    if not codes or any(c not in FEEDS for c in codes):
        raise argparse.ArgumentTypeError(f"--feeds must be a subset of {','.join(FEEDS)}")
    return codes


def _bounded_int(name: str, minimum: int) -> Any:
    def _inner(raw: str) -> int:
        value = int(raw)
        if value < minimum:
            raise argparse.ArgumentTypeError(f"--{name} must be >= {minimum}")
        return value

    return _inner


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse and validate CLI arguments."""
    parser = argparse.ArgumentParser(description="Four-feed new-token race test.")
    parser.add_argument("--feeds", type=_feeds_type, default=list(FEEDS))
    parser.add_argument("--duration", type=_duration_type, default=DEFAULT_DURATION)
    parser.add_argument("--warmup", type=_bounded_int("warmup", 0), default=DEFAULT_WARMUP)
    parser.add_argument("--cooldown", type=_bounded_int("cooldown", 0), default=DEFAULT_COOLDOWN)
    parser.add_argument(
        "--rc-interval", type=_bounded_int("rc-interval", MIN_RC_INTERVAL),
        default=DEFAULT_RC_INTERVAL,
    )  # fmt: skip
    parser.add_argument(
        "--ray-interval", type=_bounded_int("ray-interval", MIN_RAY_INTERVAL),
        default=DEFAULT_RAY_INTERVAL,
    )  # fmt: skip
    parser.add_argument(
        "--reconnect-wait", type=_bounded_int("reconnect-wait", MIN_RECONNECT_WAIT),
        default=DEFAULT_RECONNECT_WAIT,
    )  # fmt: skip
    parser.add_argument("--pp-url", default=DEFAULT_PP_URL)
    parser.add_argument("--pd-url", default=DEFAULT_PD_URL)
    parser.add_argument("--rc-url", default=DEFAULT_RC_URL)
    parser.add_argument("--ray-url", default=DEFAULT_RAY_URL)
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--label", type=_label_type, default=None)
    parser.add_argument("--no-sleep-inhibit", action="store_true")
    parser.add_argument("--analyze", default=None)
    return parser.parse_args(argv)


def _check_ws_url(raw_url: str) -> None:
    parsed = urlparse(raw_url)
    host = parsed.hostname or ""
    if host in ALLOWED_WS_HOSTS or host == "127.0.0.1":
        return
    raise ValueError(f"WebSocket URL host not allowed: {host!r}")


def _check_http_url(raw_url: str) -> None:
    parsed = urlparse(raw_url)
    host = parsed.hostname or ""
    if host in ALLOWED_HTTP_HOSTS or host == "127.0.0.1":
        return
    raise ValueError(f"HTTP URL host not allowed: {host!r}")


def validate_urls(args: argparse.Namespace) -> None:
    """Enforce the WS/HTTP host allow-list on all four feed URLs."""
    _check_ws_url(args.pp_url)
    _check_ws_url(args.pd_url)
    _check_http_url(args.rc_url)
    _check_http_url(args.ray_url)


def resolve_out_dir(repo_root: Path, raw_out_dir: str) -> Path:
    """Resolve --out-dir and enforce it stays under <repo root>/data/."""
    candidate = Path(raw_out_dir)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    resolved = candidate.resolve()
    data_root = (repo_root / "data").resolve()
    if resolved != data_root and data_root not in resolved.parents:
        raise ValueError(f"--out-dir must resolve under {data_root}, got {resolved}")
    return resolved


def utc_stamp(now: datetime) -> str:
    """Format a UTC timestamp as yyyymmddTHHMMSSZ."""
    return now.strftime("%Y%m%dT%H%M%SZ")


# --------------------------------------------------------------------------
# Sleep inhibition (Windows only)
# --------------------------------------------------------------------------


def sleep_inhibit_start(disabled: bool) -> str:
    """Start sleep inhibition if applicable; return the mode string."""
    if disabled:
        return "disabled"
    if sys.platform != "win32":
        return "not_applicable"
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    except (AttributeError, OSError):
        return "not_applicable"
    return "windows_execution_state"


def sleep_inhibit_stop(mode: str) -> None:
    """Restore normal execution state if it was changed."""
    if mode != "windows_execution_state":
        return
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except (AttributeError, OSError):
        pass


# --------------------------------------------------------------------------
# Capture writer
# --------------------------------------------------------------------------


class CaptureWriter:
    """Appends one JSON line per accepted event or meta record, flushing periodically."""

    def __init__(self, path: Path) -> None:
        self._fh = path.open("a", encoding="utf-8")
        self._since_flush = 0

    def write(self, record: dict[str, Any]) -> None:
        """Append one JSON line."""
        self._fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._since_flush += 1
        if self._since_flush >= CAPTURE_FLUSH_EVERY:
            self._fh.flush()
            self._since_flush = 0

    def close(self) -> None:
        """Flush and close the capture file."""
        self._fh.flush()
        self._fh.close()


# --------------------------------------------------------------------------
# Aggregator: shared accumulation logic for both live runs and --analyze
# --------------------------------------------------------------------------


@dataclass
class FeedHealth:
    """Final per-feed counters, as written in a "stats" meta record."""

    connect_outcome: str | None = None
    connect_ms: float | None = None
    ack_ms: float | None = None
    disconnects: int = 0
    reconnect_used: bool = False
    stop_reason: str | None = None
    requests: int = 0
    failures: int = 0
    invalid: int = 0
    accepted: int = 0
    duplicates: int = 0
    longest_silence_s: float | None = None
    poll_interval_median_s: float | None = None


@dataclass
class ClockJump:
    """One detected wall/monotonic clock divergence on a feed."""

    feed: str
    jump_s: float
    mono_before: int
    mono_after: int


class Aggregator:
    """Accumulates every accepted event and meta record into the R1-R7 report."""

    def __init__(self) -> None:
        self.first_seen: dict[tuple[str, Kind, str], tuple[int, int]] = {}
        self.dup_seen: set[tuple[str, Kind, str]] = set()
        self.dup_counts: dict[str, int] = dict.fromkeys(FEEDS, 0)
        self.event_counts: dict[str, int] = dict.fromkeys(FEEDS, 0)
        self.unique_mints: dict[str, set[str]] = {f: set() for f in FEEDS}
        self.pool_by_mint: dict[tuple[Kind, str], str] = {}
        self.src_ms: dict[tuple[str, str], int] = {}
        self.health: dict[str, FeedHealth] = {f: FeedHealth() for f in FEEDS}
        self.jumps: list[ClockJump] = []
        self.min_mono_ns: int | None = None
        self.max_mono_ns: int | None = None
        self.min_wall_ns: int | None = None
        self.max_wall_ns: int | None = None

    def _track_span(self, mono_ns: int, wall_ns: int) -> None:
        if self.min_mono_ns is None or mono_ns < self.min_mono_ns:
            self.min_mono_ns = mono_ns
            self.min_wall_ns = wall_ns
        if self.max_mono_ns is None or mono_ns > self.max_mono_ns:
            self.max_mono_ns = mono_ns
            self.max_wall_ns = wall_ns

    def record_event(self, rec: dict[str, Any]) -> None:
        """Fold one accepted event capture line into the aggregate state."""
        feed, kind, mint = rec["feed"], rec["kind"], rec["mint"]
        mono_ns, wall_ns = rec["mono_ns"], rec["wall_ns"]
        self._track_span(mono_ns, wall_ns)
        self.event_counts[feed] += 1
        self.unique_mints[feed].add(mint)
        key = (feed, kind, mint)
        if key in self.first_seen:
            self.dup_counts[feed] += 1
        else:
            self.first_seen[key] = (mono_ns, wall_ns)
        pool = rec.get("pool")
        if pool is not None and (kind, mint) not in self.pool_by_mint:
            self.pool_by_mint[(kind, mint)] = pool
        src_ms = rec.get("src_ms")
        if src_ms is not None:
            self.src_ms.setdefault((feed, mint), src_ms)

    def record_meta(self, rec: dict[str, Any]) -> None:
        """Fold one meta capture line into the aggregate state."""
        kind = rec["meta"]
        feed = rec.get("feed")
        mono_ns = rec.get("mono_ns")
        wall_ns = rec.get("wall_ns")
        if isinstance(mono_ns, int) and isinstance(wall_ns, int):
            self._track_span(mono_ns, wall_ns)
        if kind == "clock_jump":
            self.jumps.append(
                ClockJump(feed, rec["jump_s"], rec["mono_before"], rec["mono_after"])
            )
            return
        if feed is None or feed not in self.health:
            return
        h = self.health[feed]
        if kind == "connect":
            h.connect_outcome = rec["outcome"]
            h.connect_ms = rec.get("connect_ms")
        elif kind == "ack":
            h.ack_ms = rec.get("ack_ms")
        elif kind == "disconnect":
            h.disconnects += 1
        elif kind == "reconnect":
            h.reconnect_used = True
        elif kind == "feed_failure":
            h.stop_reason = rec.get("reason")
        elif kind == "stats":
            h.requests = rec.get("requests", 0)
            h.failures = rec.get("failures", 0)
            h.invalid = rec.get("invalid", 0)
            h.accepted = rec.get("accepted", 0)
            h.duplicates = rec.get("duplicates", 0)
            h.longest_silence_s = rec.get("longest_silence_s")
            h.poll_interval_median_s = rec.get("poll_interval_median_s")

    def ingest_line(self, line: str) -> None:
        """Parse one capture line and fold it in (event or meta)."""
        obj = json.loads(line)
        if "meta" in obj:
            self.record_meta(obj)
        else:
            self.record_event(obj)

    # ---- jump-span exclusion ----

    def spans_jump(self, mono_a: int, mono_b: int) -> bool:
        """Whether the interval between two mono_ns points crosses a recorded jump."""
        lo, hi = (mono_a, mono_b) if mono_a <= mono_b else (mono_b, mono_a)
        return any(lo <= j.mono_before and j.mono_after <= hi for j in self.jumps)

    @property
    def suspect_run(self) -> bool:
        """Whether any detected clock jump exceeded the suspect threshold."""
        return any(abs(j.jump_s) > CLOCK_JUMP_SUSPECT_S for j in self.jumps)

    # ---- warmup/cooldown window ----

    def in_window(self, mono_ns: int, warmup_s: float, cooldown_s: float) -> bool:
        """Whether a mono_ns timestamp falls inside [start+warmup, end-cooldown]."""
        if self.min_mono_ns is None or self.max_mono_ns is None:
            return True
        lo = self.min_mono_ns + int(warmup_s * 1e9)
        hi = self.max_mono_ns - int(cooldown_s * 1e9)
        return lo <= mono_ns <= hi

    # ---- R1 / R2: pairwise create-race / migration-race stats ----

    def _pair_stats(
        self, a_feed: str, a_kind: Kind, b_feed: str, b_kind: Kind,
        warmup_s: float, cooldown_s: float, pool_filter: str | None = None,
    ) -> dict[str, Any]:  # fmt: skip
        a_mints = {
            m for (f, k, m) in self.first_seen if f == a_feed and k == a_kind
            and (pool_filter is None or self.pool_by_mint.get((a_kind, m)) == pool_filter)
        }  # fmt: skip
        b_mints = {m for (f, k, m) in self.first_seen if f == b_feed and k == b_kind}
        both = sorted(a_mints & b_mints)
        only_a = sorted(a_mints - b_mints)
        only_b = sorted(b_mints - a_mints)

        excluded_jump = 0
        deltas: list[float] = []
        pairs_in_window: list[tuple[str, float, int]] = []  # mint, delta_ms, a_mono
        for mint in both:
            a_mono, _a_wall = self.first_seen[(a_feed, a_kind, mint)]
            b_mono, _b_wall = self.first_seen[(b_feed, b_kind, mint)]
            if not (self.in_window(a_mono, warmup_s, cooldown_s) and self.in_window(b_mono, warmup_s, cooldown_s)):
                continue
            if self.spans_jump(a_mono, b_mono):
                excluded_jump += 1
                continue
            delta_ms = (b_mono - a_mono) / 1e6
            deltas.append(delta_ms)
            pairs_in_window.append((mint, delta_ms, a_mono))

        only_a_in_window = [
            m for m in only_a
            if self.in_window(self.first_seen[(a_feed, a_kind, m)][0], warmup_s, cooldown_s)
        ]  # fmt: skip
        only_b_in_window = [
            m for m in only_b
            if self.in_window(self.first_seen[(b_feed, b_kind, m)][0], warmup_s, cooldown_s)
        ]  # fmt: skip

        n_both = len(pairs_in_window)
        a_first = sum(1 for _, d, _ in pairs_in_window if d > 0)
        ties = sum(1 for _, d, _ in pairs_in_window if abs(d) < 1.0)

        slices: list[dict[str, Any]] = []
        if self.min_mono_ns is not None and self.max_mono_ns is not None and pairs_in_window:
            span = max(1, self.max_mono_ns - self.min_mono_ns)
            buckets: list[list[float]] = [[] for _ in range(5)]
            for _, d, a_mono in pairs_in_window:
                idx = min(4, int((a_mono - self.min_mono_ns) * 5 / span))
                buckets[idx].append(d)
            slices = [delta_stats(b) for b in buckets]

        return {
            "n_both": n_both,
            "only_a": len(only_a_in_window),
            "only_b": len(only_b_in_window),
            "excluded_clock_jump": excluded_jump,
            "a_first_share": share(a_first, n_both),
            "tie_share": share(ties, n_both),
            "delta_ms_a_to_b": delta_stats(deltas),
            "delta_ms_a_to_b_by_slice": slices,
            "miss_rate_b": share(len(only_a_in_window), n_both + len(only_a_in_window)),
            "miss_rate_a": share(len(only_b_in_window), n_both + len(only_b_in_window)),
        }

    def r1_pump_creates(self, warmup_s: float, cooldown_s: float) -> dict[str, Any]:
        """R1: Pump.fun creates, pp vs pd."""
        return self._pair_stats("pp", "create", "pd", "create", warmup_s, cooldown_s, pool_filter="pump")

    def r2_migrations(self, warmup_s: float, cooldown_s: float) -> dict[str, Any]:
        """R2: migrations, pp (kind=migrate) vs pd (kind=migrate, from create_pool)."""
        return self._pair_stats("pp", "migrate", "pd", "migrate", warmup_s, cooldown_s)

    def r4_bonk(self, warmup_s: float, cooldown_s: float) -> dict[str, Any]:
        """R4: LetsBonk, ray (listed) vs pp creates with pool bonk."""
        ray_mints = {m for (f, k, m) in self.first_seen if f == "ray" and k == "listed"}
        pp_bonk = {
            m for (f, k, m) in self.first_seen
            if f == "pp" and k == "create" and self.pool_by_mint.get(("create", m)) == "bonk"
        }  # fmt: skip
        both = sorted(ray_mints & pp_bonk)
        only_ray = sorted(ray_mints - pp_bonk)
        only_pp = sorted(pp_bonk - ray_mints)
        lags: list[float] = []
        create_at_lags: list[float] = []
        for mint in both:
            ray_mono, ray_wall = self.first_seen[("ray", "listed", mint)]
            pp_mono, _pp_wall = self.first_seen[("pp", "create", mint)]
            if self.spans_jump(ray_mono, pp_mono):
                continue
            lags.append((ray_mono - pp_mono) / 1e9)
            src_ms = self.src_ms.get(("ray", mint))
            if src_ms is not None:
                create_at_lags.append(ray_wall / 1e9 - src_ms / 1000.0)
        return {
            "both": len(both),
            "only_pp": len(only_pp),
            "only_ray": len(only_ray),
            "lag_s_ray_minus_pp": delta_stats(lags),
            "ray_createAt_lag_s": delta_stats(create_at_lags),
        }

    def r3_rugcheck(self, warmup_s: float, cooldown_s: float) -> dict[str, Any]:
        """R3: independent check of pp/pd coverage against RugCheck's new-token list."""
        rc_entries = [
            (m, self.first_seen[("rc", "listed", m)])
            for (f, k, m) in self.first_seen if f == "rc" and k == "listed"
        ]
        if self.min_mono_ns is None or self.max_mono_ns is None:
            return {"total": 0}
        lo = self.min_mono_ns + 60_000_000_000
        hi = self.max_mono_ns - 30_000_000_000
        checked = [(m, mono, wall) for m, (mono, wall) in rc_entries if lo <= mono <= hi]

        def seen_by(feed: str, mint: str) -> bool:
            return any(k[0] == feed and k[2] == mint for k in self.first_seen if k[0] == feed)

        pp_mints = {m for (f, k, m) in self.first_seen if f == "pp"}
        pd_mints = {m for (f, k, m) in self.first_seen if f == "pd"}

        total = len(checked)
        by_pp = sum(1 for m, _, _ in checked if m in pp_mints)
        by_pd = sum(1 for m, _, _ in checked if m in pd_mints)
        by_either = sum(1 for m, _, _ in checked if m in pp_mints or m in pd_mints)
        by_neither = total - by_either

        pump_like = [(m, mono, wall) for m, mono, wall in checked if m.endswith("pump")]
        pump_by_pp = sum(1 for m, _, _ in pump_like if m in pp_mints)
        pump_by_pd = sum(1 for m, _, _ in pump_like if m in pd_mints)
        pump_by_either = sum(1 for m, _, _ in pump_like if m in pp_mints or m in pd_mints)
        pump_by_neither = len(pump_like) - pump_by_either

        covered = [m for m, _, _ in checked if m in pp_mints or m in pd_mints]
        pp_miss = share(sum(1 for m in covered if m not in pp_mints), len(covered))
        pd_miss = share(sum(1 for m in covered if m not in pd_mints), len(covered))

        rc_lags: list[float] = []
        createat_lags: list[float] = []
        for m, mono, wall in checked:
            relay_sights = [
                self.first_seen[(f, k, m)] for (f, k, mm) in self.first_seen
                if mm == m and f in ("pp", "pd")
            ]  # fmt: skip
            if not relay_sights:
                continue
            earliest_relay_mono, earliest_relay_wall = min(relay_sights, key=lambda x: x[0])
            if self.spans_jump(mono, earliest_relay_mono):
                continue
            rc_lags.append((wall - earliest_relay_wall) / 1e9)
            src_ms = self.src_ms.get(("rc", m))
            if src_ms is not None:
                createat_lags.append(wall / 1e9 - src_ms / 1000.0)

        return {
            "total": total,
            "seen_by_pp": by_pp,
            "seen_by_pd": by_pd,
            "seen_by_either": by_either,
            "seen_by_neither": by_neither,
            "pump_like": {
                "total": len(pump_like),
                "seen_by_pp": pump_by_pp,
                "seen_by_pd": pump_by_pd,
                "seen_by_either": pump_by_either,
                "seen_by_neither": pump_by_neither,
            },
            "pp_miss_rate_vs_covered": pp_miss,
            "pd_miss_rate_vs_covered": pd_miss,
            "rc_lag_s_vs_relay": delta_stats(rc_lags),
            "rc_createAt_lag_s": delta_stats(createat_lags),
        }

    def r5_health(self) -> dict[str, Any]:
        """R5: per-feed health counters."""
        out: dict[str, Any] = {}
        for f in FEEDS:
            h = self.health[f]
            span_s = (
                (self.max_mono_ns - self.min_mono_ns) / 1e9
                if self.min_mono_ns is not None and self.max_mono_ns is not None
                else None
            )
            eps_min = (self.event_counts[f] / (span_s / 60.0)) if span_s and span_s > 0 else None
            out[f] = {
                "accepted": h.accepted or self.event_counts[f],
                "invalid": h.invalid,
                "duplicates": h.duplicates or self.dup_counts[f],
                "unique_mints": len(self.unique_mints[f]),
                "events_per_minute": eps_min,
                "connect_ms": h.connect_ms,
                "ack_ms": h.ack_ms,
                "disconnects": h.disconnects,
                "reconnect_used": h.reconnect_used,
                "longest_silence_s": h.longest_silence_s,
                "requests": h.requests,
                "failures": h.failures,
                "stop_reason": h.stop_reason,
                "poll_interval_median_s": h.poll_interval_median_s,
                "connect_outcome": h.connect_outcome,
            }
        return out

    def r6_burstiness(self) -> dict[str, Any]:
        """R6: events-per-10s-bucket for pp and pd creates."""
        out: dict[str, Any] = {}
        for f in ("pp", "pd"):
            times = sorted(mono for (ff, k, m), (mono, wall) in self.first_seen.items() if ff == f and k == "create")
            if not times or self.min_mono_ns is None:
                out[f] = {"median": None, "p95": None, "max": None}
                continue
            buckets: dict[int, int] = {}
            for t in times:
                idx = int((t - self.min_mono_ns) / 1_000_000_000 // 10)
                buckets[idx] = buckets.get(idx, 0) + 1
            counts = [float(v) for v in buckets.values()]
            s = sorted(counts)
            out[f] = {"median": statistics.median(counts), "p95": pctl(s, 0.95), "max": s[-1]}
        return out

    def r7_clock(self) -> dict[str, Any]:
        """R7: clock jump events and suspect_run."""
        return {
            "clock_jumps": [
                {"feed": j.feed, "jump_s": j.jump_s} for j in self.jumps
            ],
            "suspect_run": self.suspect_run,
        }


# --------------------------------------------------------------------------
# Live-run per-feed state (not persisted; feeds the "stats" meta line)
# --------------------------------------------------------------------------


@dataclass
class LiveFeedState:
    """Mutable bookkeeping for one feed during a live run."""

    requests: int = 0
    failures: int = 0
    invalid: int = 0
    accepted: int = 0
    duplicates: int = 0
    seen_keys: set[tuple[Kind, str]] = field(default_factory=set)
    last_activity_mono_ns: int | None = None
    longest_silence_s: float = 0.0
    poll_gaps_s: list[float] = field(default_factory=list)
    last_poll_mono_ns: int | None = None
    stopped: bool = False
    stop_reason: str | None = None
    prev_wall_ns: int | None = None
    prev_mono_ns: int | None = None

    def note_activity(self, mono_ns: int) -> None:
        if self.last_activity_mono_ns is not None:
            gap = (mono_ns - self.last_activity_mono_ns) / 1e9
            self.longest_silence_s = max(self.longest_silence_s, gap)
        self.last_activity_mono_ns = mono_ns

    def note_poll(self, mono_ns: int) -> None:
        if self.last_poll_mono_ns is not None:
            self.poll_gaps_s.append((mono_ns - self.last_poll_mono_ns) / 1e9)
        self.last_poll_mono_ns = mono_ns
        self.note_activity(mono_ns)


def check_clock_jump(
    feed: str, state: LiveFeedState, wall_ns: int, mono_ns: int, capture: CaptureWriter
) -> None:
    """Detect and record a wall/monotonic divergence since this feed's last receipt."""
    if state.prev_wall_ns is not None and state.prev_mono_ns is not None:
        delta_wall = (wall_ns - state.prev_wall_ns) / 1e9
        delta_mono = (mono_ns - state.prev_mono_ns) / 1e9
        jump_s = delta_wall - delta_mono
        if abs(jump_s) > CLOCK_JUMP_THRESHOLD_S:
            capture.write(
                {
                    "meta": "clock_jump", "feed": feed, "jump_s": round(jump_s, 3),
                    "mono_before": state.prev_mono_ns, "mono_after": mono_ns,
                    "mono_ns": mono_ns, "wall_ns": wall_ns,
                }
            )  # fmt: skip
    state.prev_wall_ns = wall_ns
    state.prev_mono_ns = mono_ns


def write_stats(feed: str, state: LiveFeedState, capture: CaptureWriter) -> None:
    """Write the final per-feed counters meta line."""
    median_poll = statistics.median(state.poll_gaps_s) if state.poll_gaps_s else None
    capture.write(
        {
            "meta": "stats", "feed": feed, "requests": state.requests,
            "failures": state.failures, "invalid": state.invalid,
            "accepted": state.accepted, "duplicates": state.duplicates,
            "longest_silence_s": round(state.longest_silence_s, 3) if state.last_activity_mono_ns else None,
            "poll_interval_median_s": median_poll,
            "mono_ns": time.perf_counter_ns(), "wall_ns": time.time_ns(),
        }
    )  # fmt: skip


# --------------------------------------------------------------------------
# WebSocket feeds: pp, pd
# --------------------------------------------------------------------------


async def _pp_send_subscribe(ws: Any) -> None:
    await ws.send(json.dumps({"method": "subscribeNewToken"}))
    await asyncio.sleep(SUBSCRIBE_GAP_S)
    await ws.send(json.dumps({"method": "subscribeMigration"}))


def _pp_handle_message(parsed: dict[str, Any], state: LiveFeedState) -> dict[str, Any] | None:
    if "mint" not in parsed:
        return None  # control frame (has "message")
    mint, sig, tx_type, pool = parsed.get("mint"), parsed.get("signature"), parsed.get("txType"), parsed.get("pool")
    kind: Kind | None = "create" if tx_type == "create" else "migrate" if tx_type == "migrate" else None
    if kind is None or not valid_mint(mint) or not valid_sig(sig) or (pool is not None and not valid_pool(pool)):
        state.invalid += 1
        return None
    rec: dict[str, Any] = {"feed": "pp", "kind": kind, "mint": mint, "sig": sig}
    if valid_pool(pool):
        rec["pool"] = pool
    return rec


async def _pd_send_subscribe(ws: Any) -> None:
    await ws.send(json.dumps({"method": "subscribeNewToken"}))


def _pd_handle_message(parsed: dict[str, Any], state: LiveFeedState) -> dict[str, Any] | None:
    if "type" in parsed:
        return None  # control frame
    mint, sig, tx_type = parsed.get("mint"), parsed.get("signature"), parsed.get("txType")
    kind: Kind | None = "create" if tx_type == "create" else "migrate" if tx_type == "create_pool" else None
    if kind is None or not valid_mint(mint) or not valid_sig(sig):
        state.invalid += 1
        return None
    return {"feed": "pd", "kind": kind, "mint": mint, "sig": sig}


WS_HANDLERS = {"pp": (_pp_send_subscribe, _pp_handle_message), "pd": (_pd_send_subscribe, _pd_handle_message)}


async def _ws_session(
    feed: str, url: str, agg_state: LiveFeedState, capture: CaptureWriter, deadline_mono: float
) -> bool:
    """Run one connection attempt; return True if a ConnectionClosed drop occurred."""
    send_subscribe, handle_message = WS_HANDLERS[feed]
    connect_start = time.perf_counter()
    try:
        async with connect(url, open_timeout=OPEN_TIMEOUT_S) as ws:
            connect_ms = (time.perf_counter() - connect_start) * 1000
            capture.write(
                {
                    "meta": "connect", "feed": feed, "outcome": "connected",
                    "connect_ms": connect_ms, "mono_ns": time.perf_counter_ns(), "wall_ns": time.time_ns(),
                }
            )  # fmt: skip
            sub_start = time.perf_counter()
            await send_subscribe(ws)
            acked = False
            while True:
                remaining = deadline_mono - time.perf_counter()
                if remaining <= 0:
                    return False
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except TimeoutError:
                    return False
                except ConnectionClosed:
                    capture.write(
                        {"meta": "disconnect", "feed": feed, "mono_ns": time.perf_counter_ns(), "wall_ns": time.time_ns()}
                    )  # fmt: skip
                    return True

                wall_ns, mono_ns = time.time_ns(), time.perf_counter_ns()
                check_clock_jump(feed, agg_state, wall_ns, mono_ns, capture)
                raw = message if isinstance(message, str) else message.decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    agg_state.invalid += 1
                    continue
                if not isinstance(parsed, dict):
                    agg_state.invalid += 1
                    continue

                rec = handle_message(parsed, agg_state)
                if rec is None:
                    if not acked:
                        acked = True
                        capture.write(
                            {
                                "meta": "ack", "feed": feed,
                                "ack_ms": (time.perf_counter() - sub_start) * 1000,
                                "mono_ns": mono_ns, "wall_ns": wall_ns,
                            }
                        )  # fmt: skip
                    continue

                agg_state.accepted += 1
                agg_state.note_activity(mono_ns)
                key = (rec["kind"], rec["mint"])
                if key in agg_state.seen_keys:
                    agg_state.duplicates += 1
                else:
                    agg_state.seen_keys.add(key)
                rec["mono_ns"], rec["wall_ns"] = mono_ns, wall_ns
                capture.write(rec)
    except InvalidStatus as exc:
        capture.write(
            {
                "meta": "connect", "feed": feed, "outcome": "failed",
                "http_status": exc.response.status_code,
                "mono_ns": time.perf_counter_ns(), "wall_ns": time.time_ns(),
            }
        )  # fmt: skip
        return False
    except Exception:  # noqa: BLE001 - must record and continue, never crash the run
        capture.write(
            {"meta": "connect", "feed": feed, "outcome": "failed", "mono_ns": time.perf_counter_ns(), "wall_ns": time.time_ns()}
        )  # fmt: skip
        return False


async def run_ws_feed(
    feed: str, url: str, capture: CaptureWriter, deadline_mono: float, reconnect_wait: float
) -> None:
    """Run a websocket feed: one connection, at most one reconnect after a drop."""
    state = LiveFeedState()
    dropped = await _ws_session(feed, url, state, capture, deadline_mono)
    if dropped and time.perf_counter() < deadline_mono:
        wait_s = min(reconnect_wait, max(0.0, deadline_mono - time.perf_counter()))
        await asyncio.sleep(wait_s)
        if time.perf_counter() < deadline_mono:
            dropped_again = await _ws_session(feed, url, state, capture, deadline_mono)
            capture.write(
                {
                    "meta": "reconnect", "feed": feed, "outcome": "failed" if dropped_again else "connected",
                    "mono_ns": time.perf_counter_ns(), "wall_ns": time.time_ns(),
                }
            )  # fmt: skip
            if dropped_again:
                capture.write(
                    {
                        "meta": "feed_failure", "feed": feed, "reason": "reconnect_exhausted",
                        "mono_ns": time.perf_counter_ns(), "wall_ns": time.time_ns(),
                    }
                )  # fmt: skip
    write_stats(feed, state, capture)


# --------------------------------------------------------------------------
# Polled feeds: rc, ray
# --------------------------------------------------------------------------


def http_get_sync(url: str) -> tuple[int | None, dict[str, str], bytes, str | None]:
    """Blocking, credential-free, single-attempt GET. Run via run_in_executor."""
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    )  # fmt: skip
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            body = resp.read(READ_LIMIT_BYTES)
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, body, None
    except urllib.error.HTTPError as exc:
        body = exc.read(READ_LIMIT_BYTES)
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, body, None
    except Exception as exc:  # noqa: BLE001 - must record, never crash the poll loop
        return None, {}, b"", type(exc).__name__


def _is_hard_block(status: int | None, headers: dict[str, str]) -> str | None:
    if status in (401, 403, 429):
        return str(status)
    if "cf-mitigated" in headers:
        return "challenge"
    ctype = headers.get("content-type", "").lower()
    return "html" if "html" in ctype else None


def _extract_rc_items(data: Any) -> list[dict[str, Any]]:
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


async def run_poll_feed(
    feed: str, base_url: str, path: str, interval_s: float, capture: CaptureWriter, deadline_mono: float
) -> None:
    """Run a polled feed: sequential bounded-interval GETs, one mint-first-seen write each."""
    state = LiveFeedState()
    seen_mints: set[str] = set()
    consecutive_failures = 0
    url = base_url.rstrip("/") + path
    loop = asyncio.get_running_loop()

    while time.perf_counter() < deadline_mono and state.requests < HARD_REQUEST_CAP and not state.stopped:
        tick_start = time.perf_counter()
        status, headers, body, error = await loop.run_in_executor(None, http_get_sync, url)
        state.requests += 1
        mono_ns, wall_ns = time.perf_counter_ns(), time.time_ns()
        check_clock_jump(feed, state, wall_ns, mono_ns, capture)
        state.note_poll(mono_ns)

        if error is not None:
            state.failures += 1
            consecutive_failures += 1
        else:
            reason = _is_hard_block(status, headers)
            if reason is not None:
                capture.write(
                    {"meta": "feed_failure", "feed": feed, "reason": reason, "mono_ns": mono_ns, "wall_ns": wall_ns}
                )  # fmt: skip
                state.stopped, state.stop_reason = True, reason
                break
            if status is not None and status >= 500:
                state.failures += 1
                consecutive_failures += 1
            else:
                consecutive_failures = 0
                try:
                    data = json.loads(body.decode("utf-8", errors="replace"))
                except ValueError:
                    data = None
                items = find_item_list(data) if feed == "ray" else _extract_rc_items(data)
                for item in items:
                    mint, created = item.get("mint"), item.get("createAt")
                    src_ms = parse_create_at(created)
                    if not valid_mint(mint) or src_ms is None:
                        state.invalid += 1
                        continue
                    state.accepted += 1
                    if mint in seen_mints:
                        state.duplicates += 1
                        continue
                    seen_mints.add(mint)
                    capture.write(
                        {
                            "feed": feed, "kind": "listed", "mint": mint, "src_ms": src_ms,
                            "mono_ns": mono_ns, "wall_ns": wall_ns,
                        }
                    )  # fmt: skip

        if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
            capture.write(
                {
                    "meta": "feed_failure", "feed": feed, "reason": "consecutive_failures",
                    "mono_ns": mono_ns, "wall_ns": wall_ns,
                }
            )  # fmt: skip
            state.stopped, state.stop_reason = True, "consecutive_failures"
            break

        elapsed = time.perf_counter() - tick_start
        remaining = deadline_mono - time.perf_counter()
        await asyncio.sleep(max(0.0, min(interval_s - elapsed, remaining)))

    write_stats(feed, state, capture)


# --------------------------------------------------------------------------
# Live-run orchestration
# --------------------------------------------------------------------------


async def run_live(args: argparse.Namespace, capture: CaptureWriter) -> None:
    """Run all selected feeds concurrently for args.duration seconds."""
    deadline_mono = time.perf_counter() + float(args.duration)
    tasks = []
    if "pp" in args.feeds:
        tasks.append(run_ws_feed("pp", args.pp_url, capture, deadline_mono, float(args.reconnect_wait)))
    if "pd" in args.feeds:
        tasks.append(run_ws_feed("pd", args.pd_url, capture, deadline_mono, float(args.reconnect_wait)))
    if "rc" in args.feeds:
        tasks.append(run_poll_feed("rc", args.rc_url, RC_PATH, float(args.rc_interval), capture, deadline_mono))
    if "ray" in args.feeds:
        tasks.append(run_poll_feed("ray", args.ray_url, RAY_PATH, float(args.ray_interval), capture, deadline_mono))
    await asyncio.gather(*tasks)


# --------------------------------------------------------------------------
# Report building
# --------------------------------------------------------------------------


def build_summary(agg: Aggregator, args: argparse.Namespace, connected_feeds: set[str]) -> dict[str, Any]:
    """Build the full R1-R7 summary dict."""
    warmup_s, cooldown_s = float(args.warmup), float(args.cooldown)
    return {
        "host_note": "All figures below are specific to this host and this run's time window only.",
        "feeds_requested": args.feeds,
        "feeds_connected": sorted(connected_feeds),
        "duration_s": args.duration,
        "warmup_s": args.warmup,
        "cooldown_s": args.cooldown,
        "r1_pump_creates_pp_vs_pd": agg.r1_pump_creates(warmup_s, cooldown_s),
        "r2_migrations_pp_vs_pd": agg.r2_migrations(warmup_s, cooldown_s),
        "r3_rugcheck_independent_check": agg.r3_rugcheck(warmup_s, cooldown_s),
        "r4_letsbonk_ray_vs_pp": agg.r4_bonk(warmup_s, cooldown_s),
        "r5_feed_health": agg.r5_health(),
        "r6_burstiness": agg.r6_burstiness(),
        "r7_clock": agg.r7_clock(),
    }


def build_verdict(summary: dict[str, Any], sleep_mode: str) -> str:
    """Build the one-line VERDICT summary."""
    r1, r2, r3, r7 = (
        summary["r1_pump_creates_pp_vs_pd"], summary["r2_migrations_pp_vs_pd"],
        summary["r3_rugcheck_independent_check"], summary["r7_clock"],
    )  # fmt: skip
    health = summary["r5_feed_health"]
    feeds_ok = sorted(f for f, h in health.items() if h["accepted"] > 0 or h["requests"] > 0)
    feeds_failed = sorted(f for f in FEEDS if f not in feeds_ok)
    return (
        f"VERDICT feeds_ok={','.join(feeds_ok) or 'none'} feeds_failed={','.join(feeds_failed) or 'none'} "
        f"pp_pd_pairs={r1['n_both']} pp_first_share={r1['a_first_share']} "
        f"median_delta_ms_pp_to_pd={r1['delta_ms_a_to_b']['median']} "
        f"pp_miss_rate={r1['miss_rate_a']} pd_miss_rate={r1['miss_rate_b']} "
        f"migration_pairs={r2['n_both']} rc_checked={r3.get('total', 0)} "
        f"suspect_run={str(r7['suspect_run']).lower()} sleep_inhibit={sleep_mode}"
    )


def build_digest(summary: dict[str, Any], verdict: str) -> str:
    """Build a human-readable digest, VERDICT first, at most DIGEST_MAX_LINES lines."""
    lines = [
        verdict, "",
        "All numbers in this digest are specific to this host and to the time window of this run only.",
        f"Feeds requested: {','.join(summary['feeds_requested'])}  connected: {','.join(summary['feeds_connected'])}",
        f"Duration: {summary['duration_s']}s  warmup: {summary['warmup_s']}s  cooldown: {summary['cooldown_s']}s",
        "",
    ]  # fmt: skip
    body = json.dumps(summary, indent=2, default=str).splitlines()
    lines.extend(body)
    if len(lines) > DIGEST_MAX_LINES:
        lines = lines[: DIGEST_MAX_LINES - 1] + ["... (truncated)"]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# --analyze mode
# --------------------------------------------------------------------------


def run_analyze(capture_path: Path, args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    """Recompute the full report from a saved capture file, no network access."""
    agg = Aggregator()
    connected_feeds: set[str] = set()
    with capture_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            agg.ingest_line(line)
    for f in FEEDS:
        obj_outcome = agg.health[f].connect_outcome
        if obj_outcome == "connected" or agg.health[f].requests > 0:
            connected_feeds.add(f)
    summary = build_summary(agg, args, connected_feeds)
    verdict = build_verdict(summary, "n/a_analyze")
    return summary, verdict


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point: run the race test (live or --analyze) and write/print results."""
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    args = parse_args(argv)
    repo_root = Path(__file__).resolve().parent.parent

    try:
        out_dir = resolve_out_dir(repo_root, args.out_dir)
        if args.analyze is None:
            validate_urls(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(UTC)
    stamp = utc_stamp(now)
    stem = f"race_{stamp}" + (f"_{args.label}" if args.label else "")
    summary_path = out_dir / f"{stem}.summary.json"
    digest_path = out_dir / f"{stem}.digest.txt"

    if args.analyze is not None:
        capture_path = Path(args.analyze)
        print(f"{now.isoformat()} analyze capture={capture_path.name}")
        summary, verdict = run_analyze(capture_path, args)
        sleep_mode = "n/a_analyze"
        exit_code = 0
        feeds_with_events = {
            f for f, h in summary["r5_feed_health"].items() if h["accepted"] > 0 or h["requests"] > 0
        }  # fmt: skip
    else:
        capture_path = out_dir / f"{stem}.capture.jsonl"
        capture = CaptureWriter(capture_path)
        sleep_mode = sleep_inhibit_start(args.no_sleep_inhibit)
        print(
            f"{now.isoformat()} start feeds={','.join(args.feeds)} duration={args.duration} "
            f"sleep_inhibit={sleep_mode}"
        )  # fmt: skip
        exit_code = 0
        try:
            try:
                asyncio.run(run_live(args, capture))
            except KeyboardInterrupt:
                exit_code = 130
        finally:
            capture.close()
            sleep_inhibit_stop(sleep_mode)

        agg = Aggregator()
        with capture_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    agg.ingest_line(line)
        connected_feeds = {
            f for f in FEEDS
            if f in args.feeds and (agg.health[f].connect_outcome == "connected" or agg.health[f].requests > 0)
        }  # fmt: skip
        summary = build_summary(agg, args, connected_feeds)
        verdict = build_verdict(summary, sleep_mode)
        feeds_with_events = {
            f for f, h in summary["r5_feed_health"].items() if h["accepted"] > 0 or h["requests"] > 0
        }  # fmt: skip

    summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    digest = build_digest(summary, verdict)
    digest_path.write_text(digest, encoding="utf-8")

    print(verdict)

    if args.analyze is not None:
        return 0
    if exit_code == 130:
        return 130
    return 0 if feeds_with_events else 2


if __name__ == "__main__":
    sys.exit(main())
