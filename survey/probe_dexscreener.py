"""One-shot, anonymous probe of DexScreener's public API.

Throwaway Phase 1 measurement script. Never imported by src/. Mirrors the
conventions of survey/probe_raydium.py (request layer, printable-key rules,
Shape walker, summary style, VERDICT line). Stdlib only. All response text
about tokens is hostile: values are never printed except numbers, booleans,
null counts, string length ranges, allow-listed enum-like strings, distinct
pubkeys at a narrow allow-listed path and a small set of allow-listed header
values. Dictionary keys are only printed when they match a strict identifier
pattern.

Covers two hosts: DexScreener's public API (keyless) and RugCheck's
new-token feed (used only to source fresh mints for the appearance-lag
measurement).
"""

from __future__ import annotations

import argparse
import ctypes
import itertools
import json
import math
import re
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HOSTS = ("dexscreener", "rugcheck")
DEFAULT_URLS = {
    "dexscreener": "https://api.dexscreener.com",
    "rugcheck": "https://api.rugcheck.xyz",
}
PHASE_ORDER = ["shapes", "profiles", "appear", "pairfresh", "limits"]
USER_AGENT = "floor-mop-survey/0.1"
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
REQUEST_TIMEOUT_S = 20.0
MIN_SPACING_S = 0.5
CAPTURE_TRUNCATE = 200_000
CAPTURE_FLUSH_EVERY = 20
READ_LIMIT_BYTES = 8_000_000
REQUEST_CAP = 350
DEFAULT_OUT_DIR = "data/survey"
CLOCK_JUMP_THRESHOLD_S = 3.0
CLOCK_JUMP_SUSPECT_S = 60.0

MAX_WALK_DEPTH = 5
MAX_NUMERIC_PATHS = 150
MAX_ENUM_VALUES = 12
MAX_DICT_KEYS_PRINTABLE = 60
MAX_PUBKEYS_TRACKED = 5000
MAX_QUOTE_ADDR_PRINTABLE = 8

LIMITS_300_BURST = 8
LIMITS_300_OFFSETS_S = (1, 2, 4)
LIMITS_60_BURST = 12
LIMITS_60_OFFSETS_S = (1, 2, 4)
LIMITS_CLASS_GAP_S = 20.0

PRINTABLE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,40}\Z")
MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}\Z")
HEADER_VALUE_RE = re.compile(r"^[ -~]{1,80}\Z")
HEADER_NAME_RE = re.compile(r"^[a-z0-9-]{1,50}\Z")
ENUM_RE = re.compile(r"^[A-Za-z0-9_.-]+\Z")
TIME_KEY_RE = re.compile(r"time|at|date|created|ts|stamp|^t$", re.IGNORECASE)
ISO_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}\Z")

UNTRUSTED_KEYS = {
    "name", "symbol", "url", "icon", "header", "description", "label",
    "handle", "imageurl", "websites", "socials", "links", "slug", "text",
    "title", "type", "platform",
}  # fmt: skip
COUNT_ONLY_UNTRUSTED = {"type", "platform"}
OMIT_SEGMENTS = {"info", "links", "socials", "websites", "icon", "labels"}

ALLOWED_HEADER_NAMES = {
    "content-type", "cache-control", "age", "retry-after", "date", "server",
    "content-length", "cf-cache-status", "cf-mitigated",
}  # fmt: skip
RATE_PREFIXES = ("x-ratelimit", "x-rate-limit", "ratelimit")
DIGEST_MAX_LINES = 350
HARD_REASONS = {"401", "403", "429", "challenge", "html"}
PATH_ADDR_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")


def redact_path(path: str) -> str:
    """Replace any base58-pubkey-looking segment of a request path with a marker.

    Endpoint paths are our own request targets (mint/pair addresses), never
    response text, but they must still never be echoed verbatim into the
    summary or digest.
    """
    return PATH_ADDR_RE.sub("<addr>", path)


# --------------------------------------------------------------------------
# Generic helpers
# --------------------------------------------------------------------------


def type_name(value: object) -> str:
    """Map a parsed JSON value to str/int/float/bool/null/list/dict."""
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


def key_ok(key: str) -> bool:
    """Whether a dict key may be printed."""
    return bool(PRINTABLE_KEY_RE.match(key)) and not MINT_RE.match(key)


def dict_is_printable(value: dict[str, Any]) -> bool:
    """All keys identifier-like, none pubkey-like, at most 60 keys."""
    return len(value) <= MAX_DICT_KEYS_PRINTABLE and all(key_ok(k) for k in value)


def safe_key(key: str) -> str:
    """Return the key if printable, else a redaction marker."""
    return key if key_ok(key) else "<redacted-key>"


def sig(x: float) -> float:
    """Round to 6 significant digits."""
    return float(f"{x:.6g}")


def stats3(values: list[float]) -> list[float]:
    """[min, median, max] of a non-empty list."""
    return [sig(min(values)), sig(statistics.median(values)), sig(max(values))]


def is_num(value: object) -> bool:
    """True for finite int/float that is not a bool."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def to_epoch(value: object) -> tuple[float, str] | None:
    """Interpret a value as a timestamp: (epoch seconds, kind) or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            return None
        if 1e12 < value < 1e14:
            return value / 1000.0, "epoch_ms"
        if 1e9 <= value < 1e11:
            return float(value), "epoch_s"
        return None
    if isinstance(value, str) and re.fullmatch(r"[0-9]{10,13}", value):
        return to_epoch(int(value))
    if isinstance(value, str) and len(value) <= 40 and ISO_PREFIX_RE.match(value):
        try:
            dt = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp(), "iso8601"
    return None


def find_items(data: Any) -> list[dict[str, Any]]:
    """Largest list of dicts found breadth-first within the first 3 levels."""
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


def get_path(item: dict[str, Any], path: str) -> Any:
    """Follow a dotted path through nested dicts; None if absent."""
    node: Any = item
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def valid_mint(value: Any) -> bool:
    """Whether a value is a syntactically valid base58 mint/pair address."""
    return isinstance(value, str) and bool(MINT_RE.match(value))


# --------------------------------------------------------------------------
# Shape walker
# --------------------------------------------------------------------------


