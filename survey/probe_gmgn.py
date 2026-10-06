"""Phase 1 survey: short GMGN read-only probe using the public demo key.

Structure only: field names, types, numbers and short enum values. No token text, no addresses.
Set GMGN_API_KEY in your PowerShell session first, then run:  uv run python survey/probe_gmgn.py
"""
from __future__ import annotations

import json
import os
import re
import socket
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only(*args, **kwargs):
    """GMGN rejects IPv6 clients, so resolve IPv4 addresses only."""
    res = _orig_getaddrinfo(*args, **kwargs)
    return [r for r in res if r[0] == socket.AF_INET] or res


socket.getaddrinfo = _ipv4_only

BASE = "https://openapi.gmgn.ai"
UA = "floor-mop-survey/0.1"
GAP = 1.2
B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
KEY_OK = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,40}$")
ENUM = re.compile(r"^[A-Za-z0-9_.-]{1,24}$")
CODE = re.compile(r"^[A-Za-z0-9_]{3,40}$")
UNTRUSTED_WORDS = ("name", "symbol", "url", "link", "logo", "image", "icon", "header", "twitter",
                   "telegram", "website", "instagram", "tiktok", "desc", "text", "title", "uri")
KEYWORDS = ("creator", "smart", "renowned", "rug", "bundler", "insider", "sniper", "fresh", "progress",
            "complete", "open", "rat_", "entrap", "dev_", "launchpad", "exchange", "status")
SHOW_HDR = {"content-type", "cache-control", "retry-after", "server"}
STATE: dict = {"last": 0.0, "stop": "", "requests": 0}
HDR_NAMES: set[str] = set()
HDR_VALS: dict[str, str] = {}
LINES: list[str] = []


def out(s: str) -> None:
    print(s)
    LINES.append(s)


class Stats:
    """Accumulates structure-only statistics for one field path."""

    def __init__(self) -> None:
        self.types: set[str] = set()
        self.nums: list[float] = []
        self.t = self.f = self.nulls = self.n = 0
        self.lens: list[int] = []
        self.enums: Counter = Counter()
        self.pubs: set[str] = set()

    def add(self, v: object) -> None:
        self.n += 1
        if v is None:
            self.types.add("null")
            self.nulls += 1
        elif isinstance(v, bool):
            self.types.add("bool")
            if v:
                self.t += 1
            else:
                self.f += 1
        elif isinstance(v, (int, float)):
            self.types.add("int" if isinstance(v, int) else "float")
            self.nums.append(float(v))
        elif isinstance(v, str):
            self.types.add("str")
            self.lens.append(len(v))
            if B58.match(v):
                self.pubs.add(v)
            elif ENUM.match(v):
                self.enums[v] += 1
            else:
                self.enums["<other>"] += 1
        else:
            self.types.add(type(v).__name__)


