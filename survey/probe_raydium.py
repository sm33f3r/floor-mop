"""One-shot, anonymous probe of Raydium's public read APIs.

Throwaway Phase 1 measurement script. Never imported by src/. Mirrors the
conventions of survey/probe_rugcheck.py (request layer, printable-key rules,
summary style, VERDICT line). Stdlib only. Covers three hosts: the LaunchLab
Mint API, the LaunchLab History API and Raydium API v3. All response text
about tokens is hostile: values are never printed except numbers, booleans,
null counts, string length ranges, allow-listed enum-like strings, distinct
programId pubkeys and a small set of allow-listed header values. Dictionary
keys are only printed when they match a strict identifier pattern.
"""

from __future__ import annotations

import argparse
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

HOSTS = ("launch_mint", "launch_history", "v3")
DEFAULT_URLS = {
    "launch_mint": "https://launch-mint-v1.raydium.io",
    "launch_history": "https://launch-history-v1.raydium.io",
    "v3": "https://api-v3.raydium.io",
}
PHASE_ORDER = [
    "launch_mint",
    "launch_history",
    "v3_basic",
    "v3_pools",
    "v3_launch_pools",
    "v3_join",
    "freshness",
    "etag",
    "limits",
]
USER_AGENT = "floor-mop-survey/0.1"
SOL_MINT = "So11111111111111111111111111111111111111112"
REQUEST_TIMEOUT_S = 20.0
MIN_SPACING_S = 0.5
CAPTURE_TRUNCATE = 200_000
CAPTURE_FLUSH_EVERY = 20
READ_LIMIT_BYTES = 8_000_000
REQUEST_CAP = 250
DEFAULT_OUT_DIR = "data/survey"

LIMITS_BURST = 5
LIMITS_OFFSETS_S = (1, 2, 4, 8)
LIMITS_HOST_CAP = LIMITS_BURST + len(LIMITS_OFFSETS_S)
LIMITS_HOST_GAP_S = 20.0

MAX_WALK_DEPTH = 5
MAX_NUMERIC_PATHS = 150
MAX_ENUM_VALUES = 12
MAX_DICT_KEYS_PRINTABLE = 60
MAX_PUBKEYS_TRACKED = 5000

PRINTABLE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,40}\Z")
MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}\Z")
HEADER_VALUE_RE = re.compile(r"^[ -~]{1,80}\Z")
HEADER_NAME_RE = re.compile(r"^[a-z0-9-]{1,50}\Z")
ENUM_RE = re.compile(r"^[A-Za-z0-9_.-]+\Z")
TIME_KEY_RE = re.compile(r"time|at|date|created|ts|stamp|^t$", re.IGNORECASE)
ISO_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
NEXT_KEY_RE = re.compile(r"^[A-Za-z0-9_=.+/-]{1,256}\Z")

UNTRUSTED_KEYS = {
    "name",
    "symbol",
    "uri",
    "description",
    "image",
    "logo",
    "twitter",
    "telegram",
    "website",
    "links",
    "metadata",
    "text",
    "title",
    "comment",
    "bio",
}
ALLOWED_HEADER_NAMES = {
    "content-type",
    "cache-control",
    "age",
    "retry-after",
    "date",
    "server",
    "content-length",
    "cf-cache-status",
    "cf-mitigated",
}
RATE_PREFIXES = ("x-ratelimit", "x-rate-limit", "ratelimit")
INTERESTING_SUBSTRINGS = (
    "pool", "creator", "owner", "platform", "migrate", "status", "curve",
    "complete", "graduat", "vest", "supply", "price", "cap", "volume", "trade",
    "holder", "fee", "bonk", "config", "quote", "base",
)  # fmt: skip
MINT_KEYS = ("mint", "mintAddress", "address", "tokenMint", "baseMint", "id", "poolId")
POOL_KEYS = (
    "poolId", "pool", "poolAddress", "migratedPool", "migrated_pool",
    "cpmmPool", "pairAddress",
)  # fmt: skip
CREATOR_KEYS = ("creator", "creatorWallet", "owner", "user", "wallet", "deployer")


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


def first_time(item: dict[str, Any]) -> tuple[str, float, str] | None:
    """First time-like top-level key of an item: (key, epoch s, kind)."""
    for key, value in item.items():
        if TIME_KEY_RE.search(key):
            parsed = to_epoch(value)
            if parsed is not None:
                return key, parsed[0], parsed[1]
    return None


def items_of(data: Any) -> list[dict[str, Any]]:
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


def payload(data: Any) -> Any:
    """Unwrap a {"data": ...} envelope if present."""
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data


def find_next_key(data: Any, name: str = "nextPageKey") -> str | None:
    """Find a safe-looking paging-key string (default nextPageKey) in the first 3 levels."""
    queue: list[tuple[Any, int]] = [(data, 0)]
    while queue:
        node, depth = queue.pop(0)
        if isinstance(node, dict):
            value = node.get(name)
            if isinstance(value, str) and NEXT_KEY_RE.match(value):
                return value
            if depth < 3:
                queue.extend((v, depth + 1) for v in node.values())
    return None


def meta_fields(data: Any, names: tuple[str, ...]) -> dict[str, Any]:
    """Presence of named keys; values only for bool/number ones."""
    body = payload(data)
    out: dict[str, Any] = {}
    for name in names:
        present = isinstance(body, dict) and name in body
        entry: dict[str, Any] = {"present": present}
        value = body.get(name) if isinstance(body, dict) else None
        if isinstance(value, bool) or is_num(value):
            entry["value"] = value
        out[name] = entry
    return out


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
    pubkeys: dict[str, set[str]] = field(default_factory=dict)
    programs: dict[str, Counter[str]] = field(default_factory=dict)
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
            if last == "programId":
                self.programs.setdefault(path, Counter())[value] += 1
            else:
                seen = self.pubkeys.setdefault(path, set())
                if len(seen) < MAX_PUBKEYS_TRACKED:
                    seen.add(value)
            return
        segments = {s.replace("[]", "").lower() for s in path.split(".")}
        if segments & UNTRUSTED_KEYS or path in self.not_enum:
            self.not_enum.add(path)
            return
        if len(value) <= 24 and ENUM_RE.match(value):
            counter = self.enums.setdefault(path, Counter())
            counter[value] += 1
            if len(counter) > MAX_ENUM_VALUES:
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
            if self.enums.get(path) and path not in self.not_enum:
                entry["enum"] = dict(self.enums[path])
            if path in self.pubkeys:
                entry["pubkeys_distinct"] = len(self.pubkeys[path])
            if path in self.programs:
                progs = self.programs[path]
                if len(progs) <= MAX_ENUM_VALUES:
                    entry["program_ids"] = dict(progs)
                else:
                    entry["program_ids_distinct"] = len(progs)
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


def interesting_subset(rendered: dict[str, Any]) -> dict[str, Any]:
    """Keep only fields whose path contains an interesting substring."""
    keep = {
        p: e
        for p, e in rendered["fields"].items()
        if any(s in p.lower() for s in INTERESTING_SUBSTRINGS)
    }
    return {"docs": rendered["docs"], "total_fields": len(rendered["fields"]), "fields": keep}


# --------------------------------------------------------------------------
# Identity discovery
# --------------------------------------------------------------------------


