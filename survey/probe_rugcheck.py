"""One-shot, anonymous probe of RugCheck's public HTTP/SSE API.

Throwaway Phase 1 measurement script. Never imported by src/. Mirrors the
structure and conventions of survey/probe_pumpportal.py and
survey/probe_pumpdev.py so outputs are comparable. Stdlib only. Treats all
token-related response text (names, symbols, descriptions, risk text, free
text of any kind) and dictionary keys as hostile, untrusted data: values are
never printed except narrow, explicitly allow-listed numeric/enum categories,
and keys are only ever printed when they match a strict identifier pattern.
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.client import HTTPConnection, HTTPSConnection
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://api.rugcheck.xyz"
ALL_PHASES = ["basic", "feed", "summary", "report", "analytics", "ticker", "limits"]
DEFAULT_PHASES = ["basic", "feed", "summary", "report", "analytics", "ticker"]
USER_AGENT = "floor-mop-survey/0.1"
REQUEST_TIMEOUT_S = 20.0
CONNECT_TIMEOUT_S = 15.0
MIN_SPACING_S = 0.5
CAPTURE_TRUNCATE = 200_000
HTTP_REQUEST_CAP = 250
DEFAULT_OUT_DIR = "data/survey"
CAPTURE_FLUSH_EVERY = 20

DEFAULT_FEED_POLLS = 5
HARD_MAX_FEED_POLLS = 30
DEFAULT_FEED_INTERVAL = 15.0
MIN_FEED_INTERVAL = 3.0
DEFAULT_SAMPLE_MINTS = 12
HARD_MAX_SAMPLE_MINTS = 25
DEFAULT_SAMPLE_SPACING = 1.0
MIN_SAMPLE_SPACING = 0.5
DEFAULT_REPORT_MINTS = 5
HARD_MAX_REPORT_MINTS = 5
LIMITS_REQUEST_CAP = 10
LIMITS_BURST_SIZE = 5
LIMITS_OFFSETS_S = (1, 2, 4, 8, 16)
DEFAULT_SSE_SECONDS = 0
HARD_MAX_SSE_SECONDS = 600
DEFAULT_RAMP_STEPS = "1,2,4,8"
HARD_MAX_RAMP_STEP_RATE = 10
HARD_MAX_RAMP_STEPS = 6
DEFAULT_RAMP_STEP_SECONDS = 10
HARD_MAX_RAMP_STEP_SECONDS = 15
DEFAULT_RAMP_MAX_REQUESTS = 150
HARD_MAX_RAMP_MAX_REQUESTS = 200
MAX_WALK_DEPTH_SUMMARY = 4
MAX_WALK_DEPTH_REPORT = 5

PRINTABLE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,40}$")
MAX_DICT_KEYS_PRINTABLE = 60
MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
LEVEL_RE = re.compile(r"^[A-Za-z0-9 _-]{1,24}$")
RISK_NAME_RE = re.compile(r"^[A-Za-z0-9 ()%./,'_-]{1,60}$")
PROGRAM_VALUE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,44}$")
HEADER_VALUE_RE = re.compile(r"^[ -~]{1,80}$")
TIME_KEY_RE = re.compile(r"time|at|date", re.IGNORECASE)
TICKER_NUMERIC_KEY_RE = re.compile(r"liquidity|usd|amount|count|score", re.IGNORECASE)
EVENT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,24}$")
INTERESTING_KEY_RE = re.compile(
    r"rug|honeypot|scam|verified|blacklist|mutable|freeze|mintauth|lp|lock|"
    r"insider|creator|holder|liquidity|score|rating|market|event|graph|"
    r"extension|transfer|fee",
    re.IGNORECASE,
)

ALLOWED_HEADER_NAMES = {
    "content-type",
    "cache-control",
    "age",
    "retry-after",
    "date",
    "server",
    "content-length",
    "cf-cache-status",
}


def _header_value_allowed(name_lower: str) -> bool:
    """Return whether this header's VALUE is allowed to be printed."""
    return name_lower in ALLOWED_HEADER_NAMES or name_lower.startswith(
        ("x-ratelimit", "ratelimit", "x-rate-limit")
    )


def _allowed_headers(headers: dict[str, str]) -> dict[str, str]:
    """Filter headers down to printable names and printable, safe values."""
    out: dict[str, str] = {}
    for name, value in headers.items():
        lname = name.lower()
        if _header_value_allowed(lname) and HEADER_VALUE_RE.match(value or ""):
            out[lname] = value
    return out


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


def _dict_is_printable(value: dict[str, Any]) -> bool:
    """A dict's keys are printable iff every key matches the identifier
    pattern, no key looks like a base58 public key, and it has at most
    MAX_DICT_KEYS_PRINTABLE keys. All three must hold or the whole dict
    collapses."""
    if len(value) > MAX_DICT_KEYS_PRINTABLE:
        return False
    for key in value:
        if not PRINTABLE_KEY_RE.match(key) or MINT_RE.match(key):
            return False
    return True


@dataclass
class ShapeWalk:
    """Accumulated path -> type/null/array-length stats across one or more
    JSON documents, with non-printable-key dicts collapsed to a single
    dynamic-keys marker instead of being recursed into."""

    key_types: dict[str, set[str]] = field(default_factory=dict)
    null_counts: dict[str, int] = field(default_factory=dict)
    array_lengths: dict[str, list[int]] = field(default_factory=dict)
    dynamic: dict[str, dict[str, Any]] = field(default_factory=dict)
    noncompliant_dicts: int = 0
    noncompliant_keys: int = 0

    def walk(self, value: Any, path: str, depth: int, max_depth: int) -> None:
        """Recursively fold value into this walk's accumulators.

        A dict collapses entirely into a single dynamic-keys marker unless
        ALL of its keys match the identifier pattern, none look like a
        base58 public key, and it has at most MAX_DICT_KEYS_PRINTABLE keys
        (see _dict_is_printable). A collapsed dict is never recursed into;
        its non-conforming keys are counted but neither their names nor
        their values are ever printed.
        """
        if depth > max_depth:
            return
        if isinstance(value, dict):
            if not _dict_is_printable(value):
                type_union = sorted({_json_type_name(v) for v in value.values()})
                entry = self.dynamic.setdefault(path or "$", {"n": 0, "types": set()})
                entry["n"] = max(entry["n"], len(value))
                entry["types"].update(type_union)
                self.noncompliant_dicts += 1
                self.noncompliant_keys += sum(
                    1
                    for key in value
                    if not PRINTABLE_KEY_RE.match(key) or MINT_RE.match(key)
                )
                return
            for key, v in value.items():
                child = f"{path}.{key}" if path else key
                self.key_types.setdefault(child, set()).add(_json_type_name(v))
                if v is None:
                    self.null_counts[child] = self.null_counts.get(child, 0) + 1
                self.walk(v, child, depth + 1, max_depth)
        elif isinstance(value, list):
            self.array_lengths.setdefault(path or "$", []).append(len(value))
            for item in value:
                child = f"{path}[]"
                self.key_types.setdefault(child, set()).add(_json_type_name(item))
                self.walk(item, child, depth + 1, max_depth)

    def render(self) -> dict[str, Any]:
        """Render into a JSON-serializable, printable-only summary."""
        return {
            "key_types": {p: sorted(t) for p, t in self.key_types.items()},
            "null_counts": dict(self.null_counts),
            "array_lengths": {
                p: {"min": min(v), "median": statistics.median(v), "max": max(v)}
                for p, v in self.array_lengths.items()
            },
            "dynamic_keys": {
                p: f"<dynamic-keys:{d['n']}>" for p, d in self.dynamic.items()
            },
            "dynamic_key_value_types": {
                p: sorted(d["types"]) for p, d in self.dynamic.items()
            },
            "noncompliant_dicts": self.noncompliant_dicts,
            "noncompliant_keys": self.noncompliant_keys,
        }