@dataclass
class Shape:
    """Accumulates path -> type/number/bool/string/time stats over documents."""

    max_depth: int = MAX_WALK_DEPTH
    recv_s: float | None = None
    docs: int = 0
    num_overflow: int = 0
    types: dict[str, set[str]] = field(default_factory=dict)
    nulls: Counter[str] = field(default_factory=Counter)
    arrays: dict[str, list[int]] = field(default_factory=dict)
    dynamic: dict[str, dict[str, Any]] = field(default_factory=dict)
    nums: dict[str, list[float]] = field(default_factory=dict)
    bools: dict[str, list[int]] = field(default_factory=dict)
    strlen: dict[str, list[int]] = field(default_factory=dict)
    enums: dict[str, Counter[str]] = field(default_factory=dict)
    not_enum: set[str] = field(default_factory=set)
    count_only: set[str] = field(default_factory=set)
    pubkeys: dict[str, set[str]] = field(default_factory=dict)
    quote_addrs: dict[str, Counter[str]] = field(default_factory=dict)
    times: dict[str, list[float]] = field(default_factory=dict)
    lags: dict[str, list[float]] = field(default_factory=dict)
    kinds: dict[str, str] = field(default_factory=dict)

    def add(self, value: Any) -> None:
        """Fold one document (an item or a whole body) into the walk."""
        self.docs += 1
        self._fold(value, "", 0)

    def _fold(self, value: Any, path: str, depth: int) -> None:
        if isinstance(value, dict):
            if not dict_is_printable(value):
                entry = self.dynamic.setdefault(
                    path or "$", {"n": 0, "types": set(), "bad": 0}
                )
                entry["n"] = max(entry["n"], len(value))
                entry["types"].update(type_name(v) for v in value.values())
                entry["bad"] = max(entry["bad"], sum(1 for k in value if not key_ok(k)))
                return
            for key, child in value.items():
                self._visit(f"{path}.{key}" if path else key, child, depth + 1)
        elif isinstance(value, list):
            self.arrays.setdefault(path or "$", []).append(len(value))
            for child in value:
                self._visit(f"{path}[]", child, depth + 1)

    def _visit(self, path: str, value: Any, depth: int) -> None:
        self.types.setdefault(path, set()).add(type_name(value))
        if value is None:
            self.nulls[path] += 1
            return
        if isinstance(value, (dict, list)):
            if depth < self.max_depth:
                self._fold(value, path, depth)
            return
        last = path.rsplit(".", 1)[-1].replace("[]", "")
        if isinstance(value, bool):
            self.bools.setdefault(path, [0, 0])[0 if value else 1] += 1
        elif is_num(value):
            if path in self.nums or len(self.nums) < MAX_NUMERIC_PATHS:
                self.nums.setdefault(path, []).append(float(value))
            else:
                self.num_overflow += 1
        elif isinstance(value, str):
            self._visit_str(path, last, value)
        if self.recv_s is not None and TIME_KEY_RE.search(last):
            parsed = to_epoch(value)
            if parsed is not None:
                self.times.setdefault(path, []).append(parsed[0])
                self.lags.setdefault(path, []).append(self.recv_s - parsed[0])
                self.kinds.setdefault(path, parsed[1])

    def _visit_str(self, path: str, last: str, value: str) -> None:
        self.strlen.setdefault(path, []).append(len(value))
        if MINT_RE.match(value):
            if path.endswith("quoteToken.address"):
                counter = self.quote_addrs.setdefault(path, Counter())
                counter[value] += 1
                if len(counter) > MAX_QUOTE_ADDR_PRINTABLE:
                    # still counted, just not printed individually once over cap
                    pass
                return
            seen = self.pubkeys.setdefault(path, set())
            if len(seen) < MAX_PUBKEYS_TRACKED:
                seen.add(value)
            return
        segments = {s.replace("[]", "").lower() for s in path.split(".")}
        untrusted = segments & UNTRUSTED_KEYS
        if untrusted:
            if untrusted & COUNT_ONLY_UNTRUSTED:
                counter = self.enums.setdefault(path, Counter())
                counter[value] += 1
                self.count_only.add(path)
            else:
                self.not_enum.add(path)
            return
        if path in self.not_enum:
            return
        if len(value) <= 24 and ENUM_RE.match(value):
            counter = self.enums.setdefault(path, Counter())
            counter[value] += 1
            if len(counter) > MAX_ENUM_VALUES and path not in self.count_only:
                self.not_enum.add(path)
                self.enums.pop(path, None)
        else:
            self.not_enum.add(path)
            self.enums.pop(path, None)

    def render(self) -> dict[str, Any]:
        """Render into a JSON-serializable, printable-only structure."""
        fields: dict[str, Any] = {}
        for path in sorted(self.types):
            entry: dict[str, Any] = {"types": "|".join(sorted(self.types[path]))}
            if self.nulls.get(path):
                entry["nulls"] = self.nulls[path]
            if path in self.nums:
                entry["num_min_med_max"] = stats3(self.nums[path])
            if path in self.bools:
                entry["bool_true_false"] = self.bools[path]
            if path in self.strlen:
                entry["str_len_min_max"] = [min(self.strlen[path]), max(self.strlen[path])]
            if path in self.count_only and path in self.enums:
                entry["enum_distinct"] = len(self.enums[path])
            elif self.enums.get(path) and path not in self.not_enum:
                entry["enum"] = dict(self.enums[path])
            if path in self.pubkeys:
                entry["pubkeys_distinct"] = len(self.pubkeys[path])
            if path in self.quote_addrs:
                qa = self.quote_addrs[path]
                if len(qa) <= MAX_QUOTE_ADDR_PRINTABLE:
                    entry["quote_token_address"] = dict(qa)
                else:
                    entry["quote_token_address_distinct"] = len(qa)
            if path in self.times:
                times = self.times[path]
                entry["time"] = {
                    "kind": self.kinds[path],
                    "n": len(times),
                    "lag_s_min_med_max": stats3(self.lags[path]),
                    "span_h": sig((max(times) - min(times)) / 3600.0),
                }
            fields[path] = entry
        return {
            "docs": self.docs,
            "fields": fields,
            "arrays": {
                p: [min(v), statistics.median(v), max(v)] for p, v in self.arrays.items()
            },
            "dynamic": {
                p: {
                    "keys": f"<dynamic-keys:{d['n']}>",
                    "value_types": sorted(d["types"]),
                    "noncompliant_keys": d["bad"],
                }
                for p, d in self.dynamic.items()
            },
            "numeric_paths_over_cap": self.num_overflow,
        }


# --------------------------------------------------------------------------
# Clock jump detection
# --------------------------------------------------------------------------


@dataclass
class ClockTracker:
    """Detects wall/monotonic divergence across consecutive requests."""

    prev_wall_ns: int | None = None
    prev_mono_ns: int | None = None
    jumps: list[dict[str, Any]] = field(default_factory=list)
    request_index: int = 0
    suspect: bool = False
    appear_suspect: bool = False
    jump_spans: list[tuple[int, int]] = field(default_factory=list)  # (mono_ns before, after)

    def observe(self, wall_ns: int, mono_ns: int, phase: str) -> None:
        """Record this request's timestamps and detect a jump since the last one."""
        self.request_index += 1
        if self.prev_wall_ns is not None and self.prev_mono_ns is not None:
            delta_wall = (wall_ns - self.prev_wall_ns) / 1e9
            delta_mono = (mono_ns - self.prev_mono_ns) / 1e9
            jump_s = delta_wall - delta_mono
            if abs(jump_s) > CLOCK_JUMP_THRESHOLD_S:
                self.jumps.append({"request_index": self.request_index, "jump_s": round(jump_s, 3)})
                self.jump_spans.append((self.prev_mono_ns, mono_ns))
                if abs(jump_s) > CLOCK_JUMP_SUSPECT_S:
                    self.suspect = True
                    if phase == "appear":
                        self.appear_suspect = True
        self.prev_wall_ns = wall_ns
        self.prev_mono_ns = mono_ns

    def spans_jump(self, mono_a_ns: int, mono_b_ns: int) -> bool:
        """Whether the interval [mono_a_ns, mono_b_ns] (either order) crosses a recorded jump."""
        lo, hi = (mono_a_ns, mono_b_ns) if mono_a_ns <= mono_b_ns else (mono_b_ns, mono_a_ns)
        return any(lo <= a and b <= hi for a, b in self.jump_spans)


# --------------------------------------------------------------------------
# Sleep inhibition (Windows)
# --------------------------------------------------------------------------

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def sleep_inhibit_start(disabled: bool) -> str:
    """Start sleep inhibition if applicable; return the mode string."""
    if disabled:
        return "disabled"
    if sys.platform != "win32":
        return "not_available"
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    except (AttributeError, OSError):
        return "not_available"
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
# Request layer
# --------------------------------------------------------------------------


@dataclass
class ReqResult:
    """Result of a single HTTP request."""

    status: int | None
    headers: dict[str, str]
    body_text: str
    latency_ms: float
    wall_ns: int
    mono_ns: int
    size: int = 0
    error: str | None = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: a 3xx is recorded as-is."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        return None


OPENER = urllib.request.build_opener(_NoRedirect)