@dataclass
class Ident:
    """Discovered identity key names and valid values for a set of items."""

    mint_key: str = "none"
    pool_key: str = "none"
    creator_key: str = "none"
    mints: list[str] = field(default_factory=list)
    creators: list[str] = field(default_factory=list)
    pools: dict[str, tuple[str, str]] = field(default_factory=dict)

    def keys_report(self) -> dict[str, str]:
        """Key names only."""
        return {
            "mint_key": self.mint_key,
            "pool_key": self.pool_key,
            "creator_key": self.creator_key,
        }


def first_valid(item: dict[str, Any], keys: tuple[str, ...]) -> tuple[str, str] | None:
    """First candidate key whose value is a valid pubkey: (key, value)."""
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and MINT_RE.match(value):
            return key, value
    return None


def modal_key(items: list[dict[str, Any]], keys: tuple[str, ...]) -> str | None:
    """Most common first-valid candidate key across items."""
    counts = Counter(p[0] for p in (first_valid(i, keys) for i in items) if p)
    return counts.most_common(1)[0][0] if counts else None


def identity(items: list[dict[str, Any]]) -> Ident:
    """Pick the modal identity keys and collect distinct valid values."""
    ident = Ident()
    mint_key = modal_key(items, MINT_KEYS)
    creator_key = modal_key(items, CREATOR_KEYS)
    pool_key = modal_key(items, POOL_KEYS)
    ident.mint_key = mint_key or "none"
    ident.creator_key = creator_key or "none"
    ident.pool_key = pool_key or "none"
    mints: list[str] = []
    creators: list[str] = []
    for item in items:
        mint = item.get(mint_key) if mint_key else None
        creator = item.get(creator_key) if creator_key else None
        if isinstance(creator, str) and MINT_RE.match(creator):
            creators.append(creator)
        if not (isinstance(mint, str) and MINT_RE.match(mint)):
            continue
        mints.append(mint)
        pool = first_valid(item, POOL_KEYS)
        if pool is not None and pool[1] != mint:
            ident.pools.setdefault(mint, pool)
    ident.mints = list(dict.fromkeys(mints))
    ident.creators = list(dict.fromkeys(creators))
    return ident


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
class Analysis:
    """Analysed list-like response."""

    items: list[dict[str, Any]]
    shape: Shape
    report: dict[str, Any]
    ident: Ident


ERR_MSG_RE = re.compile(r"[ -~]+")
ETAG_RE = re.compile(r"[ -~]{1,200}")
COMPLETION_SUBS = ("complete", "migrat", "graduat", "finish", "launched", "done", "listed")
OMIT_SEGMENTS = {
    "extensions",
    "tags",
    "logoURI",
    "rewardDefaultInfos",
    "rewardDefaultPoolInfos",
    "tips",
}
DIGEST_MAX_LINES = 350
HARD_REASONS = {"401", "403", "429", "challenge", "html"}
GROUP_ORDER = ("lastTrade", "hotToken", "marketCap", "new")
GROUP_SHORT = {"lastTrade": "lt", "hotToken": "hot", "marketCap": "mc", "new": "new"}
TRADER_KEYS = ("trader", "owner", "maker", "wallet", "user", "signer", "account")
POOL_NUM_PATHS = [
    "tvl", "lpPrice", "burnPercent", "config.tradeFeeRate", "config.protocolFeeRate",
    "config.fundFeeRate", "config.creatorFeeRate", "mintAmountA", "mintAmountB",
    *[
        f"{p}.{m}"
        for p in ("day", "week", "month")
        for m in ("apr", "feeApr", "volume", "volumeFee", "volumeQuote")
    ],
]  # fmt: skip


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


def get_path(item: dict[str, Any], path: str) -> Any:
    """Follow a dotted path through nested dicts; None if absent."""
    node: Any = item
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def truthy(value: Any) -> bool:
    """Truthiness for a flag that may be a bool, number or string."""
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no", "none", "null")
    return bool(value)


def item_flag(item: dict[str, Any]) -> bool | None:
    """Completion-like boolean of an item: True/False, or None if no flag exists."""
    flags: list[bool] = []

    def scan(node: dict[str, Any], depth: int) -> None:
        for key, child in node.items():
            if isinstance(child, bool) and any(s in key.lower() for s in COMPLETION_SUBS):
                flags.append(child)
            elif isinstance(child, dict) and depth < 4:
                scan(child, depth + 1)

    scan(item, 0)
    return any(flags) if flags else None


def has_nonnull_key(node: Any, sub: str, depth: int = 0) -> bool:
    """Whether any dict key containing `sub` holds a non-null value."""
    if depth > 5 or not isinstance(node, dict):
        return False
    for key, child in node.items():
        if sub in key.lower() and child is not None:
            return True
        if isinstance(child, dict) and has_nonnull_key(child, sub, depth + 1):
            return True
    return False


def time_metrics(items: list[dict[str, Any]], recv_s: float) -> dict[str, Any]:
    """Newest/oldest item age and covered span in hours."""
    times = [t[1] for t in (first_time(i) for i in items) if t]
    if not times:
        return {}
    return {
        "newest_age_h": sig((recv_s - max(times)) / 3600.0),
        "oldest_age_h": sig((recv_s - min(times)) / 3600.0),
        "span_h": sig((max(times) - min(times)) / 3600.0),
    }


def trader_set(items: list[dict[str, Any]]) -> set[str]:
    """Distinct trader pubkeys (kept in memory only; callers print counts)."""
    out: set[str] = set()
    for item in items:
        found = first_valid(item, TRADER_KEYS)
        if found:
            out.add(found[1])
    return out


def survey_pools(entries: list[tuple[dict[str, Any], float]]) -> dict[str, Any]:
    """Numeric stats and counters over a set of v3 pool items."""
    out: dict[str, Any] = {"pools": len(entries)}
    nums: dict[str, Any] = {}
    for path in POOL_NUM_PATHS:
        vals = [float(v) for v in (get_path(i, path) for i, _ in entries) if is_num(v)]
        if vals:
            nums[path] = {"n": len(vals), "min_med_max": stats3(vals)}
    out["numeric"] = nums
    ages = []
    for item, recv in entries:
        parsed = to_epoch(item.get("openTime"))
        if parsed is not None:
            ages.append((recv - parsed[0]) / 3600.0)
    out["age_h_from_openTime"] = {"n": len(ages), "min_med_max": stats3(ages)} if ages else None

    def has_reward(item: dict[str, Any]) -> bool:
        for window in ("day", "week", "month"):
            rewards = get_path(item, f"{window}.rewardApr")
            if isinstance(rewards, list) and rewards:
                return True
        return False

    def has_fee_ext(item: dict[str, Any]) -> bool:
        for key in ("mintA", "mintB"):
            if get_path(item, f"{key}.extensions.feeConfig") is not None:
                return True
        return False

    n = len(entries)
    rewards = sum(1 for i, _ in entries if has_reward(i))
    out["rewardApr_nonempty"] = {"count": rewards, "share": sig(rewards / n) if n else None}
    out["mint_with_extensions_feeConfig"] = sum(1 for i, _ in entries if has_fee_ext(i))
    return out


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


@dataclass
class HistCall:
    """Outcome of one history call (pool id first, mint as single fallback)."""

    resp: Resp | None
    id_type: str
    fallback: bool
    pool_key: str
    used_id: str | None