def _collect_named_numeric(value: Any, name: str, acc: list[float]) -> None:
    """Recursively collect numeric values stored under an exact key name."""
    if isinstance(value, dict):
        for k, v in value.items():
            if k == name and isinstance(v, (int, float)) and not isinstance(v, bool):
                acc.append(float(v))
            _collect_named_numeric(v, name, acc)
    elif isinstance(value, list):
        for item in value:
            _collect_named_numeric(item, name, acc)


def _collect_named_strings(value: Any, name: str, acc: list[str]) -> None:
    """Recursively collect string values stored under an exact key name."""
    if isinstance(value, dict):
        for k, v in value.items():
            if k == name and isinstance(v, str):
                acc.append(v)
            _collect_named_strings(v, name, acc)
    elif isinstance(value, list):
        for item in value:
            _collect_named_strings(item, name, acc)


def _collect_risk_names(value: Any, acc: list[str]) -> None:
    """Recursively collect risks[].name strings."""
    if isinstance(value, dict):
        for k, v in value.items():
            if k == "risks" and isinstance(v, list):
                for risk in v:
                    if isinstance(risk, dict) and isinstance(risk.get("name"), str):
                        acc.append(risk["name"])
            _collect_risk_names(v, acc)
    elif isinstance(value, list):
        for item in value:
            _collect_risk_names(item, acc)


def _collect_numeric_leaves(value: Any, path: str, acc: list[tuple[str, float]]) -> None:
    """Collect numeric leaves with printable-or-redacted key path labels."""
    if isinstance(value, dict):
        for k, v in value.items():
            label = k if PRINTABLE_KEY_RE.match(k) else "<redacted-key>"
            child = f"{path}.{label}" if path else label
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                acc.append((child, float(v)))
            elif isinstance(v, (dict, list)):
                _collect_numeric_leaves(v, child, acc)
    elif isinstance(value, list):
        for item in value:
            _collect_numeric_leaves(item, path, acc)


@dataclass
class InterestingKeyAgg:
    """Aggregated, printable-only stats for one "interesting" key name,
    across every occurrence at any depth in one or more documents."""

    numbers: list[float] = field(default_factory=list)
    bool_true: int = 0
    bool_false: int = 0
    null_count: int = 0
    str_lengths: list[int] = field(default_factory=list)
    list_lengths: list[int] = field(default_factory=list)
    dict_lengths: list[int] = field(default_factory=list)
    dict_key_names: set[str] = field(default_factory=set)


def _collect_interesting_keys(value: Any, acc: dict[str, InterestingKeyAgg]) -> None:
    """Recursively fold every key whose name matches INTERESTING_KEY_RE into
    acc, by type. String VALUES are never collected, only their lengths."""
    if isinstance(value, dict):
        for key, v in value.items():
            if INTERESTING_KEY_RE.search(key):
                agg = acc.setdefault(key, InterestingKeyAgg())
                if isinstance(v, bool):
                    if v:
                        agg.bool_true += 1
                    else:
                        agg.bool_false += 1
                elif isinstance(v, (int, float)):
                    agg.numbers.append(float(v))
                elif v is None:
                    agg.null_count += 1
                elif isinstance(v, str):
                    agg.str_lengths.append(len(v))
                elif isinstance(v, list):
                    agg.list_lengths.append(len(v))
                elif isinstance(v, dict):
                    agg.dict_lengths.append(len(v))
                    if _dict_is_printable(v):
                        agg.dict_key_names.update(v.keys())
            _collect_interesting_keys(v, acc)
    elif isinstance(value, list):
        for item in value:
            _collect_interesting_keys(item, acc)


def _render_interesting_keys(acc: dict[str, InterestingKeyAgg]) -> dict[str, Any]:
    """Render interesting-key aggregates into printable-only summary form."""
    out: dict[str, Any] = {}
    for key, agg in sorted(acc.items()):
        entry: dict[str, Any] = {}
        if agg.numbers:
            entry["numbers"] = {
                "min": min(agg.numbers),
                "median": statistics.median(agg.numbers),
                "max": max(agg.numbers),
            }
        if agg.bool_true or agg.bool_false:
            entry["booleans"] = {"true": agg.bool_true, "false": agg.bool_false}
        if agg.null_count:
            entry["null_count"] = agg.null_count
        if agg.str_lengths:
            entry["string_length"] = {"min": min(agg.str_lengths), "max": max(agg.str_lengths)}
        if agg.list_lengths:
            entry["list_lengths"] = {"min": min(agg.list_lengths), "max": max(agg.list_lengths)}
        if agg.dict_lengths:
            entry["dict_lengths"] = {"min": min(agg.dict_lengths), "max": max(agg.dict_lengths)}
        if agg.dict_key_names:
            entry["dict_key_names"] = sorted(agg.dict_key_names)
        out[key] = entry
    return out


def _parse_iso8601(text: str) -> datetime | None:
    """Best-effort ISO 8601 parse, returning an aware UTC datetime or None."""
    try:
        cleaned = text.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _time_like_lag_s(item: dict[str, Any], receive_dt: datetime) -> float | None:
    """Find a time-like top-level field and return its lag vs receive_dt."""
    for key, value in item.items():
        if not TIME_KEY_RE.search(key) or not isinstance(value, str):
            continue
        parsed = _parse_iso8601(value)
        if parsed is not None:
            return (receive_dt - parsed).total_seconds()
    return None


@dataclass
class ReqResult:
    """Result of a single HTTP request."""

    status: int | None
    headers: dict[str, str]
    body_text: str
    latency_ms: float
    wall_ns: int
    mono_ns: int
    error: str | None = None