def walk(obj: object, path: str, acc: dict[str, Stats], depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(obj, dict):
        keys = list(obj)
        if len(keys) > 60 or any((not KEY_OK.match(k)) or B58.match(k) for k in keys):
            acc.setdefault(path + ".<dynamic>", Stats()).add(len(keys))
            return
        for k, v in obj.items():
            walk(v, f"{path}.{k}" if path else k, acc, depth + 1)
    elif isinstance(obj, list):
        acc.setdefault(path + "#len", Stats()).add(len(obj))
        for v in obj[:200]:
            walk(v, path + "[]", acc, depth + 1)
    else:
        acc.setdefault(path, Stats()).add(obj)


def find_items(data: object) -> list[dict]:
    """The largest list of dicts found within three levels."""
    best: list[dict] = []
    queue = [(data, 0)]
    while queue:
        cur, d = queue.pop(0)
        if isinstance(cur, list):
            dicts = [x for x in cur if isinstance(x, dict)]
            if len(dicts) > len(best):
                best = dicts
        elif isinstance(cur, dict) and d < 3:
            queue.extend((v, d + 1) for v in cur.values())
    return best


def g6(x: float) -> str:
    return f"{x:.6g}"


def untrusted(last: str) -> bool:
    low = last.lower()
    return any(w in low for w in UNTRUSTED_WORDS)


def timelike(last: str) -> bool:
    return "timestamp" in last or last.endswith(("_ts", "_at", "_time")) or last == "time"


def lag_stats(nums: list[float], now: float) -> str:
    lags = []
    for v in nums:
        if 1e12 <= v < 1e14:
            v /= 1000
        if 1e9 <= v < 1e11:
            lags.append(now - v)
    if not lags:
        return ""
    return "/".join(g6(f(lags)) for f in (min, statistics.median, max))


def describe(path: str, st: Stats, now: float) -> str:
    last = path.rsplit(".", 1)[-1].replace("[]", "").replace("#len", "")
    parts = [path, "/".join(sorted(st.types))]
    if st.nums:
        parts.append("num=" + "/".join(g6(f(st.nums)) for f in (min, statistics.median, max)))
        if timelike(last):
            lag = lag_stats(st.nums, now)
            if lag:
                parts.append("lag_s=" + lag)
    if st.t or st.f:
        parts.append(f"bool_t/f={st.t}/{st.f}")
    if st.nulls:
        parts.append(f"nulls={st.nulls}/{st.n}")
    if st.lens:
        parts.append(f"len={min(st.lens)}-{max(st.lens)}")
    if st.pubs:
        parts.append(f"pubkeys_distinct={len(st.pubs)}")
    if st.enums and not untrusted(last) and len(st.enums) <= 12 and "<other>" not in st.enums:
        parts.append("enum=" + ",".join(f"{k}:{v}" for k, v in st.enums.most_common()))
    return " | ".join(parts)


def table(acc: dict[str, Stats], cap: int = 150, known: dict | None = None,
          keywords: tuple = (), only: tuple = ()) -> None:
    now = time.time()
    shown = total = 0
    for path, st in acc.items():
        if only and not any(k in path for k in only):
            continue
        if known is not None and path in known and not any(k in path for k in keywords):
            continue
        total += 1
        if shown < cap:
            out("  " + describe(path, st, now))
            shown += 1
    if total > shown:
        out(f"  ... {total - shown} more paths")


def call(method: str, path: str, query: dict | None = None, body: object | None = None) -> dict:
    """One paced GMGN request. Never raises and never prints the key."""
    wait = GAP - (time.monotonic() - STATE["last"])
    if wait > 0:
        time.sleep(wait)
    q = dict(query or {})
    q["timestamp"] = int(time.time())
    q["client_id"] = str(uuid.uuid4())
    url = f"{BASE}{path}?{urllib.parse.urlencode(q)}"
    headers = {"X-APIKEY": os.environ["GMGN_API_KEY"], "Content-Type": "application/json",
               "Accept": "application/json", "User-Agent": UA}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    t0 = time.perf_counter()
    status, raw, hdrs, err = 0, b"", {}, None
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            status, raw = r.status, r.read()
            hdrs = {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
        hdrs = {k.lower(): v for k, v in (e.headers or {}).items()}
    except Exception as e:  # network or TLS failure  # noqa: BLE001
        err = type(e).__name__
    ms = (time.perf_counter() - t0) * 1000
    STATE["last"] = time.monotonic()
    STATE["requests"] += 1
    try:
        js = json.loads(raw) if raw else None
    except ValueError:
        js = None
    return {"status": status, "ms": ms, "json": js, "headers": hdrs, "net_error": err}


def note_headers(r: dict) -> None:
    HDR_NAMES.update(r["headers"])
    for k, v in r["headers"].items():
        if (k in SHOW_HDR or k.startswith(("x-ratelimit", "ratelimit"))) and re.fullmatch(r"[ -~]{1,80}", v or ""):
            HDR_VALS[k] = v


def why(r: dict) -> str:
    js = r["json"] if isinstance(r["json"], dict) else {}
    bits = [f"status={r['status']}"]
    if r["net_error"]:
        bits.append(f"net_error={r['net_error']}")
    code = js.get("code")
    if isinstance(code, (int, str)) and CODE.match(str(code)):
        bits.append(f"code={code}")
    e = js.get("error")
    if isinstance(e, str) and CODE.match(e):
        bits.append(f"error={e}")
    m = js.get("message")
    if isinstance(m, str) and len(m) <= 120 and re.fullmatch(r"[ -~]+", m) and "://" not in m:
        bits.append(f"message={m}")
    reset = r["headers"].get("x-ratelimit-reset")
    if reset and re.fullmatch(r"\d{1,12}", reset):
        bits.append(f"reset_in_s={int(reset) - int(time.time())}")
    return " ".join(bits)


def step(name: str, method: str, path: str, query: dict | None = None, body: object | None = None):
    """Run one request; stop the whole probe on 401, 403 or 429. Returns (ok, data)."""
    if STATE["stop"]:
        return False, None
    r = call(method, path, query, body)
    note_headers(r)
    js = r["json"]
    ok = r["status"] == 200 and isinstance(js, dict) and js.get("code") == 0
    out(f"EP {name} {'status=200' if ok else why(r)} ms={r['ms']:.0f}")
    if r["status"] in (401, 403, 429):
        STATE["stop"] = f"{name}:{r['status']}"
    return ok, (js.get("data") if ok else None)


def trenches_body(types: list[str], limit: int, extra: dict | None = None) -> dict:
    section: dict = {"filters": ["offchain", "onchain"], "launchpad_platform_v2": True, "limit": limit,
                     "quote_address_type": [4, 5, 3, 1, 13, 0]}
    section.update(extra or {})
    body: dict = {"version": "v2"}
    for t in types:
        body[t] = dict(section)
    return body


def addrs(items: list[dict]) -> list[str]:
    return [it["address"] for it in items if isinstance(it.get("address"), str) and B58.match(it["address"])]


def show(title: str, items: list[dict], cap: int = 150) -> None:
    acc: dict[str, Stats] = {}
    for it in items:
        walk(it, "", acc)
    out(f"{title} items={len(items)}")
    table(acc, cap)


def listed(data: object, key: str) -> list[dict]:
    v = data.get(key) if isinstance(data, dict) else None
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def main() -> int:
    sys.stdout.reconfigure(errors="replace")
    if not os.environ.get("GMGN_API_KEY"):
        print("GMGN_API_KEY is not set in this PowerShell window. Set it first, then rerun.")
        return 2
    t_start = datetime.now(UTC)
    out(f"START {t_start.isoformat()} host=openapi.gmgn.ai")

    ok, data = step("rank_1h_volume", "GET", "/v1/market/rank",
                    {"chain": "sol", "interval": "1h", "limit": 20, "order_by": "volume"})
    rank_items = find_items(data) if ok else []
    if rank_items:
        show("RANK", rank_items, 120)

    new_items: list[dict] = []
    pump_items: list[dict] = []
    done_items: list[dict] = []
    ok, data = step("trenches_all", "POST", "/v1/trenches", {"chain": "sol"},
                    trenches_body(["new_creation", "near_completion", "completed"], 50))
    if ok and isinstance(data, dict):
        new_items, pump_items, done_items = (listed(data, "new_creation"), listed(data, "pump"),
                                             listed(data, "completed"))
        out(f"TRENCHES counts new_creation={len(new_items)} near_completion(key pump)={len(pump_items)} "
            f"completed={len(done_items)} data_keys={sorted(k for k in data if KEY_OK.match(k))}")
        known: dict | None = None
        for title, items in (("new_creation", new_items), ("near_completion", pump_items),
                             ("completed", done_items)):
            acc: dict[str, Stats] = {}
            for it in items:
                walk(it, "", acc)
            out(f"TRENCHES {title} items={len(items)}" + ("" if known is None else " (paths not already listed)"))
            table(acc, 170 if known is None else 90, known, KEYWORDS)
            if known is None and acc:
                known = acc

    mints = addrs(new_items)[:2] + addrs(done_items)[:1]
    for kind, path in (("info", "/v1/token/info"), ("security", "/v1/token/security")):
        acc = {}
        got = 0
        for m in mints:
            ok, data = step(f"token_{kind}", "GET", path, {"chain": "sol", "address": m})
            if ok and isinstance(data, dict):
                walk(data, "", acc)
                got += 1
        out(f"TOKEN_{kind.upper()} responses={got}")
        table(acc, 120)

    pick = (addrs(done_items) or addrs(new_items) or [None])[0]
    if pick:
        ok, data = step("top_holders", "GET", "/v1/market/token_top_holders", {"chain": "sol", "address": pick})
        if ok:
            show("TOP_HOLDERS", find_items(data), 60)
        now_s = int(time.time())
        ok, data = step("kline_1m_last_hour", "GET", "/v1/market/token_kline",
                        {"chain": "sol", "address": pick, "resolution": "1m", "from": now_s - 3600, "to": now_s})
        if ok:
            show("KLINE", find_items(data), 30)

    ok, data = step("signals_smart_money_buys", "POST", "/v1/market/token_signal", None,
                    {"chain": "sol", "groups": [{"signal_type": [12]}]})
    if ok:
        show("SIGNALS", find_items(data), 60)

    creators = [it["creator"] for it in new_items + rank_items
                if isinstance(it.get("creator"), str) and B58.match(it["creator"])]
    if creators:
        ok, data = step("created_tokens", "GET", "/v1/user/created_tokens",
                        {"chain": "sol", "wallet_address": creators[0]})
        if ok:
            show("CREATED_TOKENS", find_items(data), 60)

    base_n = len(new_items)
    for label, extra in (("safe_preset_keys", {"max_rug_ratio": 0.3, "max_bundler_rate": 0.3, "max_insider_ratio": 0.3}),
                         ("creator_open_ratio_min_0.1", {"min_creator_created_open_ratio": 0.1})):
        ok, data = step(f"trenches_filter_{label}", "POST", "/v1/trenches", {"chain": "sol"},
                        trenches_body(["new_creation"], 50, extra))
        if ok:
            items = listed(data, "new_creation")
            out(f"FILTER {label} new_creation unfiltered={base_n} filtered={len(items)}")
            if label.startswith("creator") and items:
                acc = {}
                for it in items:
                    walk(it, "", acc)
                table(acc, 30, only=("creator",))

    names = " ".join(sorted(h for h in HDR_NAMES if re.fullmatch(r"[a-z0-9-]{1,40}", h)))
    out(f"HEADERS names: {names}")
    out("HEADER_VALUES " + "; ".join(f"{k}={v}" for k, v in sorted(HDR_VALUES.items())) if (HDR_VALUES := HDR_VALS) else "HEADER_VALUES none")
    out(f"REQUESTS {STATE['requests']} STOPPED {STATE['stop'] or 'no'}")
    out(f"END {datetime.now(UTC).isoformat()}")

    path = Path(__file__).resolve().parent.parent / "data" / "survey" / f"gmgn_{t_start:%Y%m%dT%H%M%SZ}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    print(f"saved {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())