class Probe:
    """Runs the phases, owns request pacing, caps and per-host blocking."""

    def __init__(self, args: argparse.Namespace, bases: dict[str, str], capture: CaptureWriter):
        self.args = args
        self.bases = bases
        self.capture = capture
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
        self.groups: dict[str, list[str]] = {g: [] for g in GROUP_ORDER}
        self.launch_items: dict[str, list[dict[str, Any]]] = {}
        self.launch_ident: dict[str, Ident] = {}
        self.hist_sample: list[str] = []
        self.pool_for: dict[str, tuple[str, str]] = {}
        self.first_list_items: list[dict[str, Any]] = []
        self.control_mints: list[str] = []
        self.control_counts = [0, 0]
        self.join_counts: dict[str, list[int]] = {g: [0, 0] for g in GROUP_ORDER}
        self.launch_pool_totals = [0, 0]
        self.etag_results: dict[str, str] = {h: "not_run" for h in HOSTS}
        self.limits_outcome: str | None = None
        self.limits_ran_hosts = 0

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
        if host in self.blocked:
            return
        self.blocked[host] = {
            "phase": phase,
            "status": res.status,
            "reason": reason,
            "headers": allowed_headers(res.headers),
        }
        if phase == "limits":
            self.limits_blocked.add(host)

    def record(self, host: str, phase: str, path: str, url: str, res: ReqResult) -> str | None:
        """Fold one result into stats/capture; apply the stop rules.

        Returns a reason string for any stop-worthy response (429/403/401,
        challenge, HTML, 5xx). Only the hard reasons, or 5xx on two different
        paths of one host, mark the host blocked; a lone 5xx fails the endpoint.
        """
        self.total += 1
        self.last_done = time.monotonic()
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
            self.ep[(host, path)].items.append(len(items_of(data)))
        return Resp(res, data, res.wall_ns / 1e9)

    # ---- analysis ----

    def analyze(self, r: Resp) -> Analysis:
        """Walk a response: items, shape, identity, time lags."""
        report: dict[str, Any] = {
            "status": r.status,
            "latency_ms": round(r.res.latency_ms, 1),
            "bytes": r.res.size,
        }
        if not r.ok:
            return Analysis([], Shape(), report, Ident())
        items = items_of(r.data)
        shape = Shape(recv_s=r.recv_s)
        for doc in items or [payload(r.data)]:
            shape.add(doc)
        ident = identity(items)
        report["items"] = len(items)
        times = [t for t in (first_time(i) for i in items) if t]
        if times:
            report["time_key"] = safe_key(times[0][0])
            report["time_kind"] = times[0][2]
            report["newest_item_lag_s"] = round(r.recv_s - max(t[1] for t in times), 3)
            report["oldest_item_lag_s"] = round(r.recv_s - min(t[1] for t in times), 3)
        if items:
            report["identity"] = ident.keys_report()
        report["shape"] = shape.render()
        return Analysis(items, shape, report, ident)

    # ---- phase: launch_mint ----

    def phase_launch_mint(self) -> None:
        """LaunchLab Mint API: lists, continuity of sort=new, samples, creators."""
        host = "launch_mint"
        rep: dict[str, Any] = {}
        self.reports["launch_mint"] = rep
        eps: dict[str, Any] = {}
        rep["endpoints"] = eps
        analyses: dict[str, Analysis] = {}
        calls: list[tuple[str, str, dict[str, str] | None]] = [
            ("list_new", "/get/list", {"sort": "new"}),
            ("list_marketCap", "/get/list", {"sort": "marketCap"}),
            ("list_hotToken", "/get/list", {"sort": "hotToken"}),
            ("list_lastTrade", "/get/list", {"sort": "lastTrade"}),
            ("list_bonk_custom", "/get/list-bonk-custom", None),
            ("random_index_left_mint", "/get/random/index-left-mint", None),
            ("search", "/get/search", {"text": "sol"}),
        ]
        for name, path, params in calls:
            r = self.get(host, "launch_mint", path, params)
            if r is None:
                if self.is_down(host):
                    break
                continue
            analyses[name] = self.analyze(r)
            eps[name] = analyses[name].report
            self.pool_for.update(analyses[name].ident.pools)
        for name in ("list_new", "list_lastTrade"):
            if name in analyses and "shape" in analyses[name].report:
                self.digest_shapes.append((f"launch {name} item", analyses[name].report["shape"]))

        per_group = min(self.args.sample_mints, 6)
        used: set[str] = set()
        for group in GROUP_ORDER:
            a = analyses.get(f"list_{group}")
            if a is None:
                continue
            self.launch_items[group] = a.items
            self.launch_ident[group] = a.ident
            picked = [m for m in a.ident.mints if m not in used][:per_group]
            used.update(picked)
            self.groups[group] = picked
        self.hist_sample = (self.groups["lastTrade"] + self.groups["hotToken"])[
            : self.args.history_mints
        ]
        rep["sample"] = {g: len(m) for g, m in self.groups.items()}
        if self.is_down(host):
            return

        rep["continuity_new"] = self.continuity()

        ids = self.groups["lastTrade"][:5] + self.groups["new"][:2]
        creators: list[str] = []
        if ids and not self.is_down(host):
            r = self.get(host, "launch_mint", "/get/by/mints", {"ids": ",".join(ids)})
            if r is not None:
                a = self.analyze(r)
                interesting = interesting_subset(a.report.pop("shape", {"docs": 0, "fields": {}}))
                a.report["interesting"] = interesting
                eps["by_mints"] = a.report
                self.digest_shapes.append(("launch by_mints (interesting paths)", interesting))
                self.pool_for.update(a.ident.pools)
                creators = a.ident.creators
        if not creators:
            for group in GROUP_ORDER:
                ident = self.launch_ident.get(group)
                if ident:
                    creators.extend(ident.creators)
        creators = list(dict.fromkeys(creators))[:3]
        if creators and not self.is_down(host):
            walk = Shape(recv_s=None)
            statuses: list[int | None] = []
            for wallet in creators:
                r = self.get(host, "launch_mint", "/get-by-user/stats/create", {"wallet": wallet})
                if r is None:
                    break
                statuses.append(r.status)
                if r.ok:
                    walk.add(payload(r.data))
            eps["creator_stats"] = {
                "requests": len(statuses),
                "statuses": statuses,
                "shape": walk.render(),
            }

    def continuity(self) -> dict[str, Any]:
        """Poll /get/list?sort=new and measure freshness and overlap."""
        host = "launch_mint"
        polls: list[dict[str, Any]] = []
        overlaps: list[int | None] = []
        seen: set[str] = set()
        prev: set[str] | None = None
        zero = 0
        descending = True
        order_key: str | None = None
        order_kind: str | None = None
        times_by_mint: dict[str, float] = {}
        start = time.monotonic()
        for i in range(self.args.new_polls):
            if i > 0:
                self.sleep_until(start + i * self.args.new_interval)
            r = self.get(host, "launch_mint", "/get/list", {"sort": "new"})
            if r is None:
                break
            if not r.ok:
                polls.append({"status": r.status})
                continue
            items = items_of(r.data)
            ident = identity(items)
            mints = set(ident.mints)
            overlap = len(mints & prev) if prev is not None else None
            if overlap == 0:
                zero += 1
            overlaps.append(overlap)
            times = [first_time(it) for it in items]
            valid_times = [t for t in times if t]
            if valid_times and order_key is None:
                order_key, order_kind = safe_key(valid_times[0][0]), valid_times[0][2]
            for a, b in itertools.pairwise(valid_times):
                if b[1] > a[1] + 1e-6:
                    descending = False
            for item, t in zip(items, times, strict=True):
                mint = item.get(ident.mint_key)
                if t and isinstance(mint, str) and mint not in times_by_mint and MINT_RE.match(mint):
                    times_by_mint[mint] = t[1]
            polls.append(
                {
                    "status": r.status,
                    "items": len(items),
                    "new_items": len(mints - seen),
                    "overlap_with_previous": overlap,
                    "newest_lag_s": round(r.recv_s - max(t[1] for t in valid_times), 3)
                    if valid_times
                    else None,
                }
            )
            seen |= mints
            prev = mints
        rate = None
        if len(times_by_mint) >= 2:
            span = max(times_by_mint.values()) - min(times_by_mint.values())
            if span > 0:
                rate = sig((len(times_by_mint) - 1) / span)
        return {
            "polls": polls,
            "overlap_per_poll": overlaps,
            "zero_overlap_polls": zero,
            "descending_order": descending,
            "order_time_key": order_key,
            "order_time_kind": order_kind,
            "distinct_items": len(seen),
            "effective_item_rate_per_s": rate,
        }

    # ---- phase: launch_history ----

    def hist_call(self, mint: str, path: str, params: dict[str, str]) -> HistCall:
        """Pool id first; the mint only as ONE fallback (pool id missing or 4xx)."""
        host = "launch_history"
        pool = self.pool_for.get(mint)
        key = pool[0] if pool else "none"
        first: Resp | None = None
        if pool is not None:
            first = self.get(host, "launch_history", path, {"poolId": pool[1], **params})
            if first is None:
                return HistCall(None, "none", False, key, None)
            if first.ok and items_of(first.data):
                return HistCall(first, "poolId", False, key, pool[1])
            if not (first.status is not None and 400 <= first.status < 500):
                return HistCall(first, "none", False, key, None)
        second = self.get(host, "launch_history", path, {"poolId": mint, **params})
        if second is None:
            return HistCall(first, "none", True, key, None)
        if second.ok and items_of(second.data):
            return HistCall(second, "mint", True, key, mint)
        return HistCall(second, "none", True, key, None)

    def phase_launch_history(self) -> None:
        """LaunchLab History API: kline and trade depth."""
        host = "launch_history"
        mints = self.hist_sample
        if not mints:
            self.skips.append("launch_history_no_sample_mints")
            return
        rep: dict[str, Any] = {"calls": [], "kinds": {}}
        self.reports["launch_history"] = rep
        calls: list[dict[str, Any]] = rep["calls"]
        shapes = {k: Shape() for k in ("kline_1m", "kline_5m", "kline_15m", "trade")}
        id_counts: dict[str, Counter[str]] = {k: Counter() for k in shapes}
        trade_times: list[float] = []
        first_traders: set[str] = set()

        def run(idx: int, kind: str, path: str, params: dict[str, str]) -> HistCall:
            hc = self.hist_call(mints[idx], path, params)
            r = hc.resp
            items = items_of(r.data) if r is not None and r.ok else []
            entry: dict[str, Any] = {
                "mint_idx": idx,
                "kind": kind,
                "status": r.status if r else None,
                "items": len(items),
                "id_type": hc.id_type,
                "fallback": hc.fallback,
                "pool_key": hc.pool_key,
            }
            if r is not None and items:
                entry.update(time_metrics(items, r.recv_s))
                shapes[kind].recv_s = r.recv_s
                for it in items:
                    shapes[kind].add(it)
                if kind == "trade":
                    entry["distinct_traders"] = len(trader_set(items))
            id_counts[kind][hc.id_type] += 1
            calls.append(entry)
            return hc

        for idx, mint in enumerate(mints):
            if self.is_down(host):
                break
            run(idx, "kline_1m", "/kline", {"interval": "1m", "limit": "50"})
            if idx == 0:
                run(idx, "kline_5m", "/kline", {"interval": "5m", "limit": "100"})
                run(idx, "kline_15m", "/kline", {"interval": "15m", "limit": "500"})
            if self.is_down(host):
                break
            hc = run(idx, "trade", "/trade", {"limit": "100"})
            if idx != 0 or hc.resp is None:
                continue
            page_items = items_of(hc.resp.data) if hc.resp.ok else []
            trade_times.extend(t[1] for t in (first_time(i) for i in page_items) if t)
            first_traders |= trader_set(page_items)
            pages = [len(page_items)]
            key = find_next_key(hc.resp.data) if hc.resp.ok else None
            for _ in range(3):
                if key is None or hc.used_id is None or self.is_down(host):
                    break
                rp = self.get(
                    host, "launch_history", "/trade",
                    {"poolId": hc.used_id, "limit": "100", "nextPageKey": key},
                )  # fmt: skip
                if rp is None:
                    break
                got = items_of(rp.data) if rp.ok else []
                entry = {
                    "mint_idx": idx,
                    "kind": "trade_followup",
                    "status": rp.status,
                    "items": len(got),
                    "id_type": hc.id_type,
                    "fallback": False,
                    "pool_key": hc.pool_key,
                }
                if got:
                    entry.update(time_metrics(got, rp.recv_s))
                    shapes["trade"].recv_s = rp.recv_s
                    for it in got:
                        shapes["trade"].add(it)
                calls.append(entry)
                pages.append(len(got))
                trade_times.extend(t[1] for t in (first_time(i) for i in got) if t)
                first_traders |= trader_set(got)
                key = find_next_key(rp.data) if rp.ok else None
            rep["trade_pages_first_mint"] = {
                "fetched": len(pages),
                "items_per_page": pages,
                "more_available": key is not None,
            }
        rate = None
        if len(trade_times) >= 2 and max(trade_times) > min(trade_times):
            rate = sig(len(trade_times) / ((max(trade_times) - min(trade_times)) / 60.0))
        rep["trade_rate_per_min_first_mint"] = rate
        rep["trade_distinct_traders_first_mint"] = len(first_traders)
        for kind, shape in shapes.items():
            rep["kinds"][kind] = {
                "id_type_counts": dict(id_counts[kind]),
                "shape": shape.render(),
            }
            self.digest_shapes.append((f"history {kind} item", rep["kinds"][kind]["shape"]))

    # ---- phase: v3_basic ----

    def phase_v3_basic(self) -> None:
        """/main/info twice, 2 s apart."""
        host = "v3"
        rows: list[Resp] = []
        for i in range(2):
            if i > 0:
                time.sleep(2.0)
            r = self.get(host, "v3_basic", "/main/info")
            if r is None:
                break
            rows.append(r)
        if not rows:
            return
        polls = []
        for r in rows:
            body = payload(r.data) if r.ok else None
            body = body if isinstance(body, dict) else {}
            polls.append(
                {
                    "status": r.status,
                    "tvl": body.get("tvl") if is_num(body.get("tvl")) else None,
                    "volume24": body.get("volume24") if is_num(body.get("volume24")) else None,
                    "headers": {
                        k: v
                        for k, v in allowed_headers(r.res.headers).items()
                        if k in ("age", "cf-cache-status", "cache-control")
                    },
                }
            )
        shape = Shape()
        if rows[0].ok:
            shape.add(payload(rows[0].data))
        both = len(rows) == 2 and rows[0].ok and rows[1].ok
        self.reports["v3_basic"] = {
            "polls": polls,
            "bodies_identical_full": len(rows) == 2 and rows[0].res.body_text == rows[1].res.body_text,
            "bodies_identical_data": both and payload(rows[0].data) == payload(rows[1].data),
            "shape": shape.render(),
        }

    # ---- phase: v3_pools ----

    @staticmethod
    def pool_stats(items: list[dict[str, Any]]) -> dict[str, Any]:
        """Share of non-empty day objects and zero-tvl pools."""
        day_idx = [i for i, it in enumerate(items) if isinstance(it.get("day"), dict) and it["day"]]
        zero = sum(1 for it in items if is_num(it.get("tvl")) and it["tvl"] == 0)
        return {
            "pools": len(items),
            "day_nonempty_count": len(day_idx),
            "zero_tvl_count": zero,
            "zero_tvl_share": sig(zero / len(items)) if items else None,
        }

    def phase_v3_pools(self) -> None:
        """Pool list, ids and liquidity history endpoints."""
        host = "v3"
        rep: dict[str, Any] = {}
        self.reports["v3_pools"] = rep
        first_ids: list[str] = []
        for name, field_name in (("list_volume24h", "volume24h"), ("list_apr24h", "apr24h")):
            r = self.get(
                host, "v3_pools", "/pools/info/list-v2",
                {"size": "20", "sortField": field_name, "sortType": "desc"},
            )  # fmt: skip
            if r is None:
                if self.is_down(host):
                    return
                continue
            a = self.analyze(r)
            if r.ok:
                a.report["meta"] = meta_fields(r.data, ("count", "hasNextPage", "nextPageId"))
                a.report["pool_stats"] = self.pool_stats(a.items)
                if not first_ids:
                    first_ids = [
                        i["id"] for i in a.items if isinstance(i.get("id"), str) and MINT_RE.match(i["id"])
                    ]
                    self.first_list_items = a.items
                    self.digest_shapes.append(("v3 list-v2 pool item", a.report["shape"]))
            rep[name] = a.report
        if first_ids:
            r = self.get(host, "v3_pools", "/pools/info/ids", {"ids": ",".join(first_ids[:3])})
            if r is not None:
                a = self.analyze(r)
                if r.ok:
                    a.report["pool_stats"] = self.pool_stats(a.items)
                rep["ids"] = a.report
            r = self.get(host, "v3_pools", "/pools/line/liquidity", {"id": first_ids[0]})
            if r is not None:
                a = self.analyze(r)
                if r.ok:
                    a.report["meta"] = meta_fields(r.data, ("count",))
                    ts = sorted({t[1] for t in (first_time(i) for i in a.items) if t})
                    if len(ts) >= 2:
                        a.report["span_days"] = sig((ts[-1] - ts[0]) / 86400.0)
                        a.report["update_interval_s_min_med_max"] = stats3(
                            [b - a_ for a_, b in itertools.pairwise(ts)]
                        )
                rep["line_liquidity"] = a.report

    # ---- phase: v3_launch_pools ----

    def phase_v3_launch_pools(self) -> None:
        """Paginate list-v2 (Standard pools) and survey launchMigratePool pools."""
        host = "v3"
        rep: dict[str, Any] = {}
        self.reports["v3_launch_pools"] = rep
        for sort_name, pages in (("volume24h", self.args.pool_pages), ("apr24h", 2)):
            entries: list[tuple[dict[str, Any], float]] = []
            per_page: list[dict[str, Any]] = []
            next_id: str | None = None
            for _ in range(pages):
                params = {
                    "size": "100", "poolType": "Standard",
                    "sortField": sort_name, "sortType": "desc",
                }  # fmt: skip
                if next_id:
                    params["nextPageId"] = next_id
                r = self.get(host, "v3_launch_pools", "/pools/info/list-v2", params)
                if r is None:
                    break
                if not r.ok:
                    per_page.append({"status": r.status})
                    break
                items = items_of(r.data)
                per_page.append({"status": r.status, "items": len(items)})
                entries.extend((i, r.recv_s) for i in items)
                next_id = find_next_key(r.data, "nextPageId")
                if not items or next_id is None:
                    break
            migrated = [(i, t) for i, t in entries if truthy(i.get("launchMigratePool"))]
            flag_types = Counter(type_name(i.get("launchMigratePool")) for i, _ in entries)
            flag_bools = Counter(
                str(i["launchMigratePool"]).lower()
                for i, _ in entries
                if isinstance(i.get("launchMigratePool"), bool)
            )
            flag_strs = Counter(
                i["launchMigratePool"]
                for i, _ in entries
                if isinstance(i.get("launchMigratePool"), str)
            )
            enum_ok = (
                0 < len(flag_strs) <= MAX_ENUM_VALUES
                and all(len(s) <= 24 and ENUM_RE.match(s) for s in flag_strs)
            )
            self.launch_pool_totals[0] += len(migrated)
            self.launch_pool_totals[1] += len(entries)
            rep[sort_name] = {
                "pages": per_page,
                "pools": len(entries),
                "launchMigratePool_types": dict(flag_types),
                "launchMigratePool_bool_counts": dict(flag_bools),
                "launchMigratePool_enum": dict(flag_strs) if enum_ok else None,
                "migrated_pools": len(migrated),
                "migrated_share": sig(len(migrated) / len(entries)) if entries else None,
                "all": survey_pools(entries),
                "migrated": survey_pools(migrated),
            }

    # ---- phase: v3_join ----

    def pool_count(self, mint: str, cache: dict[str, int | None], shape: Shape) -> int | None:
        """/pools/info/mint for one mint (cached); None if not answered."""
        if mint in cache:
            return cache[mint]
        r = self.get(
            "v3", "v3_join", "/pools/info/mint",
            {
                "mint1": mint, "poolType": "all", "poolSortField": "liquidity",
                "sortType": "desc", "pageSize": "5", "page": "1",
            },
        )  # fmt: skip
        if r is None:
            return None
        items = items_of(r.data) if r.ok else []
        if r.ok:
            shape.recv_s = r.recv_s
            for it in items:
                shape.add(it)
        cache[mint] = len(items) if r.ok else None
        return cache[mint]

    def price_count(self, mints: list[str]) -> tuple[int, int, dict[str, Any]]:
        """One /mint/price request: (priced, asked, prices keyed by mint)."""
        if not mints:
            return 0, 0, {}
        r = self.get("v3", "v3_join", "/mint/price", {"mints": ",".join(mints)})
        if r is None or not r.ok:
            return 0, len(mints), {}
        body = payload(r.data)
        values = body if isinstance(body, dict) else {}
        priced = sum(1 for m in mints if values.get(m) is not None)
        return priced, len(mints), values

    def phase_v3_join(self) -> None:
        """Join LaunchLab tokens to API v3 pools, with diagnostics and a control."""
        host = "v3"
        rep: dict[str, Any] = {"groups": {}}
        self.reports["v3_join"] = rep
        cache: dict[str, int | None] = {}
        join_shape = Shape()
        if any(self.groups.values()):
            for group in GROUP_ORDER:
                if self.is_down(host):
                    break
                items = self.launch_items.get(group, [])
                ident = self.launch_ident.get(group, Ident())
                diag = Shape()
                for it in items:
                    diag.add(it)
                completion = {
                    p: v
                    for p, v in diag.bools.items()
                    if any(s in p.rsplit(".", 1)[-1].lower() for s in COMPLETION_SUBS)
                }
                enums = {
                    p: dict(c)
                    for p, c in diag.enums.items()
                    if p not in diag.not_enum
                    and any(s in p.rsplit(".", 1)[-1].lower() for s in ("status", "platform"))
                }
                flagged: list[str] = []
                for it in items:
                    mint = it.get(ident.mint_key)
                    if item_flag(it) and isinstance(mint, str) and MINT_RE.match(mint):
                        flagged.append(mint)
                flagged = list(dict.fromkeys(flagged))[:8]
                queried = list(dict.fromkeys(flagged + self.groups[group][:4]))
                with_pools = 0
                asked = 0
                flagged_with = 0
                flagged_asked = 0
                for mint in queried:
                    n = self.pool_count(mint, cache, join_shape)
                    if n is None:
                        continue
                    asked += 1
                    with_pools += 1 if n > 0 else 0
                    if mint in flagged:
                        flagged_asked += 1
                        flagged_with += 1 if n > 0 else 0
                priced, price_asked, _ = self.price_count(queried[:12]) if asked else (0, 0, {})
                self.join_counts[group] = [with_pools, asked]
                rep["groups"][group] = {
                    "keys": ident.keys_report(),
                    "items": len(items),
                    "completion_flags_true_false": completion,
                    "status_platform_enums": enums,
                    "migrate_nonnull_items": sum(1 for it in items if has_nonnull_key(it, "migrate")),
                    "pools_for_launch_mints": f"{with_pools}/{asked}",
                    "flagged_with_pools": f"{flagged_with}/{flagged_asked}",
                    "priced": f"{priced}/{price_asked}",
                }
        else:
            self.skips.append("v3_join_no_sample_mints")
        rep["pool_item_shape_docs"] = join_shape.docs
        if join_shape.docs:
            self.digest_shapes.append(("v3 pools-by-mint item", join_shape.render()))
        self.control(rep, cache, join_shape)

    def control(self, rep: dict[str, Any], cache: dict[str, int | None], shape: Shape) -> None:
        """Positive control: mints from the first list-v2 result."""
        if self.is_down("v3"):
            return
        if not self.first_list_items:
            r = self.get(
                "v3", "v3_join", "/pools/info/list-v2",
                {"size": "20", "sortField": "volume24h", "sortType": "desc"},
            )  # fmt: skip
            if r is not None and r.ok:
                self.first_list_items = items_of(r.data)
        appearances: Counter[str] = Counter()
        order: list[str] = []
        for pool in self.first_list_items:
            seen_here: set[str] = set()
            for key in ("mintA", "mintB"):
                addr = get_path(pool, f"{key}.address")
                if isinstance(addr, str) and MINT_RE.match(addr) and addr not in seen_here:
                    seen_here.add(addr)
                    appearances[addr] += 1
                    order.append(addr)
        candidates = [
            a for a in dict.fromkeys(order) if a != SOL_MINT and appearances[a] < 3
        ][:5]
        with_pools = 0
        asked = 0
        for mint in candidates:
            n = self.pool_count(mint, cache, shape)
            if n is None:
                continue
            asked += 1
            with_pools += 1 if n > 0 else 0
        priced, price_asked, _ = self.price_count(candidates) if candidates else (0, 0, {})
        self.control_mints = candidates
        self.control_counts = [with_pools, asked]
        rep["control"] = {
            "candidates": len(candidates),
            "pools_for_control_mints": f"{with_pools}/{asked}",
            "priced": f"{priced}/{price_asked}",
        }

    # ---- phase: freshness ----

    def phase_freshness(self) -> None:
        """Cache behaviour of /mint/price and /main/info over time."""
        host = "v3"
        rep: dict[str, Any] = {}
        self.reports["freshness"] = rep
        mints = [SOL_MINT] + self.control_mints[:1]
        labels = ["SOL"] + ["control"] * (len(mints) - 1)
        series: dict[str, list[tuple[float, Any]]] = {label: [] for label in labels}
        ages: list[str | None] = []
        cache_controls: list[str | None] = []
        first_recv: float | None = None
        start = time.monotonic()
        for i in range(self.args.freshness_polls):
            if i > 0:
                self.sleep_until(start + i * self.args.freshness_interval)
            r = self.get(host, "freshness", "/mint/price", {"mints": ",".join(mints)})
            if r is None:
                break
            first_recv = first_recv if first_recv is not None else r.recv_s
            body = payload(r.data) if r.ok else None
            values = body if isinstance(body, dict) else {}
            hdr = allowed_headers(r.res.headers)
            ages.append(hdr.get("age"))
            cache_controls.append(hdr.get("cache-control"))
            for label, mint in zip(labels, mints, strict=True):
                try:
                    price = float(values[mint]) if values.get(mint) is not None else None
                except (TypeError, ValueError):
                    price = None
                series[label].append((r.recv_s - first_recv, price))
        per_mint: dict[str, Any] = {}
        for label, rows in series.items():
            changes: list[float] = []
            prev: float | None = None
            for t, price in rows:
                if price is None:
                    continue
                if prev is not None and price != prev:
                    changes.append(t)
                prev = price
            intervals = [b - a for a, b in itertools.pairwise(changes)]
            per_mint[label] = {
                "polls": len(rows),
                "priced_polls": sum(1 for _, p in rows if p is not None),
                "distinct_prices": len({p for _, p in rows if p is not None}),
                "change_times_s": [round(c, 2) for c in changes],
                "median_change_interval_s": round(statistics.median(intervals), 2) if intervals else None,
            }
        rep["mint_price"] = {
            "per_mint": per_mint,
            "age_per_poll": ages,
            "cache_control_per_poll": cache_controls,
        }
        if self.is_down(host):
            return
        info_rows: list[dict[str, Any]] = []
        start = time.monotonic()
        for i in range(self.args.maininfo_polls):
            if i > 0:
                self.sleep_until(start + i * self.args.maininfo_interval)
            r = self.get(host, "freshness", "/main/info")
            if r is None:
                break
            body = payload(r.data) if r.ok else None
            body = body if isinstance(body, dict) else {}
            hdr = allowed_headers(r.res.headers)
            info_rows.append(
                {
                    "tvl": body.get("tvl") if is_num(body.get("tvl")) else None,
                    "volume24": body.get("volume24") if is_num(body.get("volume24")) else None,
                    "age": hdr.get("age"),
                    "cache_control": hdr.get("cache-control"),
                }
            )
        rep["main_info"] = {
            "polls": info_rows,
            "tvl_changed": len({r["tvl"] for r in info_rows}) > 1,
            "volume24_changed": len({r["volume24"] for r in info_rows}) > 1,
        }

    # ---- phase: etag ----

    def phase_etag(self) -> None:
        """Conditional GET (If-None-Match) on one cheap endpoint per host."""
        rep: dict[str, Any] = {}
        self.reports["etag"] = rep
        targets: dict[str, tuple[str, dict[str, str] | None]] = {
            "v3": ("/main/info", None),
            "launch_mint": ("/get/list", {"sort": "new"}),
        }
        first_pool = self.pool_for.get(self.hist_sample[0]) if self.hist_sample else None
        if first_pool is not None:
            targets["launch_history"] = (
                "/kline",
                {"poolId": first_pool[1], "interval": "1m", "limit": "5"},
            )
        for host in HOSTS:
            if host not in targets or self.is_down(host):
                rep[host] = {"conditional_ok": "not_run"}
                continue
            path, params = targets[host]
            r1 = self.get(host, "etag", path, params)
            if r1 is None:
                rep[host] = {"conditional_ok": "not_run"}
                continue
            etag = r1.res.headers.get("etag")
            entry: dict[str, Any] = {
                "etag_present": etag is not None,
                "first": {"status": r1.status, "latency_ms": round(r1.res.latency_ms, 1), "bytes": r1.res.size},
            }
            if etag is None or not ETAG_RE.fullmatch(etag):
                entry["conditional_ok"] = "no_etag"
                self.etag_results[host] = "no_etag"
            else:
                r2 = self.get(host, "etag", path, params, {"If-None-Match": etag})
                if r2 is None:
                    entry["conditional_ok"] = "not_run"
                else:
                    entry["second"] = {
                        "status": r2.status,
                        "latency_ms": round(r2.res.latency_ms, 1),
                        "bytes": r2.res.size,
                    }
                    ok = r2.status == 304
                    entry["conditional_ok"] = "true" if ok else "false"
                    self.etag_results[host] = "ok" if ok else "fail"
            rep[host] = entry

    # ---- phase: limits ----

    def phase_limits(self) -> None:
        """Bounded, no-ramp burst probe per host (5 concurrent, then 4 singles)."""
        rep: dict[str, Any] = {}
        self.reports["limits"] = rep
        for host in ("v3", "launch_mint", "launch_history"):
            path, params = {
                "v3": ("/main/info", None),
                "launch_mint": ("/get/list", {"sort": "new"}),
                "launch_history": ("/kline", None),
            }[host]
            if host == "launch_history":
                first = self.pool_for.get(self.hist_sample[0]) if self.hist_sample else None
                if first is None:
                    rep[host] = {"skipped": "no_sample_pool_id"}
                    continue
                params = {"poolId": first[1], "interval": "1m", "limit": "5"}
            if self.is_down(host):
                rep[host] = {"skipped": "host_down"}
                continue
            known = self.ep.get((host, path))
            if known is not None and known.failed:
                rep[host] = {"skipped": "endpoint_failed"}
                continue
            if self.total + LIMITS_HOST_CAP > REQUEST_CAP:
                self.cap_hit = True
                rep[host] = {"skipped": "request_cap"}
                continue
            self.limits_ran_hosts += 1
            rep[host] = self.limits_for_host(host, path, params)

    def limits_for_host(
        self, host: str, path: str, params: dict[str, str] | None
    ) -> dict[str, Any]:
        """One host's burst + offset singles; stops at the first stop-worthy response."""
        self.pace(LIMITS_HOST_GAP_S)
        url = self.url(host, path, params)
        sequence: list[dict[str, Any]] = []
        results: list[ReqResult | None] = [None] * LIMITS_BURST
        barrier = threading.Barrier(LIMITS_BURST)

        def worker(idx: int) -> None:
            barrier.wait()
            results[idx] = http_get(url)

        burst_start = time.perf_counter()
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(LIMITS_BURST)]
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
            for offset in LIMITS_OFFSETS_S:
                remaining = burst_start + offset - time.perf_counter()
                if remaining > 0:
                    time.sleep(remaining)
                r = self.get(host, "limits", path, params)
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
            self.limits_outcome = f"429@{host}" if stop == "429" else f"blocked@{host}"
        return {"sequence": sequence, "stopped": stop}