def _http_get(url: str, timeout: float = REQUEST_TIMEOUT_S) -> ReqResult:
    """Perform a single, credential-free GET and capture its outcome."""
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "identity",
        },
    )
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            status = resp.status
            headers = {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        body = exc.read()
        status = exc.code
        headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
    except Exception as exc:  # noqa: BLE001 - must record, never crash a phase
        latency_ms = (time.perf_counter() - start) * 1000
        return ReqResult(
            status=None,
            headers={},
            body_text="",
            latency_ms=latency_ms,
            wall_ns=time.time_ns(),
            mono_ns=time.perf_counter_ns(),
            error=type(exc).__name__,
        )
    latency_ms = (time.perf_counter() - start) * 1000
    return ReqResult(
        status=status,
        headers=headers,
        body_text=body.decode("utf-8", errors="replace"),
        latency_ms=latency_ms,
        wall_ns=time.time_ns(),
        mono_ns=time.perf_counter_ns(),
    )


@dataclass
class EndpointStats:
    """Aggregate status/latency stats for one endpoint template."""

    status_counts: dict[str, int] = field(default_factory=dict)
    latencies_ms: list[float] = field(default_factory=list)
    rate_limit_values: set[str] = field(default_factory=set)
    rate_limit_remaining_counts: dict[str, int] = field(default_factory=dict)

    def observe(self, result: ReqResult) -> None:
        """Fold one request result into the aggregate."""
        key = str(result.status) if result.status is not None else f"error:{result.error}"
        self.status_counts[key] = self.status_counts.get(key, 0) + 1
        self.latencies_ms.append(result.latency_ms)
        for name, value in result.headers.items():
            lname = name.lower()
            if not value or not HEADER_VALUE_RE.match(value):
                continue
            if lname == "x-rate-limit-limit":
                self.rate_limit_values.add(value)
            elif lname == "x-rate-limit-remaining":
                self.rate_limit_remaining_counts[value] = (
                    self.rate_limit_remaining_counts.get(value, 0) + 1
                )

    def render(self) -> dict[str, Any]:
        """Render into JSON-serializable summary form."""
        remaining_ints = [
            int(v) for v in self.rate_limit_remaining_counts if v.lstrip("-").isdigit()
        ]
        return {
            "status_counts": dict(self.status_counts),
            "latency_ms": {
                "median": statistics.median(self.latencies_ms),
                "min": min(self.latencies_ms),
                "max": max(self.latencies_ms),
            }
            if self.latencies_ms
            else None,
            "x_rate_limit_limit_values": sorted(self.rate_limit_values),
            "x_rate_limit_remaining_counts": dict(
                sorted(self.rate_limit_remaining_counts.items())
            ),
            "x_rate_limit_remaining_min": min(remaining_ints) if remaining_ints else None,
            "x_rate_limit_remaining_max": max(remaining_ints) if remaining_ints else None,
        }


@dataclass
class ProbeState:
    """All mutable accumulators for the probe run, for interrupt-safety."""

    http_requests_total: int = 0
    last_request_mono: float | None = None
    header_names_seen: set[str] = field(default_factory=set)
    allowed_header_values: dict[str, str] = field(default_factory=dict)
    endpoint_stats: dict[str, EndpointStats] = field(default_factory=dict)
    auth_required: set[str] = field(default_factory=set)
    endpoints_attempted: set[str] = field(default_factory=set)
    rate_limited_endpoints: dict[str, Any] = field(default_factory=dict)
    cap_hit: bool = False
    rate_limited: bool = False
    run_start_mono: float = field(default_factory=time.monotonic)

    basic_ok: bool = False
    basic_connect_failed: bool = False

    sample_mints: list[str] = field(default_factory=list)
    feed_report: dict[str, Any] = field(default_factory=dict)
    feed_ran: bool = False

    summary_report: dict[str, Any] = field(default_factory=dict)
    summary_ran: bool = False
    summary_success_mints: list[str] = field(default_factory=list)

    report_report: dict[str, Any] = field(default_factory=dict)
    report_ran: bool = False

    analytics_report: dict[str, Any] = field(default_factory=dict)
    ticker_report: dict[str, Any] = field(default_factory=dict)

    sse_report: dict[str, Any] = field(default_factory=dict)
    sse_ran: bool = False

    ramp_report: dict[str, Any] = field(default_factory=dict)
    ramp_ran: bool = False

    limits_report: dict[str, Any] = field(default_factory=dict)
    limits_ran: bool = False

    skips: list[str] = field(default_factory=list)


class CaptureWriter:
    """Appends one JSON line per HTTP request / SSE line, flushing often."""

    def __init__(self, path: Path) -> None:
        self._fh = path.open("a", encoding="utf-8")
        self._since_flush = 0

    def _maybe_flush(self) -> None:
        self._since_flush += 1
        if self._since_flush >= CAPTURE_FLUSH_EVERY:
            self._fh.flush()
            self._since_flush = 0

    def write_request(
        self,
        phase: str,
        endpoint: str,
        wall_ns: int,
        mono_ns: int,
        status: int | None,
        latency_ms: float,
        headers: dict[str, str],
        body: str,
    ) -> None:
        """Append one capture line for an HTTP request/response pair."""
        remaining = None
        for name, value in headers.items():
            if name.lower() == "x-rate-limit-remaining" and value and HEADER_VALUE_RE.match(value):
                remaining = value
                break
        line = json.dumps(
            {
                "phase": phase,
                "endpoint": endpoint,
                "wall_ns": wall_ns,
                "mono_ns": mono_ns,
                "status": status,
                "latency_ms": latency_ms,
                "headers": headers,
                "x_rate_limit_remaining": remaining,
                "body": body[:CAPTURE_TRUNCATE],
            }
        )
        self._fh.write(line + "\n")
        self._maybe_flush()

    def write_sse_line(self, wall_ns: int, mono_ns: int, line_text: str) -> None:
        """Append one capture line for a received SSE line."""
        line = json.dumps(
            {"phase": "sse", "wall_ns": wall_ns, "mono_ns": mono_ns, "line": line_text}
        )
        self._fh.write(line + "\n")
        self._maybe_flush()

    def close(self) -> None:
        """Flush and close the capture file."""
        self._fh.flush()
        self._fh.close()


def _pace(state: ProbeState, min_gap: float = MIN_SPACING_S) -> None:
    """Sleep as needed to keep at least min_gap seconds since the last
    request COMPLETED (state.last_request_mono is stamped post-response),
    so consecutive capture-line receive timestamps are also >=min_gap apart."""
    if state.last_request_mono is not None:
        remaining = min_gap - (time.monotonic() - state.last_request_mono)
        if remaining > 0:
            time.sleep(remaining)


def _do_request(
    state: ProbeState,
    capture: CaptureWriter,
    phase: str,
    endpoint_template: str,
    url: str,
) -> ReqResult | None:
    """Pace (>=0.5s since the last request completed), cap, perform, and
    record one GET request. Returns None if the request cap has been
    reached."""
    if state.http_requests_total >= HTTP_REQUEST_CAP:
        state.cap_hit = True
        return None
    _pace(state)
    result = _http_get(url)
    state.last_request_mono = time.monotonic()
    state.http_requests_total += 1
    state.endpoints_attempted.add(endpoint_template)

    for name in result.headers:
        state.header_names_seen.add(name.lower())
    state.allowed_header_values.update(_allowed_headers(result.headers))

    stats = state.endpoint_stats.setdefault(endpoint_template, EndpointStats())
    stats.observe(result)

    if result.status in (401, 403):
        state.auth_required.add(endpoint_template)
    if result.status == 429:
        state.rate_limited = True
        state.rate_limited_endpoints[endpoint_template] = {
            "headers": _allowed_headers(result.headers),
        }

    capture.write_request(
        phase,
        endpoint_template,
        result.wall_ns,
        result.mono_ns,
        result.status,
        result.latency_ms,
        result.headers,
        result.body_text,
    )
    return result


def _safe_json(text: str) -> Any | None:
    """Parse JSON text, returning None (never raising) on failure."""
    try:
        return json.loads(text)
    except ValueError:
        return None


def _is_terminal(result: ReqResult | None) -> bool:
    """Return whether a phase loop must stop: capped, 401/403, or 429.

    401/403 are never retried for that endpoint; 429 stops the current
    phase outright and is never retried either.
    """
    return result is None or result.status in (401, 403, 429)


# --------------------------------------------------------------------------
# Phase 1: basic
# --------------------------------------------------------------------------


def _phase_basic(state: ProbeState, capture: CaptureWriter, base_url: str) -> None:
    """GET /ping and record whether it parses and its top-level keys."""
    result = _do_request(state, capture, "basic", "/ping", f"{base_url}/ping")
    if result is None:
        return
    if result.status == 200:
        state.basic_ok = True
    elif result.error is not None:
        state.basic_connect_failed = True

    parsed = _safe_json(result.body_text)
    walk = ShapeWalk()
    if isinstance(parsed, dict):
        walk.walk(parsed, "", 0, 2)
    state.feed_report.setdefault("_basic_body_shape", walk.render())


# --------------------------------------------------------------------------
# Phase 2: feed
# --------------------------------------------------------------------------


def _phase_feed(
    state: ProbeState, capture: CaptureWriter, base_url: str, polls: int, interval: float
) -> None:
    """Poll /v1/stats/new_tokens repeatedly and characterize the feed."""
    state.feed_ran = True
    endpoint = "/v1/stats/new_tokens"
    seen_mints: set[str] = set()
    recency_order: list[str] = []
    all_lags: list[float] = []
    newest10_lags: list[float] = []
    poll_summaries: list[dict[str, Any]] = []
    auth_counts: dict[str, int] = {}
    program_values: dict[str, int] = {}
    program_overflow = False
    first_poll_createats: list[datetime] = []
    oldest_age_s: float | None = None

    previous_poll_mints: set[str] = set()
    overlap_per_poll: list[int] = []
    zero_overlap_polls = 0
    descending_createat_order = True
    newest_item_lags: list[float] = []
    all_createats_global: list[datetime] = []

    for poll_idx in range(polls):
        if poll_idx > 0:
            time.sleep(interval)
        result = _do_request(state, capture, "feed", endpoint, f"{base_url}{endpoint}")
        if _is_terminal(result):
            break
        receive_dt = datetime.fromtimestamp(result.wall_ns / 1e9, tz=UTC)
        parsed = _safe_json(result.body_text)
        items = parsed if isinstance(parsed, list) else []

        new_count = 0
        item_createats: list[datetime] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            mint = item.get("mint")
            if isinstance(mint, str) and MINT_RE.match(mint) and mint not in seen_mints:
                new_count += 1
                seen_mints.add(mint)
                recency_order.append(mint)

            for auth_key in ("mintAuthority", "freezeAuthority"):
                value = item.get(auth_key)
                bucket = auth_counts.setdefault(auth_key, {"empty": 0, "non_empty": 0})
                if value in (None, ""):
                    bucket["empty"] += 1
                else:
                    bucket["non_empty"] += 1

            for key in ("program", "decimals"):
                if key not in item or program_overflow:
                    continue
                str_value = str(item[key])
                if PROGRAM_VALUE_RE.match(str_value):
                    program_values.setdefault(key, {})
                    program_values[key][str_value] = program_values[key].get(str_value, 0) + 1
                    if len(program_values[key]) > 12:
                        program_overflow = True

            create_at = item.get("createAt")
            if isinstance(create_at, str):
                parsed_dt = _parse_iso8601(create_at)
                if parsed_dt is not None:
                    item_createats.append(parsed_dt)
                    lag_s = (receive_dt - parsed_dt).total_seconds()
                    all_lags.append(lag_s)

        poll_mints = {
            item.get("mint")
            for item in items
            if isinstance(item, dict) and isinstance(item.get("mint"), str)
        }
        overlap = len(poll_mints & previous_poll_mints) if poll_idx > 0 else 0
        overlap_per_poll.append(overlap)
        if poll_idx > 0 and overlap == 0:
            zero_overlap_polls += 1
        previous_poll_mints = poll_mints

        if len(item_createats) > 1:
            for i in range(1, len(item_createats)):
                if item_createats[i] > item_createats[i - 1]:
                    descending_createat_order = False
        all_createats_global.extend(item_createats)
        if item_createats:
            newest_dt = max(item_createats)
            newest_item_lags.append((receive_dt - newest_dt).total_seconds())

        newest_slice = [i for i in items if isinstance(i, dict)][:10]
        for item in newest_slice:
            create_at = item.get("createAt")
            if isinstance(create_at, str):
                parsed_dt = _parse_iso8601(create_at)
                if parsed_dt is not None:
                    newest10_lags.append((receive_dt - parsed_dt).total_seconds())

        if poll_idx == 0:
            first_poll_createats = sorted(item_createats)
            if first_poll_createats:
                oldest_age_s = (receive_dt - first_poll_createats[0]).total_seconds()

        poll_summaries.append(
            {"item_count": len(items), "new_items": new_count, "auth_counts": dict(auth_counts)}
        )

        for auth_key, bucket in auth_counts.items():
            auth_counts[auth_key] = dict(bucket)

    median_interval_s: float | None = None
    if len(first_poll_createats) >= 2:
        diffs = [
            (first_poll_createats[i] - first_poll_createats[i - 1]).total_seconds()
            for i in range(1, len(first_poll_createats))
        ]
        median_interval_s = statistics.median(abs(d) for d in diffs)

    def _lag_stats(values: list[float]) -> dict[str, float] | None:
        if not values:
            return None
        sv = sorted(values)
        p95_idx = min(len(sv) - 1, round(0.95 * (len(sv) - 1)))
        return {
            "min": sv[0],
            "median": statistics.median(sv),
            "p95": sv[p95_idx],
            "max": sv[-1],
        }

    program_report: dict[str, Any] = {}
    for key in ("program", "decimals"):
        values = program_values.get(key, {})
        if program_overflow or len(values) > 12:
            program_report[key] = {"distinct_count": len(values)}
        else:
            program_report[key] = {"values": dict(values)}

    effective_item_rate: float | None = None
    if len(all_createats_global) >= 2:
        span_s = (max(all_createats_global) - min(all_createats_global)).total_seconds()
        if span_s > 0:
            effective_item_rate = (len(seen_mints) - 1) / span_s

    state.sample_mints = recency_order[:50]
    state.feed_report.update(
        {
            "polls": poll_summaries,
            "all_items_lag_s": _lag_stats(all_lags),
            "newest10_lag_s": _lag_stats(newest10_lags),
            "oldest_item_age_s_first_poll": oldest_age_s,
            "median_createat_interval_s_first_poll": median_interval_s,
            "auth_counts": auth_counts,
            "program_and_decimals": program_report,
            "distinct_mints_collected": len(state.sample_mints),
            "overlap_per_poll": overlap_per_poll,
            "zero_overlap_polls": zero_overlap_polls,
            "descending_createat_order": descending_createat_order,
            "effective_item_rate_per_s": effective_item_rate,
            "newest_item_lag_s": _lag_stats(newest_item_lags),
        }
    )


# --------------------------------------------------------------------------
# Phase 3: summary
# --------------------------------------------------------------------------


def _phase_summary(
    state: ProbeState,
    capture: CaptureWriter,
    base_url: str,
    sample_mints: int,
    spacing: float,
) -> None:
    """Probe /v1/tokens/{mint}/report/summary with the cacheOnly dance."""
    state.summary_ran = True
    endpoint = "/v1/tokens/{mint}/report/summary"
    mints = state.sample_mints[:sample_mints]
    if not mints:
        state.skips.append("summary_skipped_no_sample_mints")
        return

    step_stats: dict[str, EndpointStats] = {
        "a_cacheOnly_1": EndpointStats(),
        "b_live": EndpointStats(),
        "c_cacheOnly_2": EndpointStats(),
    }
    already_cached = 0
    walk = ShapeWalk()
    score_values: list[float] = []
    score_norm_values: list[float] = []
    lp_locked_values: list[float] = []
    level_values: list[str] = []
    risk_names: list[str] = []
    risk_mints_by_name: dict[str, set[str]] = {}
    risk_counts_by_name: dict[str, int] = {}

    for i, mint in enumerate(mints):
        if i > 0:
            time.sleep(spacing)
        base = f"{base_url}/v1/tokens/{mint}/report/summary"

        res_a = _do_request(
            state, capture, "summary", endpoint, f"{base}?cacheOnly=true"
        )
        if _is_terminal(res_a):
            break
        step_stats["a_cacheOnly_1"].observe(res_a)
        if res_a.status == 200:
            already_cached += 1

        time.sleep(spacing)
        res_b = _do_request(state, capture, "summary", endpoint, base)
        if _is_terminal(res_b):
            break
        step_stats["b_live"].observe(res_b)

        time.sleep(spacing)
        res_c = _do_request(
            state, capture, "summary", endpoint, f"{base}?cacheOnly=true"
        )
        if _is_terminal(res_c):
            break
        step_stats["c_cacheOnly_2"].observe(res_c)

        mint_risk_names: list[str] = []
        for res in (res_a, res_b, res_c):
            if res.status != 200:
                continue
            parsed = _safe_json(res.body_text)
            if parsed is None:
                continue
            state.summary_success_mints.append(mint)
            walk.walk(parsed, "", 0, MAX_WALK_DEPTH_SUMMARY)
            _collect_named_numeric(parsed, "score", score_values)
            _collect_named_numeric(parsed, "score_normalised", score_norm_values)
            _collect_named_numeric(parsed, "lpLockedPct", lp_locked_values)
            _collect_named_strings(parsed, "level", level_values)
            this_risk_names: list[str] = []
            _collect_risk_names(parsed, this_risk_names)
            mint_risk_names.extend(this_risk_names)
            risk_names.extend(this_risk_names)

        for name in set(mint_risk_names):
            risk_mints_by_name.setdefault(name, set()).add(mint)
        for name in mint_risk_names:
            risk_counts_by_name[name] = risk_counts_by_name.get(name, 0) + 1

    def _numeric_summary(values: list[float]) -> dict[str, float] | None:
        if not values:
            return None
        return {"min": min(values), "median": statistics.median(values), "max": max(values)}

    level_report: dict[str, int] | None = None
    distinct_levels = set(level_values)
    if level_values and len(distinct_levels) <= 10 and all(
        LEVEL_RE.match(v) for v in distinct_levels
    ):
        level_report = {v: level_values.count(v) for v in distinct_levels}

    allowed_risk_names: dict[str, int] = {}
    other_names_count = 0
    for name, count in risk_counts_by_name.items():
        if len(risk_mints_by_name.get(name, set())) >= 3 and RISK_NAME_RE.match(name):
            allowed_risk_names[name] = count
        else:
            other_names_count += count

    state.summary_report = {
        "mints_sampled": len(mints),
        "step_stats": {k: v.render() for k, v in step_stats.items()},
        "already_cached_before_a": already_cached,
        "shape": walk.render(),
        "score": _numeric_summary(score_values),
        "score_normalised": _numeric_summary(score_norm_values),
        "lp_locked_pct": _numeric_summary(lp_locked_values),
        "level_values": level_report,
        "risk_names": allowed_risk_names,
        "other_names": f"other_names:{other_names_count}",
    }


# --------------------------------------------------------------------------
# Phase 4: report
# --------------------------------------------------------------------------


def _phase_report(
    state: ProbeState, capture: CaptureWriter, base_url: str, report_mints: int, spacing: float
) -> None:
    """Probe /v1/tokens/{mint}/report for a small sample of mints."""
    state.report_ran = True
    endpoint = "/v1/tokens/{mint}/report"
    pool = state.summary_success_mints or state.sample_mints
    mints = pool[:report_mints]
    if not mints:
        state.skips.append("report_skipped_no_sample_mints")
        return

    walk = ShapeWalk()
    risk_names: list[str] = []
    risk_mints_by_name: dict[str, set[str]] = {}
    risk_counts_by_name: dict[str, int] = {}
    sizes: list[int] = []
    stats = EndpointStats()
    top_level_key_types: dict[str, set[str]] = {}
    interesting: dict[str, InterestingKeyAgg] = {}

    for i, mint in enumerate(mints):
        if i > 0:
            time.sleep(spacing)
        url = f"{base_url}/v1/tokens/{mint}/report"
        result = _do_request(state, capture, "report", endpoint, url)
        if _is_terminal(result):
            break
        stats.observe(result)
        sizes.append(len(result.body_text))
        if result.status != 200:
            continue
        parsed = _safe_json(result.body_text)
        if parsed is None:
            continue
        walk.walk(parsed, "", 0, MAX_WALK_DEPTH_REPORT)
        if isinstance(parsed, dict) and _dict_is_printable(parsed):
            for key, v in parsed.items():
                top_level_key_types.setdefault(key, set()).add(_json_type_name(v))
        _collect_interesting_keys(parsed, interesting)
        mint_risk_names: list[str] = []
        _collect_risk_names(parsed, mint_risk_names)
        risk_names.extend(mint_risk_names)
        for name in set(mint_risk_names):
            risk_mints_by_name.setdefault(name, set()).add(mint)
        for name in mint_risk_names:
            risk_counts_by_name[name] = risk_counts_by_name.get(name, 0) + 1

    allowed_risk_names: dict[str, int] = {}
    other_names_count = 0
    for name, count in risk_counts_by_name.items():
        if len(risk_mints_by_name.get(name, set())) >= 3 and RISK_NAME_RE.match(name):
            allowed_risk_names[name] = count
        else:
            other_names_count += count

    state.report_report = {
        "mints_sampled": len(mints),
        "status_stats": stats.render(),
        "response_size_bytes": {
            "min": min(sizes),
            "median": statistics.median(sizes),
            "max": max(sizes),
        }
        if sizes
        else None,
        "shape": walk.render(),
        "top_level_keys": {k: sorted(v) for k, v in sorted(top_level_key_types.items())},
        "interesting_keys": _render_interesting_keys(interesting),
        "risk_names": allowed_risk_names,
        "other_names": f"other_names:{other_names_count}",
    }


# --------------------------------------------------------------------------
# Phase 5: analytics
# --------------------------------------------------------------------------


def _phase_analytics(state: ProbeState, capture: CaptureWriter, base_url: str, spacing: float) -> None:
    """GET /v1/stats/analytics for two windows and report structure + numbers."""
    endpoint = "/v1/stats/analytics"
    windows = ["24h", "7d"]
    shapes: dict[str, Any] = {}
    numeric_by_window: dict[str, list[list[Any]]] = {}

    for i, window in enumerate(windows):
        if i > 0:
            time.sleep(spacing)
        url = f"{base_url}{endpoint}?window={urllib.parse.quote(window)}"
        result = _do_request(state, capture, "analytics", endpoint, url)
        if _is_terminal(result):
            break
        if result.status != 200:
            continue
        parsed = _safe_json(result.body_text)
        if parsed is None:
            continue
        walk = ShapeWalk()
        walk.walk(parsed, "", 0, 4)
        shapes[window] = walk.render()
        leaves: list[tuple[str, float]] = []
        _collect_numeric_leaves(parsed, "", leaves)
        numeric_by_window[window] = [[p, v] for p, v in leaves]

    state.analytics_report = {"shapes": shapes, "numeric_values": numeric_by_window}


# --------------------------------------------------------------------------
# Phase 6: ticker
# --------------------------------------------------------------------------


def _phase_ticker(state: ProbeState, capture: CaptureWriter, base_url: str, spacing: float) -> None:
    """GET the rugs ticker and top-liquidity endpoints."""
    reports: dict[str, Any] = {}
    calls = [
        ("/v1/stats/rugs/ticker", "limit=10"),
        ("/v1/stats/rugs/top-liquidity", "limit=5"),
    ]
    for i, (endpoint, query) in enumerate(calls):
        if i > 0:
            time.sleep(spacing)
        url = f"{base_url}{endpoint}?{query}"
        result = _do_request(state, capture, "ticker", endpoint, url)
        if _is_terminal(result):
            break
        if result.status != 200:
            reports[endpoint] = {"status": result.status}
            continue
        parsed = _safe_json(result.body_text)
        items = parsed if isinstance(parsed, list) else []
        receive_dt = datetime.fromtimestamp(result.wall_ns / 1e9, tz=UTC)

        walk = ShapeWalk()
        walk.walk(items, "", 0, 4)

        lags: list[float] = []
        numeric_values: list[tuple[str, float]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            lag = _time_like_lag_s(item, receive_dt)
            if lag is not None:
                lags.append(lag)
            for key, value in item.items():
                if (
                    TICKER_NUMERIC_KEY_RE.search(key)
                    and isinstance(value, (int, float))
                    and not isinstance(value, bool)
                ):
                    label = key if PRINTABLE_KEY_RE.match(key) else "<redacted-key>"
                    numeric_values.append((label, float(value)))

        reports[endpoint] = {
            "status": result.status,
            "item_count": len(items),
            "shape": walk.render(),
            "time_lag_s": {
                "min": min(lags),
                "median": statistics.median(lags),
                "max": max(lags),
            }
            if lags
            else None,
            "numeric_values": [[p, v] for p, v in numeric_values],
        }

    state.ticker_report = reports


# --------------------------------------------------------------------------
# Phase: limits (bounded burst + backoff rate-limit probe)
# --------------------------------------------------------------------------


def _phase_limits(state: ProbeState, capture: CaptureWriter, base_url: str) -> None:
    """Bounded, no-ramp probe of the cacheOnly rate limit: a 5-way
    concurrent burst, then single requests at 1/2/4/8/16s offsets. Hard
    cap of 10 requests total; stops immediately on 429/401/403/5xx."""
    state.limits_ran = True
    endpoint = "/v1/tokens/{mint}/report/summary?cacheOnly=true"
    mint = next(iter(state.summary_success_mints), None)
    if mint is None:
        state.skips.append("limits_skipped_no_summary_mint")
        return
    if state.http_requests_total + LIMITS_REQUEST_CAP > HTTP_REQUEST_CAP:
        state.skips.append("limits_skipped_request_cap")
        return

    url = f"{base_url}/v1/tokens/{mint}/report/summary?cacheOnly=true"
    sequence: list[dict[str, Any]] = []
    stopped = False

    def _is_stop_status(status: int | None) -> bool:
        return status in (401, 403, 429) or (status is not None and status >= 500)

    def _record(phase_label: str, offset_s: float, result: ReqResult) -> dict[str, Any]:
        state.http_requests_total += 1
        state.endpoints_attempted.add(endpoint)
        limit_val: str | None = None
        remaining_val: str | None = None
        for name, value in result.headers.items():
            lname = name.lower()
            if not value or not HEADER_VALUE_RE.match(value):
                continue
            if lname == "x-rate-limit-limit":
                limit_val = value
            elif lname == "x-rate-limit-remaining":
                remaining_val = value
        for name in result.headers:
            state.header_names_seen.add(name.lower())
        state.allowed_header_values.update(_allowed_headers(result.headers))
        capture.write_request(
            "limits", endpoint, result.wall_ns, result.mono_ns,
            result.status, result.latency_ms, result.headers, result.body_text,
        )
        if result.status == 429:
            state.rate_limited = True
            state.rate_limited_endpoints[endpoint] = {"headers": _allowed_headers(result.headers)}
        if result.status in (401, 403):
            state.auth_required.add(endpoint)
        entry = {
            "phase": phase_label,
            "offset_s": round(offset_s, 3),
            "status": result.status,
            "latency_ms": result.latency_ms,
            "x_rate_limit_limit": limit_val,
            "x_rate_limit_remaining": remaining_val,
        }
        sequence.append(entry)
        return entry

    results: list[ReqResult] = [None] * LIMITS_BURST_SIZE  # type: ignore[list-item]
    barrier = threading.Barrier(LIMITS_BURST_SIZE)

    def _worker(idx: int) -> None:
        barrier.wait()
        results[idx] = _http_get(url)

    burst_start = time.perf_counter()
    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(LIMITS_BURST_SIZE)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for res in sorted(results, key=lambda r: r.mono_ns):
        offset = max(0.0, (res.mono_ns / 1e9) - burst_start)
        entry = _record("burst", offset, res)
        if _is_stop_status(entry["status"]):
            stopped = True

    if not stopped:
        for offset_target in LIMITS_OFFSETS_S:
            if len(sequence) >= LIMITS_REQUEST_CAP:
                break
            remaining_wait = (burst_start + offset_target) - time.perf_counter()
            if remaining_wait > 0:
                time.sleep(remaining_wait)
            res = _http_get(url)
            actual_offset = time.perf_counter() - burst_start
            entry = _record("offset", actual_offset, res)
            if _is_stop_status(entry["status"]):
                stopped = True
                break

    all_limits = [
        int(e["x_rate_limit_limit"])
        for e in sequence
        if e["x_rate_limit_limit"] is not None and e["x_rate_limit_limit"].lstrip("-").isdigit()
    ]
    recovered_at: str | int = "not_within_16s"
    if all_limits:
        ref_limit = max(all_limits)
        for e in sequence:
            if e["phase"] != "offset":
                continue
            rem = e["x_rate_limit_remaining"]
            if rem is not None and rem.lstrip("-").isdigit() and int(rem) == ref_limit - 1:
                recovered_at = round(e["offset_s"])
                break

    state.limits_report = {
        "sequence": sequence,
        "total_requests": len(sequence),
        "stopped_early": stopped,
        "remaining_returned_to_max_at_offset_s": recovered_at,
    }


# --------------------------------------------------------------------------
# Phase 7: SSE
# --------------------------------------------------------------------------


def _phase_sse(state: ProbeState, capture: CaptureWriter, base_url: str, seconds: float) -> None:
    """One SSE connection to /v1/stats/rugs/stream for `seconds`.

    Staged timing: TCP+TLS connect is bounded by CONNECT_TIMEOUT_S; once
    connected, the response status line and headers are awaited for up to
    the whole `seconds` window (the server may withhold them until its
    first event); any remaining time in the window is then spent reading
    lines. Exactly one connection is ever made, no reconnect.
    """
    state.sse_ran = True
    parsed_url = urllib.parse.urlsplit(base_url)
    endpoint = "/v1/stats/rugs/stream"
    headers = {
        "Accept": "text/event-stream",
        "Cache-Control": "no-cache",
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "identity",
    }

    conn: HTTPSConnection | HTTPConnection
    connect_start = time.perf_counter()
    try:
        if parsed_url.scheme == "https":
            conn = HTTPSConnection(
                parsed_url.hostname,
                parsed_url.port or 443,
                timeout=CONNECT_TIMEOUT_S,
                context=ssl.create_default_context(),
            )
        else:
            conn = HTTPConnection(
                parsed_url.hostname, parsed_url.port or 80, timeout=CONNECT_TIMEOUT_S
            )
        conn.connect()
    except Exception as exc:  # noqa: BLE001
        state.sse_report = {
            "connected": False,
            "outcome": "connect_failed",
            "connect_ms": None,
            "exception_class": type(exc).__name__,
        }
        return
    connect_ms = (time.perf_counter() - connect_start) * 1000

    window = max(seconds, 0.0)
    try:
        conn.sock.settimeout(max(window, 1.0))  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001, S110 - best-effort
        pass

    request_sent_perf = time.perf_counter()
    try:
        conn.request("GET", endpoint, headers=headers)
        resp = conn.getresponse()
    except (TimeoutError, OSError) as exc:
        state.sse_report = {
            "connected": True,
            "outcome": "no_headers_within_window",
            "connect_ms": connect_ms,
            "headers_ms": None,
            "exception_class": type(exc).__name__,
        }
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110
            pass
        return
    except Exception as exc:  # noqa: BLE001
        state.sse_report = {
            "connected": False,
            "outcome": "connect_failed",
            "connect_ms": connect_ms,
            "exception_class": type(exc).__name__,
        }
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110
            pass
        return

    headers_ms = (time.perf_counter() - request_sent_perf) * 1000
    status = resp.status
    resp_headers = {k.lower(): v for k, v in resp.getheaders()}
    for name in resp_headers:
        state.header_names_seen.add(name.lower())
    state.allowed_header_values.update(_allowed_headers(resp_headers))
    content_type = resp_headers.get("content-type", "")
    is_event_stream = "text/event-stream" in content_type

    comment_count = 0
    comment_times: list[float] = []
    event_names: set[str] = set()
    id_count = 0
    non_json_data = 0
    data_walk = ShapeWalk()
    data_event_times: list[int] = []
    data_lags: list[float] = []
    first_event_ms: float | None = None

    deadline_perf = request_sent_perf + window
    try:
        while True:
            remaining = deadline_perf - time.perf_counter()
            if remaining <= 0:
                break
            try:
                conn.sock.settimeout(remaining)  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001, S110
                pass
            try:
                raw_line = resp.readline()
            except (TimeoutError, OSError):
                break
            if not raw_line:
                break
            wall_ns = time.time_ns()
            mono_ns = time.perf_counter_ns()
            line_text = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            capture.write_sse_line(wall_ns, mono_ns, line_text)

            if line_text.startswith(":"):
                comment_count += 1
                comment_times.append(mono_ns / 1e9)
            elif line_text.startswith("event:"):
                if first_event_ms is None:
                    first_event_ms = (time.perf_counter() - request_sent_perf) * 1000
                name = line_text[len("event:") :].strip()
                if EVENT_NAME_RE.match(name):
                    event_names.add(name)
            elif line_text.startswith("id:"):
                id_count += 1
            elif line_text.startswith("data:"):
                if first_event_ms is None:
                    first_event_ms = (time.perf_counter() - request_sent_perf) * 1000
                payload = line_text[len("data:") :].strip()
                parsed = _safe_json(payload)
                if parsed is None:
                    non_json_data += 1
                    continue
                data_walk.walk(parsed, "", 0, MAX_WALK_DEPTH_SUMMARY)
                data_event_times.append(wall_ns)
                if isinstance(parsed, dict):
                    receive_dt = datetime.fromtimestamp(wall_ns / 1e9, tz=UTC)
                    lag = _time_like_lag_s(parsed, receive_dt)
                    if lag is not None:
                        data_lags.append(lag)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110 - closing a possibly-already-dead connection
            pass

    def _interval_stats(times: list[float]) -> dict[str, float] | None:
        if len(times) < 2:
            return None
        diffs = sorted(times[i] - times[i - 1] for i in range(1, len(times)))
        p95_idx = min(len(diffs) - 1, round(0.95 * (len(diffs) - 1)))
        return {"min": diffs[0], "median": statistics.median(diffs), "p95": diffs[p95_idx], "max": diffs[-1]}

    outcome = "events_received" if (data_event_times or event_names) else "headers_received_no_events"

    event_time_s = [t / 1e9 for t in data_event_times]
    state.sse_report = {
        "connected": True,
        "outcome": outcome,
        "connect_ms": connect_ms,
        "headers_ms": headers_ms,
        "first_event_ms": first_event_ms,
        "status": status,
        "content_type_is_event_stream": is_event_stream,
        "comment_count": comment_count,
        "comment_median_interval_s": statistics.median(
            [comment_times[i] - comment_times[i - 1] for i in range(1, len(comment_times))]
        )
        if len(comment_times) >= 2
        else None,
        "event_names": sorted(event_names),
        "id_count": id_count,
        "non_json_data_count": non_json_data,
        "data_event_count": len(data_event_times),
        "data_shape": data_walk.render(),
        "data_inter_event_s": _interval_stats(event_time_s),
        "data_time_lag_s": {
            "min": min(data_lags),
            "median": statistics.median(data_lags),
            "max": max(data_lags),
        }
        if data_lags
        else None,
    }


# --------------------------------------------------------------------------
# Phase 8: ramp (opt-in)
# --------------------------------------------------------------------------


def _ramp(
    base_url: str,
    mint: str,
    steps: list[int],
    step_seconds: float,
    max_requests: int,
    capture: CaptureWriter,
) -> dict[str, Any]:
    """Send a stepped-rate ramp of cacheOnly summary requests, opt-in only."""
    endpoint = "/v1/tokens/{mint}/report/summary?cacheOnly=true"
    url = f"{base_url}/v1/tokens/{mint}/report/summary?cacheOnly=true"
    total = 0
    status_counts: dict[str, int] = {}
    request_mono_times: list[float] = []
    hit_rate = "none_observed"
    stop_detail: dict[str, Any] | None = None
    start_mono = time.monotonic()
    stopped = False

    pool = ThreadPoolExecutor(max_workers=4)
    try:
        for rate in steps:
            if stopped or total >= max_requests:
                break
            interval = 1.0 / rate
            step_deadline = time.monotonic() + step_seconds
            next_tick = time.monotonic()
            while time.monotonic() < step_deadline and total < max_requests:
                now = time.monotonic()
                if now < next_tick:
                    time.sleep(next_tick - now)
                fut = pool.submit(_http_get, url, REQUEST_TIMEOUT_S)
                try:
                    res = fut.result(timeout=REQUEST_TIMEOUT_S + 5)
                except Exception:  # noqa: BLE001
                    next_tick += interval
                    continue
                req_mono = time.monotonic()
                total += 1
                request_mono_times.append(req_mono)
                next_tick += interval

                status_key = str(res.status) if res.status is not None else "error"
                status_counts[status_key] = status_counts.get(status_key, 0) + 1
                capture.write_request(
                    "ramp", endpoint, res.wall_ns, res.mono_ns, res.status, res.latency_ms,
                    res.headers, res.body_text,
                )

                is_limit_hit = res.status == 429 or res.status == 403 or (
                    res.status is not None and res.status >= 500
                )
                if is_limit_hit:
                    stopped = True
                    elapsed = req_mono - start_mono
                    window_1s = sum(1 for t in request_mono_times if req_mono - t <= 1.0)
                    window_10s = sum(1 for t in request_mono_times if req_mono - t <= 10.0)
                    retry_after_raw = res.headers.get("retry-after")
                    hit_rate = f"{res.status}@{rate}"
                    stop_detail = {
                        "status": res.status,
                        "rate": rate,
                        "request_index": total,
                        "elapsed_s": elapsed,
                        "window_1s": window_1s,
                        "window_10s": window_10s,
                        "headers": _allowed_headers(res.headers),
                    }
                    retry_after: float | None = None
                    if retry_after_raw is not None:
                        try:
                            retry_after = float(retry_after_raw)
                        except ValueError:
                            retry_after = None
                    if res.status == 429 and retry_after is not None and retry_after <= 120:
                        time.sleep(retry_after + 2)
                        verify_res = _http_get(url, REQUEST_TIMEOUT_S)
                        total += 1
                        vkey = str(verify_res.status) if verify_res.status is not None else "error"
                        status_counts[vkey] = status_counts.get(vkey, 0) + 1
                        capture.write_request(
                            "ramp", endpoint, verify_res.wall_ns, verify_res.mono_ns,
                            verify_res.status, verify_res.latency_ms, verify_res.headers,
                            verify_res.body_text,
                        )
                        stop_detail["verification_status"] = verify_res.status
                    else:
                        stop_detail["verification_status"] = "no_verification"
                    break
            if stopped:
                break
    finally:
        pool.shutdown(wait=False)

    return {
        "total_requests": total,
        "status_counts": status_counts,
        "hit_rate": hit_rate,
        "stop_detail": stop_detail,
    }


# --------------------------------------------------------------------------
# CLI / orchestration
# --------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse and validate CLI arguments."""

    def phases_type(raw: str) -> list[str]:
        values = [v.strip() for v in raw.split(",") if v.strip()]
        for v in values:
            if v not in ALL_PHASES:
                raise argparse.ArgumentTypeError(f"unknown phase {v!r}")
        return values

    def bounded_int(name: str, lo: int, hi: int) -> Any:
        def _inner(raw: str) -> int:
            value = int(raw)
            if value < lo or value > hi:
                raise argparse.ArgumentTypeError(f"--{name} must be in [{lo}, {hi}]")
            return value

        return _inner

    def bounded_float(name: str, lo: float, hi: float) -> Any:
        def _inner(raw: str) -> float:
            value = float(raw)
            if value < lo or value > hi:
                raise argparse.ArgumentTypeError(f"--{name} must be in [{lo}, {hi}]")
            return value

        return _inner

    def ramp_steps_type(raw: str) -> list[int]:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if not parts or len(parts) > HARD_MAX_RAMP_STEPS:
            raise argparse.ArgumentTypeError(
                f"--ramp-steps must have 1..{HARD_MAX_RAMP_STEPS} values"
            )
        values = [int(p) for p in parts]
        for v in values:
            if v < 1 or v > HARD_MAX_RAMP_STEP_RATE:
                raise argparse.ArgumentTypeError(
                    f"--ramp-steps values must be in [1, {HARD_MAX_RAMP_STEP_RATE}]"
                )
        return values

    def label_type(raw: str) -> str:
        if not re.fullmatch(r"[a-z0-9_-]{1,30}", raw):
            raise argparse.ArgumentTypeError("--label must match [a-z0-9_-]{1,30}")
        return raw

    parser = argparse.ArgumentParser(description="Anonymous RugCheck API probe.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--phases", type=phases_type, default=list(DEFAULT_PHASES))
    parser.add_argument(
        "--feed-polls", type=bounded_int("feed-polls", 1, HARD_MAX_FEED_POLLS), default=DEFAULT_FEED_POLLS
    )
    parser.add_argument(
        "--feed-interval",
        type=bounded_float("feed-interval", MIN_FEED_INTERVAL, 3600),
        default=DEFAULT_FEED_INTERVAL,
    )
    parser.add_argument(
        "--sample-mints",
        type=bounded_int("sample-mints", 0, HARD_MAX_SAMPLE_MINTS),
        default=DEFAULT_SAMPLE_MINTS,
    )
    parser.add_argument(
        "--sample-spacing",
        type=bounded_float("sample-spacing", MIN_SAMPLE_SPACING, 60),
        default=DEFAULT_SAMPLE_SPACING,
    )
    parser.add_argument(
        "--report-mints",
        type=bounded_int("report-mints", 0, HARD_MAX_REPORT_MINTS),
        default=DEFAULT_REPORT_MINTS,
    )
    parser.add_argument(
        "--sse-seconds",
        type=bounded_int("sse-seconds", 0, HARD_MAX_SSE_SECONDS),
        default=DEFAULT_SSE_SECONDS,
    )
    parser.add_argument("--ramp", action="store_true")
    parser.add_argument("--ramp-steps", type=ramp_steps_type, default=ramp_steps_type(DEFAULT_RAMP_STEPS))
    parser.add_argument(
        "--ramp-step-seconds",
        type=bounded_float("ramp-step-seconds", 1, HARD_MAX_RAMP_STEP_SECONDS),
        default=DEFAULT_RAMP_STEP_SECONDS,
    )
    parser.add_argument(
        "--ramp-max-requests",
        type=bounded_int("ramp-max-requests", 1, HARD_MAX_RAMP_MAX_REQUESTS),
        default=DEFAULT_RAMP_MAX_REQUESTS,
    )
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--label", type=label_type, default=None)
    return parser.parse_args(argv)


def _validate_base_url(base_url: str) -> None:
    """Enforce https-only base URLs, except http://127.0.0.1 for self-tests."""
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and parsed.hostname == "127.0.0.1":
        return
    raise ValueError(f"--base-url must be https, got {base_url!r}")


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


def _verdict(state: ProbeState) -> str:
    """Classify keyless access per the keyless_ok contract."""
    if not state.basic_ok:
        return "false"
    if state.auth_required:
        return "partial"
    return "true"


def _sse_verdict(state: ProbeState) -> str:
    """Classify the SSE phase outcome."""
    if not state.sse_ran:
        return "not_run"
    return "true" if state.sse_report.get("outcome") == "events_received" else "false"


def _limits_verdict(state: ProbeState) -> str:
    """Classify the limits phase outcome."""
    if not state.limits_ran:
        return "not_run"
    if any(s.startswith("limits_skipped_") for s in state.skips):
        return "skipped"
    return "ran"


def _ramp_verdict(state: ProbeState) -> str:
    """Classify the ramp phase outcome."""
    if not state.ramp_ran:
        return "not_run"
    return state.ramp_report.get("hit_rate", "none_observed")


def _build_summary(state: ProbeState, started_utc: str, hostname: str, args: argparse.Namespace) -> dict[str, Any]:
    """Build the full summary dict written to the summary JSON file."""
    return {
        "started_utc": started_utc,
        "url_hostname": hostname,
        "phases": args.phases,
        "endpoint_stats": {k: v.render() for k, v in state.endpoint_stats.items()},
        "auth_required": sorted(state.auth_required),
        "header_names_seen": sorted(state.header_names_seen),
        "allowed_header_values": dict(sorted(state.allowed_header_values.items())),
        "http_requests_total": state.http_requests_total,
        "cap_hit": state.cap_hit,
        "feed": state.feed_report,
        "summary": state.summary_report,
        "report": state.report_report,
        "analytics": state.analytics_report,
        "ticker": state.ticker_report,
        "sse": state.sse_report,
        "ramp": state.ramp_report,
        "limits": state.limits_report,
        "skips": state.skips,
        "verdict_keyless_ok": _verdict(state),
        "verdict_feed_ok": state.feed_ran and bool(state.feed_report.get("distinct_mints_collected")),
        "verdict_sse_ok": _sse_verdict(state),
        "verdict_ramp": _ramp_verdict(state),
        "verdict_limits": _limits_verdict(state),
    }


def _stdout_report(summary: dict[str, Any]) -> dict[str, Any]:
    """Select the subset of the summary allowed on stdout (all permitted)."""
    return {
        "endpoint_stats": summary["endpoint_stats"],
        "auth_required": summary["auth_required"],
        "header_names_seen": summary["header_names_seen"],
        "allowed_header_values": summary["allowed_header_values"],
        "http_requests_total": summary["http_requests_total"],
        "feed": summary["feed"],
        "summary": summary["summary"],
        "report": summary["report"],
        "analytics": summary["analytics"],
        "ticker": summary["ticker"],
        "sse": summary["sse"],
        "ramp": summary["ramp"],
        "limits": summary["limits"],
        "skips": summary["skips"],
    }


def main(argv: list[str] | None = None) -> int:
    """Entry point: run the requested phases and write/print results."""
    args = _parse_args(argv)
    repo_root = Path(__file__).resolve().parent.parent

    try:
        _validate_base_url(args.base_url)
        out_dir = _resolve_out_dir(repo_root, args.out_dir)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)

    hostname = urllib.parse.urlsplit(args.base_url).hostname or ""
    now = datetime.now(UTC)
    stamp = _utc_stamp(now)
    stem = f"rugcheck_{stamp}" + (f"_{args.label}" if args.label else "")
    capture_path = out_dir / f"{stem}.raw.jsonl"
    summary_path = out_dir / f"{stem}.summary.json"

    print(
        f"{now.isoformat()} start host={hostname} phases={','.join(args.phases)} "
        f"feed_polls={args.feed_polls} sample_mints={args.sample_mints} "
        f"report_mints={args.report_mints} sse_seconds={args.sse_seconds} ramp={args.ramp}"
    )

    state = ProbeState()
    capture = CaptureWriter(capture_path)
    exit_code: int | None = None

    try:
        try:
            if "basic" in args.phases:
                _phase_basic(state, capture, args.base_url)
            if not state.basic_ok and state.basic_connect_failed:
                exit_code = 2

            if exit_code is None:

                def _gate() -> bool:
                    if state.cap_hit:
                        return False
                    if not state.rate_limited:
                        return True
                    if time.monotonic() - state.run_start_mono > 30:
                        return True
                    if "rate_limited_remaining_phases_skipped" not in state.skips:
                        state.skips.append("rate_limited_remaining_phases_skipped")
                    return False

                if "feed" in args.phases and _gate():
                    _phase_feed(state, capture, args.base_url, args.feed_polls, args.feed_interval)
                if "summary" in args.phases and _gate():
                    _phase_summary(
                        state, capture, args.base_url, args.sample_mints, args.sample_spacing
                    )
                if "report" in args.phases and _gate():
                    _phase_report(
                        state, capture, args.base_url, args.report_mints, args.sample_spacing
                    )
                if "analytics" in args.phases and _gate():
                    _phase_analytics(state, capture, args.base_url, args.sample_spacing)
                if "ticker" in args.phases and _gate():
                    _phase_ticker(state, capture, args.base_url, args.sample_spacing)
                if "limits" in args.phases and _gate():
                    _phase_limits(state, capture, args.base_url)

                if args.sse_seconds > 0:
                    _phase_sse(state, capture, args.base_url, float(args.sse_seconds))

                if args.ramp:
                    ramp_mint = next(iter(state.summary_success_mints), None)
                    if ramp_mint is None:
                        state.skips.append("ramp_skipped_no_summary_mint")
                    else:
                        state.ramp_ran = True
                        state.ramp_report = _ramp(
                            args.base_url,
                            ramp_mint,
                            args.ramp_steps,
                            args.ramp_step_seconds,
                            args.ramp_max_requests,
                            capture,
                        )
        except KeyboardInterrupt:
            exit_code = 130
    finally:
        capture.close()

    summary = _build_summary(state, now.isoformat(), hostname, args)
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(json.dumps(_stdout_report(summary), indent=2))

    print(
        f"VERDICT keyless_ok={summary['verdict_keyless_ok']} "
        f"feed_ok={summary['verdict_feed_ok']} "
        f"sse_ok={summary['verdict_sse_ok']} "
        f"sse_outcome={state.sse_report.get('outcome', 'not_run')} "
        f"ramp={summary['verdict_ramp']} "
        f"limits={summary['verdict_limits']}"
    )

    if exit_code is not None:
        return exit_code
    if not state.basic_ok and state.basic_connect_failed:
        return 2
    if state.endpoints_attempted and state.auth_required == state.endpoints_attempted:
        return 3
    return 0 if state.basic_ok else 2


if __name__ == "__main__":
    sys.exit(main())