def http_get(url: str, extra: dict[str, str] | None = None) -> ReqResult:
    """Perform a single, credential-free GET and capture its outcome."""
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "identity",
            **(extra or {}),
        },
    )
    start = time.perf_counter()
    try:
        with OPENER.open(req, timeout=REQUEST_TIMEOUT_S) as resp:
            body = resp.read(READ_LIMIT_BYTES)
            status = resp.status
            headers = {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        body = exc.read(READ_LIMIT_BYTES)
        status = exc.code
        headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
    except Exception as exc:  # noqa: BLE001 - must record, never crash a phase
        return ReqResult(
            None, {}, "", (time.perf_counter() - start) * 1000,
            time.time_ns(), time.perf_counter_ns(), 0, type(exc).__name__,
        )  # fmt: skip
    return ReqResult(
        status, headers, body.decode("utf-8", errors="replace"),
        (time.perf_counter() - start) * 1000, time.time_ns(),
        time.perf_counter_ns(), len(body),
    )  # fmt: skip


def allowed_headers(headers: dict[str, str]) -> dict[str, str]:
    """Headers whose VALUE may be printed (and whose value is safe)."""
    out: dict[str, str] = {}
    for name, value in headers.items():
        lname = name.lower()
        if (lname in ALLOWED_HEADER_NAMES or lname.startswith(RATE_PREFIXES)) and (
            HEADER_VALUE_RE.match(value or "")
        ):
            out[lname] = value
    return out


def limit_headers(headers: dict[str, str]) -> dict[str, str]:
    """Rate-limit related allowed headers for the limits sequence."""
    return {
        k: v
        for k, v in allowed_headers(headers).items()
        if k.startswith(RATE_PREFIXES) or k in ("retry-after", "cf-cache-status")
    }


def block_reason(res: ReqResult) -> str | None:
    """Why a response means 'stop this host', or None."""
    status = res.status
    if status in (401, 403, 429) or (status is not None and status >= 500):
        return str(status)
    if "cf-mitigated" in res.headers:
        return "challenge"
    ctype = res.headers.get("content-type", "").lower()
    if "html" in ctype or res.body_text.lstrip()[:1] == "<":
        return "html"
    return None


@dataclass
class Resp:
    """A response with parsed JSON (None if unparseable)."""

    res: ReqResult
    data: Any
    recv_s: float

    @property
    def status(self) -> int | None:
        """HTTP status."""
        return self.res.status

    @property
    def ok(self) -> bool:
        """200 with parseable JSON."""
        return self.res.status == 200 and self.data is not None


@dataclass
class HostStats:
    """Per-host aggregate request stats."""

    status_counts: Counter[str] = field(default_factory=Counter)
    latencies_ms: list[float] = field(default_factory=list)
    header_names: set[str] = field(default_factory=set)
    header_names_nonconforming: int = 0
    header_values: dict[str, str] = field(default_factory=dict)

    def render(self) -> dict[str, Any]:
        """Render for the summary."""
        return {
            "status_counts": dict(self.status_counts),
            "latency_ms_min_med_max": stats3(self.latencies_ms) if self.latencies_ms else None,
            "header_names": sorted(self.header_names),
            "header_names_nonconforming": self.header_names_nonconforming,
            "allowed_header_values": dict(sorted(self.header_values.items())),
        }


class CaptureWriter:
    """Appends one JSON line per request, flushing often."""

    def __init__(self, path: Path) -> None:
        self._fh = path.open("a", encoding="utf-8")
        self._since_flush = 0

    def write(self, phase: str, host: str, url: str, res: ReqResult) -> None:
        """Append one capture line."""
        line = {
            "phase": phase,
            "host": host,
            "url": url,
            "wall_ns": res.wall_ns,
            "mono_ns": res.mono_ns,
            "status": res.status,
            "latency_ms": res.latency_ms,
            "size": res.size,
            "headers": res.headers,
            "error": res.error,
            "body": res.body_text[:CAPTURE_TRUNCATE],
        }
        self._fh.write(json.dumps(line) + "\n")
        self._since_flush += 1
        if self._since_flush >= CAPTURE_FLUSH_EVERY:
            self._fh.flush()
            self._since_flush = 0

    def close(self) -> None:
        """Flush and close."""
        self._fh.flush()
        self._fh.close()


@dataclass
class EndpointRec:
    """Per (host, path) outcome record."""

    statuses: Counter[str] = field(default_factory=Counter)
    latencies_ms: list[float] = field(default_factory=list)
    items: list[int] = field(default_factory=list)
    failed: bool = False
    msg: str | None = None

    def render(self) -> dict[str, Any]:
        """Render for the summary."""
        return {
            "failed": self.failed,
            "status_codes": dict(self.statuses),
            "message": self.msg,
            "items_min_max": [min(self.items), max(self.items)] if self.items else None,
            "latency_ms_min_med_max": stats3(self.latencies_ms) if self.latencies_ms else None,
        }


ERR_MSG_RE = re.compile(r"[ -~]+")


def safe_msg(data: Any) -> str | None:
    """An API-provided error message, only if short, plain ASCII and URL-free."""
    if not isinstance(data, dict):
        return None
    for key in ("msg", "message", "error"):
        value = data.get(key)
        if (
            isinstance(value, str)
            and 0 < len(value) <= 120
            and ERR_MSG_RE.fullmatch(value)
            and "http" not in value.lower()
            and "://" not in value
        ):
            return value
    return None


# --------------------------------------------------------------------------
# Pair-specific helpers
# --------------------------------------------------------------------------


def pair_age_hours(pair: dict[str, Any], recv_s: float) -> float | None:
    """Age of pairCreatedAt (epoch ms) in hours, if present and parseable."""
    parsed = to_epoch(pair.get("pairCreatedAt"))
    if parsed is None:
        return None
    return sig((recv_s - parsed[0]) / 3600.0)


def is_solana_pair(pair: dict[str, Any]) -> bool:
    """Whether a pair's chainId is solana and pairAddress looks valid."""
    return pair.get("chainId") == "solana" and valid_mint(pair.get("pairAddress"))


def txns_sum(pair: dict[str, Any], window: str) -> float | None:
    """buys + sells for txns.<window>, if both are numbers."""
    buys = get_path(pair, f"txns.{window}.buys")
    sells = get_path(pair, f"txns.{window}.sells")
    if is_num(buys) and is_num(sells):
        return float(buys) + float(sells)
    return None


def pair_identity(item: dict[str, Any], mint: str) -> str | None:
    """Which side of a pair matches a mint, if any."""
    if get_path(item, "baseToken.address") == mint:
        return "base"
    if get_path(item, "quoteToken.address") == mint:
        return "quote"
    return None


# --------------------------------------------------------------------------
# Probe
# --------------------------------------------------------------------------


@dataclass
class ActivePair:
    """A pair tracked for freshness / appearance follow-up."""

    pair_address: str
    base_address: str | None
    txns_m5: float


class Probe:
    """Runs the five phases, owns request pacing, caps and per-host blocking."""

    def __init__(
        self,
        args: argparse.Namespace,
        bases: dict[str, str],
        capture: CaptureWriter,
        clock: ClockTracker,
    ) -> None:
        self.args = args
        self.bases = bases
        self.capture = capture
        self.clock = clock
        self.total = 0
        self.cap_hit = False
        self.last_done: float | None = None
        self.blocked: dict[str, dict[str, Any]] = {}
        self.unreachable: set[str] = set()
        self.limits_blocked: set[str] = set()
        self.ok_json: set[str] = set()
        self.hstats: dict[str, HostStats] = {h: HostStats() for h in HOSTS}
        self.ep: dict[tuple[str, str], EndpointRec] = {}
        self.fail5xx: dict[str, set[str]] = {}
        self.reports: dict[str, Any] = {}
        self.skips: list[str] = []
        self.digest_shapes: list[tuple[str, dict[str, Any]]] = []
        self.active_pairs: list[ActivePair] = []
        self.appear_pairs: list[ActivePair] = []
        self.limits_outcome: str | None = None
        self.limits_ran_classes = 0
        self.current_phase = ""

    # ---- request layer ----

    def is_down(self, host: str) -> bool:
        """Whether this host must not receive further requests."""
        return host in self.blocked or host in self.unreachable

    def pace(self, min_gap: float = MIN_SPACING_S) -> None:
        """Sleep until min_gap seconds passed since the last request completed."""
        if self.last_done is not None:
            remaining = min_gap - (time.monotonic() - self.last_done)
            if remaining > 0:
                time.sleep(remaining)

    @staticmethod
    def sleep_until(target_mono: float) -> None:
        """Sleep until a monotonic timestamp."""
        remaining = target_mono - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

    def url(self, host: str, path: str, params: dict[str, str] | None) -> str:
        """Build a request URL from a base, a path and validated params."""
        query = urllib.parse.urlencode(params, safe=",") if params else ""
        return f"{self.bases[host].rstrip('/')}{path}" + (f"?{query}" if query else "")

    def _block(self, host: str, phase: str, res: ReqResult, reason: str) -> None:
        if phase == "limits":
            # The two endpoint classes share one host but have independent rate
            # budgets: a 429/403 on one class must stop only that class, not
            # take the whole host down for the other class or earlier results.
            self.limits_blocked.add(host)
            return
        if host in self.blocked:
            return
        self.blocked[host] = {
            "phase": phase,
            "status": res.status,
            "reason": reason,
            "headers": allowed_headers(res.headers),
        }

    def record(self, host: str, phase: str, path: str, url: str, res: ReqResult) -> str | None:
        """Fold one result into stats/capture; apply the stop rules and clock-jump check."""
        self.total += 1
        self.last_done = time.monotonic()
        if res.status is not None:
            self.clock.observe(res.wall_ns, res.mono_ns, phase)
        stats = self.hstats[host]
        stats.status_counts[str(res.status) if res.status is not None else f"error:{res.error}"] += 1
        stats.latencies_ms.append(res.latency_ms)
        for name in res.headers:
            if HEADER_NAME_RE.match(name):
                stats.header_names.add(name)
            else:
                stats.header_names_nonconforming += 1
        stats.header_values.update(allowed_headers(res.headers))
        self.capture.write(phase, host, url, res)
        rec = self.ep.setdefault((host, path), EndpointRec())
        rec.statuses[str(res.status) if res.status is not None else "error"] += 1
        rec.latencies_ms.append(res.latency_ms)
        if res.status is None:
            rec.failed = True
            self.unreachable.add(host)
            return "unreachable"
        if res.status not in (200, 304) and res.body_text and len(res.body_text) < 50_000:
            try:
                msg = safe_msg(json.loads(res.body_text))
            except ValueError:
                msg = None
            if msg and rec.msg is None:
                rec.msg = msg
        reason = block_reason(res)
        if reason is None:
            return None
        if reason in HARD_REASONS:
            rec.failed = True
            self._block(host, phase, res, reason)
        else:
            rec.failed = True
            paths = self.fail5xx.setdefault(host, set())
            paths.add(path)
            if len(paths) >= 2:
                self._block(host, phase, res, "5xx_two_endpoints")
        return reason

    def get(
        self,
        host: str,
        phase: str,
        path: str,
        params: dict[str, str] | None = None,
        extra: dict[str, str] | None = None,
    ) -> Resp | None:
        """Paced, capped, recorded GET. None if the host/endpoint is down or the cap hit."""
        if self.is_down(host):
            return None
        known = self.ep.get((host, path))
        if known is not None and known.failed:
            return None
        if self.total >= REQUEST_CAP:
            self.cap_hit = True
            return None
        self.pace()
        url = self.url(host, path, params)
        res = http_get(url, extra)
        self.record(host, phase, path, url, res)
        data: Any = None
        if res.status is not None and res.body_text:
            try:
                data = json.loads(res.body_text)
            except ValueError:
                data = None
        if res.status == 200 and data is not None:
            if host not in self.blocked:
                self.ok_json.add(host)
            self.ep[(host, path)].items.append(len(find_items(data)))
        return Resp(res, data, res.wall_ns / 1e9)

    # ---- analysis ----

    def analyze(self, r: Resp) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Walk a response: items and rendered shape."""
        report: dict[str, Any] = {
            "status": r.status,
            "latency_ms": round(r.res.latency_ms, 1),
            "bytes": r.res.size,
        }
        if not r.ok:
            return [], report
        items = find_items(r.data)
        shape = Shape(recv_s=r.recv_s)
        body = r.data if isinstance(r.data, dict) else None
        for doc in items or ([body] if body else []):
            shape.add(doc)
        report["items"] = len(items)
        report["shape"] = shape.render()
        return items, report

    def pair_list_report(self, items: list[dict[str, Any]], recv_s: float) -> dict[str, Any]:
        """Extra pair-list stats: dexId/labels enums, nulls, liquidity/fdv/mcap, txns, age."""
        solana = [p for p in items if is_solana_pair(p)]
        dex_ids = Counter(p.get("dexId") for p in items if isinstance(p.get("dexId"), str))
        labels: Counter[str] = Counter()
        for p in items:
            for lab in p.get("labels") or []:
                if isinstance(lab, str) and len(lab) <= 24 and ENUM_RE.match(lab):
                    labels[lab] += 1
        info_present = sum(1 for p in items if isinstance(p.get("info"), dict))
        price_null = sum(1 for p in items if p.get("priceUsd") is None)

        def numstats(path: str) -> list[float] | None:
            vals = [float(v) for v in (get_path(p, path) for p in items) if is_num(v)]
            return stats3(vals) if vals else None

        txns_stats: dict[str, Any] = {}
        for window in ("m5", "h1", "h24"):
            buys = [get_path(p, f"txns.{window}.buys") for p in items]
            sells = [get_path(p, f"txns.{window}.sells") for p in items]
            buys_n = [float(v) for v in buys if is_num(v)]
            sells_n = [float(v) for v in sells if is_num(v)]
            if buys_n:
                txns_stats[window] = {"buys": stats3(buys_n), "sells": stats3(sells_n) if sells_n else None}
        ages = [a for a in (pair_age_hours(p, recv_s) for p in items) if a is not None]
        quote_addrs = Counter(
            get_path(p, "quoteToken.address")
            for p in items
            if valid_mint(get_path(p, "quoteToken.address"))
        )
        return {
            "items": len(items),
            "solana_items": len(solana),
            "dexId_counts": dict(dex_ids) if len(dex_ids) <= MAX_ENUM_VALUES else {"distinct": len(dex_ids)},
            "labels_counts": dict(labels) if len(labels) <= MAX_ENUM_VALUES else {"distinct": len(labels)},
            "info_present_share": sig(info_present / len(items)) if items else None,
            "priceUsd_null_count": price_null,
            "liquidity_usd_min_med_max": numstats("liquidity.usd"),
            "marketCap_min_med_max": numstats("marketCap"),
            "fdv_min_med_max": numstats("fdv"),
            "txns": txns_stats,
            "pairCreatedAt_age_h_min_med_max": stats3(ages) if ages else None,
            "quoteToken_address_counts": dict(quote_addrs)
            if len(quote_addrs) <= MAX_QUOTE_ADDR_PRINTABLE
            else {"distinct": len(quote_addrs)},
        }

    # ---- phase: shapes ----

    def phase_shapes(self) -> None:
        """Shape-survey every documented keyless DexScreener endpoint."""
        host = "dexscreener"
        rep: dict[str, Any] = {"endpoints": {}}
        self.reports["shapes"] = rep
        eps = rep["endpoints"]

        r = self.get(host, "shapes", "/latest/dex/search", {"q": "SOL"})
        search_items: list[dict[str, Any]] = []
        if r is not None:
            search_items, report = self.analyze(r)
            if r.ok:
                report["pairs"] = self.pair_list_report(search_items, r.recv_s)
                self.digest_shapes.append(("search pair item", report["shape"]))
            eps["search"] = report

        r = self.get(host, "shapes", f"/token-pairs/v1/solana/{SOL_MINT}")
        if r is not None:
            items, report = self.analyze(r)
            if r.ok:
                report["pairs"] = self.pair_list_report(items, r.recv_s)
            eps["token_pairs"] = report

        r = self.get(host, "shapes", f"/tokens/v1/solana/{SOL_MINT},{USDC_MINT}")
        if r is not None:
            items, report = self.analyze(r)
            if r.ok:
                report["pairs"] = self.pair_list_report(items, r.recv_s)
            eps["tokens_v1"] = report

        solana_pairs = [p for p in search_items if is_solana_pair(p)]
        if solana_pairs:
            first = solana_pairs[0]
            pair_addr = first["pairAddress"]
            r = self.get(host, "shapes", f"/latest/dex/pairs/solana/{pair_addr}")
            if r is not None:
                items, report = self.analyze(r)
                eps["pairs_single"] = report
            base_addr = get_path(first, "baseToken.address")
            if valid_mint(base_addr):
                r = self.get(host, "shapes", f"/orders/v1/solana/{base_addr}")
                if r is not None:
                    _, report = self.analyze(r)
                    eps["orders"] = report
            ranked = sorted(
                solana_pairs,
                key=lambda p: (txns_sum(p, "m5") or 0.0),
                reverse=True,
            )
            for p in ranked[:3]:
                self.active_pairs.append(
                    ActivePair(
                        p["pairAddress"],
                        get_path(p, "baseToken.address")
                        if valid_mint(get_path(p, "baseToken.address"))
                        else None,
                        txns_sum(p, "m5") or 0.0,
                    )
                )

        for name, path in (
            ("profiles_latest", "/token-profiles/latest/v1"),
            ("profiles_recent", "/token-profiles/recent-updates/v1"),
            ("boosts_latest", "/token-boosts/latest/v1"),
            ("boosts_top", "/token-boosts/top/v1"),
            ("takeovers_latest", "/community-takeovers/latest/v1"),
            ("ads_latest", "/ads/latest/v1"),
            ("metas_trending", "/metas/trending/v1"),
        ):
            r = self.get(host, "shapes", path)
            if r is None:
                continue
            items, report = self.analyze(r)
            eps[name] = report
            if name == "metas_trending" and r.ok:
                for item in items:
                    slug = item.get("slug")
                    if isinstance(slug, str) and SLUG_RE.match(slug):
                        r2 = self.get(host, "shapes", f"/metas/meta/v1/{slug}")
                        if r2 is not None:
                            _, report2 = self.analyze(r2)
                            eps["metas_meta"] = report2
                        break

    # ---- phase: profiles ----

    def phase_profiles(self) -> None:
        """Poll profiles then boosts, tracking new Solana items per minute."""
        host = "dexscreener"
        rep: dict[str, Any] = {"polls": []}
        self.reports["profiles"] = rep
        seen_profiles: set[tuple[str, str]] = set()
        seen_boosts: set[tuple[str, str]] = set()
        new_profile_times: list[float] = []
        new_boost_times: list[float] = []
        amount_shape = Shape()
        distinct_token_addrs: set[str] = set()
        start = time.monotonic()
        for i in range(self.args.profile_polls):
            if i > 0:
                self.sleep_until(start + i * self.args.profile_interval)
            if self.is_down(host):
                break
            r1 = self.get(host, "profiles", "/token-profiles/latest/v1")
            time.sleep(0.5)
            r2 = self.get(host, "profiles", "/token-boosts/latest/v1") if not self.is_down(host) else None
            poll_entry: dict[str, Any] = {}
            for label, r, seen, new_times in (
                ("profiles", r1, seen_profiles, new_profile_times),
                ("boosts", r2, seen_boosts, new_boost_times),
            ):
                if r is None:
                    poll_entry[label] = {"status": None}
                    continue
                items = find_items(r.data) if r.ok else []
                ids = {
                    (it.get("chainId"), it.get("tokenAddress"))
                    for it in items
                    if isinstance(it.get("chainId"), str) and isinstance(it.get("tokenAddress"), str)
                }
                sol_ids = {t for t in ids if t[0] == "solana"}
                new = sol_ids - seen
                overlap = len(ids & seen) if seen else None
                if new and not self.clock.spans_jump(0, 0):
                    new_times.extend([r.recv_s] * len(new))
                seen |= ids
                for t in ids:
                    if t[1]:
                        distinct_token_addrs.add(t[1])
                amount_shape.recv_s = r.recv_s
                for it in items:
                    amount_shape.add(it)
                poll_entry[label] = {
                    "status": r.status,
                    "items": len(items),
                    "solana_items": len(sol_ids),
                    "new_solana_items": len(new),
                    "overlap_with_seen": overlap,
                    "age": allowed_headers(r.res.headers).get("age"),
                }
            rep["polls"].append(poll_entry)

        def rate_per_min(times: list[float]) -> float | None:
            if len(times) < 2:
                return None
            span = max(times) - min(times)
            return sig((len(times) - 1) / (span / 60.0)) if span > 0 else None

        amount_render = amount_shape.render()
        numeric_amount_fields = {
            p: e["num_min_med_max"]
            for p, e in amount_render["fields"].items()
            if "num_min_med_max" in e and "amount" in p.lower()
        }
        rep["new_profiles_per_min"] = rate_per_min(new_profile_times)
        rep["new_boosts_per_min"] = rate_per_min(new_boost_times)
        rep["boost_amount_numeric_fields"] = numeric_amount_fields
        rep["distinct_tokenAddress_count"] = len(distinct_token_addrs)

    # ---- phase: appear ----

    def phase_appear(self) -> None:
        """Appearance-lag: RugCheck new mints -> DexScreener /tokens/v1 polling."""
        rep: dict[str, Any] = {}
        self.reports["appear"] = rep
        host = "rugcheck"
        tracked: dict[str, float] = {}
        for _ in range(3):
            if len(tracked) >= self.args.appear_mints or self.is_down(host):
                break
            r = self.get(host, "appear", "/v1/stats/new_tokens")
            if r is None or not r.ok:
                continue
            items = find_items(r.data)
            for item in items:
                mint = item.get("mint")
                created = item.get("createAt")
                if not valid_mint(mint) or mint in tracked:
                    continue
                parsed = to_epoch(created)
                if parsed is None:
                    continue
                tracked[mint] = parsed[0]
                if len(tracked) >= self.args.appear_mints:
                    break
            if len(tracked) < self.args.appear_mints:
                time.sleep(4.0)

        if not tracked:
            self.skips.append("appear_no_rugcheck_mints")
            rep["skipped"] = "no_valid_mints"
            return

        mints = list(tracked.keys())
        first_seen: dict[str, float] = {}
        present_at_first: set[str] = set()
        first_sight: dict[str, dict[str, Any]] = {}
        last_sight: dict[str, dict[str, Any]] = {}
        side_counts: Counter[str] = Counter()
        poll_rows: list[dict[str, Any]] = []
        dexs = "dexscreener"
        start = time.monotonic()
        poll_i = 0
        polls_after_complete = 0
        max_polls = max(1, int(self.args.appear_seconds / self.args.appear_interval) + 1)
        while poll_i < max_polls:
            if poll_i > 0:
                self.sleep_until(start + poll_i * self.args.appear_interval)
            if self.is_down(dexs):
                break
            addrs = ",".join(mints[:30])
            r = self.get(dexs, "appear", f"/tokens/v1/solana/{addrs}")
            poll_i += 1
            if r is None:
                break
            items = find_items(r.data) if r.ok else []
            pairs_per_mint: Counter[str] = Counter()
            for mint in mints:
                matches = [p for p in items if pair_identity(p, mint)]
                pairs_per_mint[mint] = len(matches)
                if not matches:
                    continue
                if mint not in first_seen:
                    first_seen[mint] = r.recv_s
                    if poll_i == 1:
                        present_at_first.add(mint)
                    side = pair_identity(matches[0], mint)
                    if side:
                        side_counts[side] += 1
                    p = matches[0]
                    first_sight[mint] = {
                        "dexId": p.get("dexId") if isinstance(p.get("dexId"), str) else None,
                        "labels": [lab for lab in (p.get("labels") or []) if isinstance(lab, str)][:5],
                        "liquidity_usd": get_path(p, "liquidity.usd") if is_num(get_path(p, "liquidity.usd")) else None,
                        "marketCap": p.get("marketCap") if is_num(p.get("marketCap")) else None,
                        "fdv": p.get("fdv") if is_num(p.get("fdv")) else None,
                        "txns_m5": txns_sum(p, "m5"),
                        "pairCreatedAt_age_h": pair_age_hours(p, r.recv_s),
                        "priceUsd_null": p.get("priceUsd") is None,
                        "info_present": isinstance(p.get("info"), dict),
                    }
                last_sight[mint] = first_sight.get(mint, {}).copy()
                p = matches[0]
                last_sight[mint].update(
                    {
                        "dexId": p.get("dexId") if isinstance(p.get("dexId"), str) else None,
                        "pairs": len(matches),
                    }
                )
            poll_rows.append(
                {
                    "poll": poll_i,
                    "status": r.status,
                    "items": len(items),
                    "appeared_so_far": len(first_seen),
                    "age": allowed_headers(r.res.headers).get("age"),
                    "cache_control": allowed_headers(r.res.headers).get("cache-control"),
                }
            )
            if len(first_seen) >= len(tracked):
                polls_after_complete += 1
                if polls_after_complete >= 3:
                    break

        def ttta(mint: str) -> float | None:
            if mint not in first_seen:
                return None
            return round(first_seen[mint] - tracked[mint], 3)

        upper_bound: list[float] = []
        measured: list[float] = []
        for mint in mints:
            t = ttta(mint)
            if t is None:
                continue
            (upper_bound if mint in present_at_first else measured).append(t)

        pca_minus_create: list[float] = []
        fs_minus_pca: list[float] = []
        for mint in mints:
            fs = first_sight.get(mint)
            if fs is None or fs.get("pairCreatedAt_age_h") is None:
                continue
            pca_epoch = (first_seen[mint]) - fs["pairCreatedAt_age_h"] * 3600.0
            pca_minus_create.append(pca_epoch - tracked[mint])
            fs_minus_pca.append(first_seen[mint] - pca_epoch)

        dex_first = Counter(v.get("dexId") for v in first_sight.values() if v.get("dexId"))
        dex_last = Counter(v.get("dexId") for v in last_sight.values() if v.get("dexId"))
        grown_or_changed = sum(
            1
            for m in mints
            if m in first_sight
            and m in last_sight
            and (
                last_sight[m].get("pairs", 1) > 1
                or last_sight[m].get("dexId") != first_sight[m].get("dexId")
            )
        )
        max_pairs = max((v.get("pairs", 1) for v in last_sight.values()), default=0)

        rep.update(
            {
                "tracked": len(tracked),
                "appeared": len(first_seen),
                "already_present_at_first_poll": len(present_at_first),
                "time_to_appear_s_measured_min_med_max": stats3(measured) if measured else None,
                "time_to_appear_s_upper_bound_min_med_max": stats3(upper_bound) if upper_bound else None,
                "pairCreatedAt_minus_createAt_s_min_med_max": stats3(pca_minus_create)
                if pca_minus_create
                else None,
                "first_seen_minus_pairCreatedAt_s_min_med_max": stats3(fs_minus_pca)
                if fs_minus_pca
                else None,
                "dexId_at_first_sight": dict(dex_first),
                "dexId_at_last_sight": dict(dex_last),
                "mints_pair_count_grew_or_dexId_changed": grown_or_changed,
                "max_pairs_per_mint": max_pairs,
                "side_at_first_sight": dict(side_counts),
                "polls": poll_rows,
                "appear_suspect": self.clock.appear_suspect,
            }
        )
        self.appear_pairs = [
            ActivePair(
                None,  # pair address not tracked here; identified by mint for pairfresh join
                mint,
                first_sight[mint].get("txns_m5") or 0.0,
            )
            for mint in sorted(first_sight, key=lambda m: first_sight[m].get("txns_m5") or 0.0, reverse=True)
        ]

    # ---- phase: pairfresh ----

    def phase_pairfresh(self) -> None:
        """Poll active pairs; check freshness and single-vs-list agreement."""
        host = "dexscreener"
        pairs = list(self.active_pairs)
        extra_candidates = [p for p in self.appear_pairs if p.base_address][:2]
        pairs = pairs + extra_candidates
        seen_addrs: set[str] = set()
        deduped: list[ActivePair] = []
        for p in pairs:
            key = p.pair_address or p.base_address or ""
            if key in seen_addrs or not key:
                continue
            seen_addrs.add(key)
            deduped.append(p)
        pairs = deduped[:5]
        if not pairs:
            self.skips.append("pairfresh_no_pairs")
            return
        rep: dict[str, Any] = {"pairs": {}}
        self.reports["pairfresh"] = rep
        labels = {(p.pair_address or p.base_address): f"pair{idx}" for idx, p in enumerate(pairs)}
        series: dict[str, list[dict[str, Any]]] = {labels[k]: [] for k in labels}
        cross_matches = 0
        cross_total = 0
        cross_max_rel_diff = 0.0
        start = time.monotonic()
        for poll in range(self.args.pair_polls):
            if poll > 0:
                self.sleep_until(start + poll * self.args.pair_interval)
            if self.is_down(host):
                break
            for idx, p in enumerate(pairs):
                key = labels[p.pair_address or p.base_address]
                if p.pair_address and valid_mint(p.pair_address):
                    r = self.get(host, "pairfresh", f"/latest/dex/pairs/solana/{p.pair_address}")
                else:
                    r = self.get(host, "pairfresh", f"/token-pairs/v1/solana/{p.base_address}")
                if idx == 0:
                    time.sleep(0.5)
                if r is None:
                    continue
                items = find_items(r.data) if r.ok else []
                match = next((it for it in items if it.get("pairAddress") == p.pair_address), items[0] if items else None)
                row = {
                    "recv_s": r.recv_s,
                    "mono_ns": r.res.mono_ns,
                    "priceUsd": match.get("priceUsd") if match else None,
                    "txns_m5": txns_sum(match, "m5") if match else None,
                    "volume_m5": get_path(match, "volume.m5") if match else None,
                    "liquidity_usd": get_path(match, "liquidity.usd") if match else None,
                    "age": allowed_headers(r.res.headers).get("age"),
                    "cache_control": allowed_headers(r.res.headers).get("cache-control"),
                }
                series[key].append(row)
                if idx == 0 and match is not None and p.pair_address:
                    r2 = self.get(host, "pairfresh", f"/token-pairs/v1/solana/{get_path(match, 'baseToken.address') or ''}")
                    if r2 is not None and r2.ok:
                        tp_items = find_items(r2.data)
                        tp_match = next(
                            (it for it in tp_items if it.get("pairAddress") == p.pair_address), None
                        )
                        if tp_match is not None:
                            cross_total += 1
                            a, b = match.get("priceUsd"), tp_match.get("priceUsd")
                            if isinstance(a, str) and isinstance(b, str):
                                if a == b:
                                    cross_matches += 1
                                else:
                                    try:
                                        fa, fb = float(a), float(b)
                                        if fa != 0:
                                            cross_max_rel_diff = max(
                                                cross_max_rel_diff, abs(fa - fb) / abs(fa)
                                            )
                                    except ValueError:
                                        pass

        for key, rows in series.items():
            out: dict[str, Any] = {}
            for field_name in ("priceUsd", "txns_m5", "volume_m5", "liquidity_usd"):
                vals = [(row["recv_s"], row["mono_ns"], row[field_name]) for row in rows if row[field_name] is not None]
                distinct = len({v[2] for v in vals})
                change_rows: list[tuple[float, int]] = []
                prev = None
                prev_mono = None
                for t, mono_ns, v in vals:
                    if (
                        prev is not None
                        and v != prev
                        and prev_mono is not None
                        and not self.clock.spans_jump(prev_mono, mono_ns)
                    ):
                        change_rows.append((t, mono_ns))
                    prev, prev_mono = v, mono_ns
                change_times = [round(t - rows[0]["recv_s"], 2) for t, _ in change_rows]
                intervals = [
                    b[0] - a[0]
                    for a, b in itertools.pairwise(change_rows)
                    if not self.clock.spans_jump(a[1], b[1])
                ]
                out[field_name] = {
                    "distinct_values": distinct,
                    "polls_changed": len(change_rows),
                    "change_times_s": change_times if field_name == "priceUsd" else None,
                    "median_change_interval_s": round(statistics.median(intervals), 2) if intervals else None,
                }
            out["age_per_poll"] = [row["age"] for row in rows]
            out["cache_control_per_poll"] = sorted({row["cache_control"] for row in rows if row["cache_control"]})
            rep["pairs"][key] = out
        rep["cross_endpoint_agreement"] = {
            "matches": cross_matches,
            "total": cross_total,
            "max_abs_relative_diff": sig(cross_max_rel_diff) if cross_total else None,
        }

    # ---- phase: limits ----

    def phase_limits(self) -> None:
        """Bounded, no-ramp burst probes of the 300/min and 60/min endpoint classes."""
        rep: dict[str, Any] = {}
        self.reports["limits"] = rep
        host = "dexscreener"
        classes = [
            ("300", f"/latest/dex/pairs/solana/{self.active_pairs[0].pair_address}" if self.active_pairs else None, LIMITS_300_BURST, LIMITS_300_OFFSETS_S),
            ("60", "/token-profiles/latest/v1", LIMITS_60_BURST, LIMITS_60_OFFSETS_S),
        ]
        for name, path, burst_n, offsets in classes:
            if path is None:
                rep[name] = {"skipped": "no_active_pair"}
                continue
            if self.is_down(host):
                rep[name] = {"skipped": "host_down"}
                continue
            cap_needed = burst_n + len(offsets)
            if self.total + cap_needed > REQUEST_CAP:
                self.cap_hit = True
                rep[name] = {"skipped": "request_cap"}
                continue
            self.limits_ran_classes += 1
            rep[name] = self.limits_for_class(host, name, path, burst_n, offsets)
            self.pace(LIMITS_CLASS_GAP_S)

    def limits_for_class(
        self, host: str, class_name: str, path: str, burst_n: int, offsets: tuple[int, ...]
    ) -> dict[str, Any]:
        """One endpoint class's burst + offset singles; stops at the first stop-worthy response."""
        url = self.url(host, path, None)
        sequence: list[dict[str, Any]] = []
        results: list[ReqResult | None] = [None] * burst_n
        barrier = threading.Barrier(burst_n)

        def worker(idx: int) -> None:
            barrier.wait()
            results[idx] = http_get(url)

        burst_start = time.perf_counter()
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(burst_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stop: str | None = None
        done = sorted((r for r in results if r is not None), key=lambda r: r.mono_ns)
        for res in done:
            reason = self.record(host, "limits", path, url, res)
            sequence.append(
                {
                    "kind": "burst",
                    "offset_s": round(max(0.0, res.mono_ns / 1e9 - burst_start), 3),
                    "status": res.status,
                    "latency_ms": round(res.latency_ms, 1),
                    "headers": limit_headers(res.headers),
                }
            )
            if reason is not None and stop is None:
                stop = reason
        if stop is None:
            for offset in offsets:
                remaining = burst_start + offset - time.perf_counter()
                if remaining > 0:
                    time.sleep(remaining)
                r = self.get(host, "limits", path)
                if r is None:
                    break
                sequence.append(
                    {
                        "kind": "single",
                        "offset_s": round(time.perf_counter() - burst_start, 3),
                        "status": r.status,
                        "latency_ms": round(r.res.latency_ms, 1),
                        "headers": limit_headers(r.res.headers),
                    }
                )
                reason = block_reason(r.res)
                if reason is not None:
                    stop = reason
                    break
        if stop is not None and self.limits_outcome is None:
            self.limits_outcome = f"429@{class_name}" if stop == "429" else f"blocked@{class_name}"
        return {"sequence": sequence, "stopped": stop}


# --------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------


def fmt3(values: list[Any] | None) -> str:
    """Format a [min, med, max] list as a/b/c."""
    if not values:
        return "none"
    return "/".join(f"{v:g}" if isinstance(v, (int, float)) else str(v) for v in values)


def shape_lines(rendered: dict[str, Any]) -> list[str]:
    """One line per leaf path: 'path | types | stats'."""
    lines: list[str] = []
    omitted: set[str] = set()
    for path, e in rendered.get("fields", {}).items():
        parts = path.split(".")
        idx = next((i for i, s in enumerate(parts) if s.replace("[]", "") in OMIT_SEGMENTS), None)
        keep_enum = "labels" in path.lower() or "dexid" in path.lower() or path.lower().endswith("dexid")
        if idx is not None and not keep_enum:
            prefix = ".".join(parts[: idx + 1])
            if prefix not in omitted:
                omitted.add(prefix)
                lines.append(f"{prefix} | <omitted subtree>")
            continue
        stats: list[str] = []
        if "num_min_med_max" in e:
            stats.append("num=" + fmt3(e["num_min_med_max"]))
        if "bool_true_false" in e:
            stats.append(f"bool_t/f={e['bool_true_false'][0]}/{e['bool_true_false'][1]}")
        if e.get("nulls"):
            stats.append(f"nulls={e['nulls']}")
        if "str_len_min_max" in e:
            stats.append(f"len={e['str_len_min_max'][0]}-{e['str_len_min_max'][1]}")
        if "enum" in e:
            stats.append("enum=" + ",".join(f"{k}:{v}" for k, v in e["enum"].items()))
        if "enum_distinct" in e:
            stats.append(f"enum_distinct={e['enum_distinct']}")
        if "pubkeys_distinct" in e:
            stats.append(f"pubkeys_distinct={e['pubkeys_distinct']}")
        if "quote_token_address" in e:
            stats.append("quoteToken=" + ",".join(f"{k}:{v}" for k, v in e["quote_token_address"].items()))
        if "quote_token_address_distinct" in e:
            stats.append(f"quoteToken_distinct={e['quote_token_address_distinct']}")
        if "time" in e:
            t = e["time"]
            stats.append(f"time={t['kind']} lag_s={fmt3(t['lag_s_min_med_max'])} span_h={t['span_h']:g}")
        if not stats and e["types"] in ("dict", "list", "dict|list"):
            continue
        lines.append(f"{path} | {e['types']} | {' '.join(stats)}")
    for path, d in rendered.get("dynamic", {}).items():
        lines.append(f"{path} | dict | {d['keys']} value_types={','.join(d['value_types'])}")
    return lines


def jline(label: str, value: Any) -> str:
    """'label: compact-json' line."""
    return f"{label}: {json.dumps(value, separators=(',', ':'))}"


def build_digest(probe: Probe, summary: dict[str, Any], verdict: str) -> list[str]:
    """Compact plain-text digest (at most DIGEST_MAX_LINES lines)."""
    rep = probe.reports
    fixed: list[str] = [verdict, f"requests={summary['total_requests']} cap_hit={summary['cap_hit']}"]
    for host, s in summary["per_host"].items():
        fixed.append(
            f"HOST {host} | status {json.dumps(s['status_counts'], separators=(',', ':'))}"
            f" | latency_ms min/med/max {fmt3(s['latency_ms_min_med_max'])}"
        )
        fixed.append(f"HOST {host} | headers: {' '.join(s['header_names'])}")
        fixed.append(
            f"HOST {host} | values: "
            + "; ".join(f"{k}={v}" for k, v in s["allowed_header_values"].items())
        )
    if summary["blocked_hosts"]:
        fixed.append(jline("BLOCKED", summary["blocked_hosts"]))
    for key, rec in summary["endpoints"].items():
        lat = fmt3(rec["latency_ms_min_med_max"])
        fixed.append(
            f"EP {key} | codes {json.dumps(rec['status_codes'], separators=(',', ':'))}"
            f" | failed={rec['failed']} | items {rec['items_min_max']} | latency_ms {lat}"
        )

    shapes_rep = rep.get("shapes", {})
    for name, ep in shapes_rep.get("endpoints", {}).items():
        if "pairs" in ep:
            fixed.append(jline(f"SHAPES {name} pairs", ep["pairs"]))
        elif "shape" in ep:
            fixed.append(f"SHAPES {name} | items={ep.get('items')}")

    pr = rep.get("profiles")
    if pr:
        fixed.append(
            jline(
                "PROFILES",
                {
                    "new_profiles_per_min": pr.get("new_profiles_per_min"),
                    "new_boosts_per_min": pr.get("new_boosts_per_min"),
                    "distinct_tokenAddress_count": pr.get("distinct_tokenAddress_count"),
                },
            )
        )
        fixed.append(jline("PROFILES amounts", pr.get("boost_amount_numeric_fields")))

    ap = rep.get("appear")
    if ap:
        fixed.append(
            jline(
                "APPEAR",
                {k: v for k, v in ap.items() if k != "polls"},
            )
        )

    pf = rep.get("pairfresh")
    if pf:
        for key, d in pf.get("pairs", {}).items():
            fixed.append(jline(f"PAIRFRESH {key}", d))
        fixed.append(jline("PAIRFRESH cross_endpoint_agreement", pf.get("cross_endpoint_agreement")))

    lim = rep.get("limits")
    if lim:
        for name, d in lim.items():
            if "sequence" in d:
                seq = " ".join(f"{s['status']}@{s['offset_s']:g}" for s in d["sequence"])
                extra = {
                    k: v
                    for s in d["sequence"]
                    for k, v in s["headers"].items()
                    if k != "cf-cache-status"
                }
                fixed.append(f"LIMITS {name} | {seq} | stopped={d['stopped']} | ratelimit/retry headers {extra}")
            else:
                fixed.append(jline(f"LIMITS {name}", d))

    if summary.get("clock_jumps"):
        fixed.append(jline("CLOCK_JUMPS", summary["clock_jumps"]))

    fixed.append("ERRORS:")
    for key, rec in summary["endpoints"].items():
        if rec["failed"] or rec["message"] or any(not c.startswith(("2", "3")) for c in rec["status_codes"]):
            fixed.append(f"  {key} | codes {json.dumps(rec['status_codes'], separators=(',', ':'))} | failed={rec['failed']} | msg={rec['message']}")
    if fixed[-1] == "ERRORS:":
        fixed.append("  none")
    if probe.skips:
        fixed.append(jline("SKIPS", probe.skips))

    sections = [(title, shape_lines(shape)) for title, shape in probe.digest_shapes]
    notes: list[str] = []
    budget = DIGEST_MAX_LINES - 1
    while len(fixed) + sum(len(ls) + 1 for _, ls in sections) > budget:
        idx = max(range(len(sections)), key=lambda i: len(sections[i][1]), default=None)
        if idx is None or len(sections[idx][1]) <= 3:
            break
        title, ls = sections[idx]
        keep = max(3, len(ls) // 2)
        notes.append(f"{title}: {len(ls)}->{keep}")
        sections[idx] = (title, [*ls[:keep], "... truncated"])
    out = list(fixed)
    for title, ls in sections:
        out.append(f"SHAPE {title}")
        out.extend(ls)
    if notes:
        out.append("DIGEST TRUNCATED: " + "; ".join(notes))
    return out[:DIGEST_MAX_LINES]


# --------------------------------------------------------------------------
# CLI / orchestration
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse and validate CLI arguments."""

    def phases_type(raw: str) -> list[str]:
        values = [v.strip() for v in raw.split(",") if v.strip()]
        for v in values:
            if v not in PHASE_ORDER:
                raise argparse.ArgumentTypeError(f"unknown phase {v!r}")
        return values

    def bounded_int(name: str, lo: int, hi: int) -> Any:
        def inner(raw: str) -> int:
            value = int(raw)
            if value < lo or value > hi:
                raise argparse.ArgumentTypeError(f"--{name} must be in [{lo}, {hi}]")
            return value

        return inner

    def bounded_float(name: str, lo: float, hi: float) -> Any:
        def inner(raw: str) -> float:
            value = float(raw)
            if value < lo or value > hi:
                raise argparse.ArgumentTypeError(f"--{name} must be in [{lo}, {hi}]")
            return value

        return inner

    def label_type(raw: str) -> str:
        if not re.fullmatch(r"[a-z0-9_-]{1,30}", raw):
            raise argparse.ArgumentTypeError("--label must match [a-z0-9_-]{1,30}")
        return raw

    p = argparse.ArgumentParser(description="Anonymous DexScreener public API probe.")
    p.add_argument("--phases", type=phases_type, default=list(PHASE_ORDER))
    p.add_argument("--base-url", default=DEFAULT_URLS["dexscreener"])
    p.add_argument("--rugcheck-url", default=DEFAULT_URLS["rugcheck"])
    p.add_argument("--appear-mints", type=bounded_int("appear-mints", 1, 20), default=10)
    p.add_argument("--appear-interval", type=bounded_float("appear-interval", 3, 3600), default=6.0)
    p.add_argument("--appear-seconds", type=bounded_int("appear-seconds", 10, 600), default=300)
    p.add_argument("--pair-polls", type=bounded_int("pair-polls", 1, 30), default=12)
    p.add_argument("--pair-interval", type=bounded_float("pair-interval", 2, 3600), default=5.0)
    p.add_argument("--profile-polls", type=bounded_int("profile-polls", 1, 12), default=6)
    p.add_argument("--profile-interval", type=bounded_float("profile-interval", 5, 3600), default=10.0)
    p.add_argument("--no-sleep-inhibit", action="store_true")
    p.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    p.add_argument("--label", type=label_type, default=None)
    return p.parse_args(argv)


def validate_base_url(base_url: str) -> None:
    """Enforce https-only base URLs, except http://127.0.0.1 for self-tests."""
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme == "https" and parsed.hostname:
        return
    if parsed.scheme == "http" and parsed.hostname == "127.0.0.1":
        return
    raise ValueError("base URLs must be https (or http://127.0.0.1 for self-tests)")


def resolve_out_dir(repo_root: Path, raw_out_dir: str) -> Path:
    """Resolve --out-dir and enforce it stays under <repo root>/data/."""
    candidate = Path(raw_out_dir)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    resolved = candidate.resolve()
    data_root = (repo_root / "data").resolve()
    if resolved != data_root and data_root not in resolved.parents:
        raise ValueError(f"--out-dir must resolve under {data_root}")
    return resolved


def host_verdict(probe: Probe, host: str) -> str:
    """true / false / blocked for one host."""
    if host in probe.blocked and host not in probe.limits_blocked:
        return "blocked"
    return "true" if host in probe.ok_json else "false"


def limits_verdict(probe: Probe, phases: list[str]) -> str:
    """ran / skipped / 429@class / blocked@class."""
    if "limits" not in phases or probe.limits_ran_classes == 0:
        return "skipped"
    return probe.limits_outcome or "ran"


def main(argv: list[str] | None = None) -> int:
    """Entry point: run the requested phases and write/print results."""
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    args = parse_args(argv)
    repo_root = Path(__file__).resolve().parent.parent
    bases = {"dexscreener": args.base_url, "rugcheck": args.rugcheck_url}
    try:
        for base in bases.values():
            validate_base_url(base)
        out_dir = resolve_out_dir(repo_root, args.out_dir)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)

    sleep_mode = sleep_inhibit_start(args.no_sleep_inhibit)

    now = datetime.now(UTC)
    stem = f"dexscreener_{now.strftime('%Y%m%dT%H%M%SZ')}" + (f"_{args.label}" if args.label else "")
    capture = CaptureWriter(out_dir / f"{stem}.raw.jsonl")
    summary_path = out_dir / f"{stem}.summary.json"
    digest_path = out_dir / f"{stem}.digest.txt"
    hostnames = {h: urllib.parse.urlsplit(b).hostname for h, b in bases.items()}
    phases = [p for p in PHASE_ORDER if p in args.phases]
    print(
        f"{now.isoformat()} start hosts={json.dumps(hostnames, separators=(',', ':'))} "
        f"phases={','.join(phases)} appear_mints={args.appear_mints} "
        f"appear_interval={args.appear_interval} appear_seconds={args.appear_seconds} "
        f"pair_polls={args.pair_polls} pair_interval={args.pair_interval} "
        f"profile_polls={args.profile_polls} profile_interval={args.profile_interval} "
        f"sleep_inhibit={sleep_mode}"
    )

    clock = ClockTracker()
    probe = Probe(args, bases, capture, clock)
    runners = {
        "shapes": probe.phase_shapes,
        "profiles": probe.phase_profiles,
        "appear": probe.phase_appear,
        "pairfresh": probe.phase_pairfresh,
        "limits": probe.phase_limits,
    }
    interrupted = False
    try:
        for phase in phases:
            probe.current_phase = phase
            runners[phase]()
    except KeyboardInterrupt:
        interrupted = True
    finally:
        capture.close()
        sleep_inhibit_stop(sleep_mode)

    appear_rep = probe.reports.get("appear", {})
    median_appear = None
    for key in ("time_to_appear_s_measured_min_med_max", "time_to_appear_s_upper_bound_min_med_max"):
        if appear_rep.get(key):
            median_appear = appear_rep[key][1]
            break
    appeared = appear_rep.get("appeared", 0)
    tracked_n = appear_rep.get("tracked", 0)

    dex_status = host_verdict(probe, "dexscreener")
    verdict = (
        f"VERDICT dex_ok={dex_status} "
        f"appear={appeared}/{tracked_n} "
        f"median_appear_s={median_appear if median_appear is not None else 'none'} "
        f"pairfresh={'ran' if 'pairfresh' in probe.reports else 'skipped'} "
        f"profiles={'ran' if 'profiles' in probe.reports else 'skipped'} "
        f"limits={limits_verdict(probe, phases)} "
        f"suspect_run={str(clock.suspect).lower()} "
        f"sleep_inhibit={sleep_mode}"
    )
    merged_ep: dict[str, EndpointRec] = {}
    for (h, p), rec in probe.ep.items():
        key = f"{h} {redact_path(p)}"
        agg = merged_ep.setdefault(key, EndpointRec())
        agg.statuses.update(rec.statuses)
        agg.latencies_ms.extend(rec.latencies_ms)
        agg.items.extend(rec.items)
        agg.failed = agg.failed or rec.failed
        agg.msg = agg.msg or rec.msg
    endpoints = {k: v.render() for k, v in sorted(merged_ep.items())}
    summary: dict[str, Any] = {
        "started_utc": now.isoformat(),
        "hostnames": hostnames,
        "phases": phases,
        "interrupted": interrupted,
        "total_requests": probe.total,
        "cap_hit": probe.cap_hit,
        "blocked_hosts": probe.blocked,
        "unreachable_hosts": sorted(probe.unreachable),
        "per_host": {h: s.render() for h, s in probe.hstats.items() if s.status_counts},
        "endpoints": endpoints,
        "errors": {
            k: {"status_codes": v["status_codes"], "message": v["message"]}
            for k, v in endpoints.items()
            if v["failed"] or v["message"]
        },
        "skips": probe.skips,
        "reports": probe.reports,
        "clock_jumps": clock.jumps,
        "suspect_run": clock.suspect,
        "sleep_inhibit": sleep_mode,
        "verdict": verdict,
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    digest = build_digest(probe, summary, verdict)
    digest_path.write_text("\n".join(digest) + "\n", encoding="utf-8")
    print("\n".join(digest))
    print(f"digest: {digest_path} lines={len(digest)}")
    print(verdict)

    if interrupted:
        return 130
    reached = {h for h, s in probe.hstats.items() if any(k.isdigit() for k in s.status_counts)}
    if probe.ok_json:
        return 0
    if reached and all(h in probe.blocked for h in reached):
        return 3
    return 2


if __name__ == "__main__":
    sys.exit(main())