# --------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------


def fmt3(values: list[Any]) -> str:
    """Format a [min, med, max] list as a/b/c."""
    return "/".join(f"{v:g}" if isinstance(v, (int, float)) else str(v) for v in values)


def shape_lines(rendered: dict[str, Any]) -> list[str]:
    """One line per leaf path: 'path | types | stats'."""
    lines: list[str] = []
    omitted: set[str] = set()
    for path, e in rendered.get("fields", {}).items():
        parts = path.split(".")
        idx = next((i for i, s in enumerate(parts) if s.replace("[]", "") in OMIT_SEGMENTS), None)
        if idx is not None:
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
        if "pubkeys_distinct" in e:
            stats.append(f"pubkeys_distinct={e['pubkeys_distinct']}")
        if "program_ids" in e:
            stats.append("programIds=" + ",".join(f"{k}:{v}" for k, v in e["program_ids"].items()))
        if "program_ids_distinct" in e:
            stats.append(f"programIds_distinct={e['program_ids_distinct']}")
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
            f" | latency_ms min/med/max {fmt3(s['latency_ms_min_med_max'] or [])}"
        )
        fixed.append(f"HOST {host} | headers: {' '.join(s['header_names'])}")
        fixed.append(
            f"HOST {host} | values: "
            + "; ".join(f"{k}={v}" for k, v in s["allowed_header_values"].items())
        )
    if summary["blocked_hosts"]:
        fixed.append(jline("BLOCKED", summary["blocked_hosts"]))
    for key, rec in summary["endpoints"].items():
        lat = fmt3(rec["latency_ms_min_med_max"] or [])
        fixed.append(
            f"EP {key} | codes {json.dumps(rec['status_codes'], separators=(',', ':'))}"
            f" | failed={rec['failed']} | items {rec['items_min_max']} | latency_ms {lat}"
        )
    for name, ep in rep.get("launch_mint", {}).get("endpoints", {}).items():
        if "identity" in ep:
            fixed.append(jline(f"IDENTITY {name}", ep["identity"]))
    lm = rep.get("launch_mint", {})
    if "continuity_new" in lm:
        c = lm["continuity_new"]
        fixed.append(
            jline("CONTINUITY", {k: v for k, v in c.items() if k != "polls"})
        )
        fixed.append(
            "CONTINUITY newest_lag_s per poll: "
            + " ".join(str(p.get("newest_lag_s")) for p in c["polls"])
        )
    if "sample" in lm:
        fixed.append(jline("SAMPLE sizes", lm["sample"]))
    for host_name in ("launch_history",):
        h = rep.get(host_name)
        if h:
            for call in h["calls"]:
                fixed.append(jline("HIST call", call))
            for kind, k in h["kinds"].items():
                fixed.append(jline(f"HIST id_type {kind}", k["id_type_counts"]))
            for key in ("trade_pages_first_mint", "trade_rate_per_min_first_mint",
                        "trade_distinct_traders_first_mint"):
                fixed.append(jline(f"HIST {key}", h.get(key)))
    vb = rep.get("v3_basic")
    if vb:
        fixed.append(jline("V3_BASIC", {k: v for k, v in vb.items() if k not in ("shape",)}))
    vp = rep.get("v3_pools")
    if vp:
        for name, v in vp.items():
            fixed.append(
                jline(
                    f"V3_POOLS {name}",
                    {k: v[k] for k in ("status", "items", "meta", "pool_stats", "span_days",
                                       "update_interval_s_min_med_max") if k in v},
                )
            )
    vj = rep.get("v3_join")
    if vj:
        for g, d in vj.get("groups", {}).items():
            fixed.append(jline(f"JOIN {g}", d))
        fixed.append(jline("JOIN control", vj.get("control")))
    lp = rep.get("v3_launch_pools")
    if lp:
        for sort_name, d in lp.items():
            top = {k: v for k, v in d.items() if k not in ("all", "migrated")}
            fixed.append(jline(f"LAUNCH_POOLS {sort_name}", top))
            for subset in ("all", "migrated"):
                sub = d[subset]
                fixed.append(
                    f"LAUNCH_POOLS {sort_name} {subset} | pools={sub['pools']}"
                    f" age_h={sub['age_h_from_openTime']}"
                    f" rewardApr_nonempty={sub['rewardApr_nonempty']}"
                    f" feeConfig_pools={sub['mint_with_extensions_feeConfig']}"
                )
                for path, v in sub["numeric"].items():
                    fixed.append(f"LAUNCH_POOLS {sort_name} {subset} {path} | n={v['n']} {fmt3(v['min_med_max'])}")
    fr = rep.get("freshness")
    if fr:
        for label, d in fr["mint_price"]["per_mint"].items():
            fixed.append(jline(f"FRESH price {label}", d))
        fixed.append(jline("FRESH age_per_poll", fr["mint_price"]["age_per_poll"]))
        fixed.append(jline("FRESH cache_control", sorted({str(x) for x in fr["mint_price"]["cache_control_per_poll"]})))
        if "main_info" in fr:
            fixed.append(jline("FRESH main_info", fr["main_info"]))
    et = rep.get("etag")
    if et:
        for h, d in et.items():
            fixed.append(jline(f"ETAG {h}", d))
    lim = rep.get("limits")
    if lim:
        for h, d in lim.items():
            if "sequence" in d:
                seq = " ".join(f"{s['status']}@{s['offset_s']:g}" for s in d["sequence"])
                extra = {
                    k: v
                    for s in d["sequence"]
                    for k, v in s["headers"].items()
                    if k != "cf-cache-status"
                }
                fixed.append(f"LIMITS {h} | {seq} | stopped={d['stopped']} | ratelimit/retry headers {extra}")
            else:
                fixed.append(jline(f"LIMITS {h}", d))
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
    budget = DIGEST_MAX_LINES - 1  # room for the truncation note
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

    p = argparse.ArgumentParser(description="Anonymous Raydium public API probe.")
    p.add_argument("--phases", type=phases_type, default=list(PHASE_ORDER))
    p.add_argument("--launch-mint-url", default=DEFAULT_URLS["launch_mint"])
    p.add_argument("--launch-history-url", default=DEFAULT_URLS["launch_history"])
    p.add_argument("--v3-url", default=DEFAULT_URLS["v3"])
    p.add_argument("--new-polls", type=bounded_int("new-polls", 1, 20), default=8)
    p.add_argument("--new-interval", type=bounded_float("new-interval", 3, 3600), default=6.0)
    p.add_argument("--sample-mints", type=bounded_int("sample-mints", 0, 12), default=8)
    p.add_argument("--history-mints", type=bounded_int("history-mints", 0, 5), default=3)
    p.add_argument("--pool-pages", type=bounded_int("pool-pages", 1, 10), default=5)
    p.add_argument("--freshness-polls", type=bounded_int("freshness-polls", 1, 30), default=30)
    p.add_argument(
        "--freshness-interval", type=bounded_float("freshness-interval", 2, 3600), default=5.0
    )
    p.add_argument("--maininfo-polls", type=bounded_int("maininfo-polls", 1, 10), default=4)
    p.add_argument(
        "--maininfo-interval", type=bounded_float("maininfo-interval", 10, 3600), default=20.0
    )
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
    """ran / skipped / 429@host / blocked@host."""
    if "limits" not in phases or probe.limits_ran_hosts == 0:
        return "skipped"
    return probe.limits_outcome or "ran"


def main(argv: list[str] | None = None) -> int:
    """Entry point: run the requested phases and write/print results."""
    args = parse_args(argv)
    repo_root = Path(__file__).resolve().parent.parent
    bases = {
        "launch_mint": args.launch_mint_url,
        "launch_history": args.launch_history_url,
        "v3": args.v3_url,
    }
    try:
        for base in bases.values():
            validate_base_url(base)
        out_dir = resolve_out_dir(repo_root, args.out_dir)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(UTC)
    stem = f"raydium_{now.strftime('%Y%m%dT%H%M%SZ')}" + (f"_{args.label}" if args.label else "")
    capture = CaptureWriter(out_dir / f"{stem}.raw.jsonl")
    summary_path = out_dir / f"{stem}.summary.json"
    digest_path = out_dir / f"{stem}.digest.txt"
    hostnames = {h: urllib.parse.urlsplit(b).hostname for h, b in bases.items()}
    phases = [p for p in PHASE_ORDER if p in args.phases]
    print(
        f"{now.isoformat()} start hosts={json.dumps(hostnames, separators=(',', ':'))} "
        f"phases={','.join(phases)} new_polls={args.new_polls} new_interval={args.new_interval} "
        f"sample_mints={args.sample_mints} history_mints={args.history_mints} "
        f"pool_pages={args.pool_pages} freshness_polls={args.freshness_polls} "
        f"freshness_interval={args.freshness_interval} maininfo_polls={args.maininfo_polls} "
        f"maininfo_interval={args.maininfo_interval}"
    )

    probe = Probe(args, bases, capture)
    runners = {
        "launch_mint": probe.phase_launch_mint,
        "launch_history": probe.phase_launch_history,
        "v3_basic": probe.phase_v3_basic,
        "v3_pools": probe.phase_v3_pools,
        "v3_launch_pools": probe.phase_v3_launch_pools,
        "v3_join": probe.phase_v3_join,
        "freshness": probe.phase_freshness,
        "etag": probe.phase_etag,
        "limits": probe.phase_limits,
    }
    interrupted = False
    try:
        for phase in phases:
            runners[phase]()
    except KeyboardInterrupt:
        interrupted = True
    finally:
        capture.close()

    join = ",".join(
        f"{GROUP_SHORT[g]}:{probe.join_counts[g][0]}/{probe.join_counts[g][1]}" for g in GROUP_ORDER
    )
    mc_w, mc_t = probe.join_counts["marketCap"]
    new_w, new_t = probe.join_counts["new"]
    etag = ",".join(f"{h}:{probe.etag_results[h]}" for h in HOSTS)
    verdict = (
        f"VERDICT v3_ok={host_verdict(probe, 'v3')} "
        f"launch_mint_ok={host_verdict(probe, 'launch_mint')} "
        f"launch_history_ok={host_verdict(probe, 'launch_history')} "
        f"pools_for_launch_mints={mc_w}/{mc_t},{new_w}/{new_t} "
        f"join={join} control={probe.control_counts[0]}/{probe.control_counts[1]} "
        f"launch_pools={probe.launch_pool_totals[0]}/{probe.launch_pool_totals[1]} "
        f"etag={etag} limits={limits_verdict(probe, phases)}"
    )
    endpoints = {f"{h} {p}": rec.render() for (h, p), rec in sorted(probe.ep.items())}
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
