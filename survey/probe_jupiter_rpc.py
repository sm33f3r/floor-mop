"""Phase 1 survey: Jupiter sellability plus public Solana RPC probe (v2).

Keyless, standard library only. Output is structure only: no token text and no addresses.
Holder wallets and concentration come from RugCheck's report, not from the public RPC.
Run: uv run python survey/probe_jupiter_rpc.py
"""
from __future__ import annotations

import json
import re
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

UA = "floor-mop-survey/0.1"
RPC_URL = "https://api.mainnet-beta.solana.com"
JUP_URL = "https://lite-api.jup.ag"
RC_URL = "https://api.rugcheck.xyz"
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
ENUM = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
CODE = re.compile(r"^[A-Z0-9_]{3,40}$")
LABEL = re.compile(r"^[A-Za-z0-9 ._()+-]{1,30}$")
TX_B64 = re.compile(r"^[A-Za-z0-9+/=]{100,6000}$")
OK_HEADERS = {"content-type", "cache-control", "age", "retry-after", "server",
              "cf-cache-status", "cf-mitigated"}
N_MINTS = 8
RPC_GAP = 0.4
JUP_GAP = 2.1
RC_GAP = 0.6
RPC_CAP = 250
JUP_CAP = 60
RC_CAP = 25

COUNT: Counter = Counter()
RPC_LAT: dict[str, list[float]] = {}
JUP_LAT: list[float] = []
RPC_ERRS: Counter = Counter()
RPC_STATS: dict[str, Counter] = {}
RPC_HDR_FIRST: dict[str, dict[str, str]] = {}
RPC_HDR_LAST: dict[str, dict[str, str]] = {}
METHOD_STOP: dict[str, str] = {}
ROUTE_LABELS: Counter = Counter()
HDR_NAMES: dict[str, set[str]] = {"rpc": set(), "jup": set(), "feed": set()}
HDR_VALUES: dict[str, dict[str, str]] = {"rpc": {}, "jup": {}, "feed": {}}
BLOCKED: dict[str, str] = {}


class Pacer:
    """Enforce a minimum gap between the end of one request and the start of the next."""

    def __init__(self, gap: float) -> None:
        self.gap = gap
        self.last = 0.0

    def wait(self) -> None:
        delay = self.gap - (time.monotonic() - self.last)
        if delay > 0:
            time.sleep(delay)

    def mark(self) -> None:
        self.last = time.monotonic()


RPC_PACER = Pacer(RPC_GAP)
JUP_PACER = Pacer(JUP_GAP)
RC_PACER = Pacer(RC_GAP)


def inhibit_sleep(on: bool) -> str:
    """Ask Windows not to sleep while the probe runs. No-op elsewhere."""
    if sys.platform != "win32":
        return "not_available"
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if on else 0x80000000)
        return "windows_execution_state"
    except Exception:
        return "failed"


def http(method: str, url: str, body: object | None = None, timeout: float = 20.0) -> dict:
    """One HTTP request with fixed, minimal headers. Never raises."""
    headers = {"Accept": "application/json", "User-Agent": UA, "Accept-Encoding": "identity"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    t0 = time.perf_counter()
    status, raw, hdrs, err = 0, b"", {}, None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
        hdrs = {k.lower(): v for k, v in (exc.headers or {}).items()}
    except Exception as exc:  # network or TLS failure
        err = type(exc).__name__
    ms = (time.perf_counter() - t0) * 1000
    try:
        js = json.loads(raw) if raw else None
    except ValueError:
        js = None
    return {"status": status, "ms": ms, "json": js, "headers": hdrs, "net_error": err}


def rl_headers(r: dict) -> dict[str, str]:
    """Rate-limit related header values, printable ones only."""
    out: dict[str, str] = {}
    for k, v in r["headers"].items():
        if k.startswith(("x-ratelimit", "x-rate-limit", "ratelimit")) or k == "retry-after":
            if re.fullmatch(r"[ -~]{1,80}", v or ""):
                out[k] = v
    return out


def note(service: str, r: dict) -> None:
    """Record header names and allowed header values. A 403 blocks a service; a 429 blocks
    every service except the RPC, where only the single method is stopped."""
    HDR_NAMES[service].update(r["headers"])
    for k, v in r["headers"].items():
        allowed = k in OK_HEADERS or k.startswith(("x-ratelimit", "x-rate-limit", "ratelimit"))
        if allowed and re.fullmatch(r"[ -~]{1,80}", v or ""):
            HDR_VALUES[service][k] = v
    if r["status"] == 403 or (r["status"] == 429 and service != "rpc"):
        BLOCKED.setdefault(service, str(r["status"]))


def track_rpc(method: str, r: dict) -> None:
    note("rpc", r)
    RPC_LAT.setdefault(method, []).append(r["ms"])
    RPC_STATS.setdefault(method, Counter())[r["status"]] += 1
    rl = rl_headers(r)
    RPC_HDR_FIRST.setdefault(method, rl)
    RPC_HDR_LAST[method] = rl
    if r["status"] == 429:
        METHOD_STOP[method] = "429"


def rpc(method: str, params: list) -> object | None:
    """Paced JSON-RPC call to the public endpoint. Returns the result or None."""
    if "rpc" in BLOCKED or method in METHOD_STOP or COUNT["rpc"] >= RPC_CAP:
        return None
    RPC_PACER.wait()
    COUNT["rpc"] += 1
    r = http("POST", RPC_URL, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    RPC_PACER.mark()
    track_rpc(method, r)
    js = r["json"] if isinstance(r["json"], dict) else {}
    if r["status"] != 200 or "error" in js:
        e = js.get("error") if isinstance(js.get("error"), dict) else {}
        code = e.get("code") if isinstance(e.get("code"), int) else None
        RPC_ERRS[(method, r["status"], code)] += 1
        return None
    return js.get("result")


def value_of(res: object) -> object | None:
    return res.get("value") if isinstance(res, dict) else None


def mint_info(mint: str) -> dict | None:
    res = rpc("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
    val = value_of(res)
    data = val.get("data") if isinstance(val, dict) else None
    parsed = data.get("parsed") if isinstance(data, dict) else None
    info = parsed.get("info") if isinstance(parsed, dict) else None
    if not isinstance(info, dict) or parsed.get("type") != "mint":
        return None
    exts = [e.get("extension") for e in (info.get("extensions") or []) if isinstance(e, dict)]
    exts = [x for x in exts if isinstance(x, str) and re.fullmatch(r"[A-Za-z0-9]{1,40}", x)]
    prog = data.get("program")
    dec = info.get("decimals")
    return {
        "program": prog if prog in ("spl-token", "spl-token-2022") else "other",
        "decimals": dec if isinstance(dec, int) else 6,
        "mint_auth": bool(info.get("mintAuthority")),
        "freeze_auth": bool(info.get("freezeAuthority")),
        "exts": exts,
    }


def token_account_frozen(addr: str) -> bool | None:
    val = value_of(rpc("getAccountInfo", [addr, {"encoding": "jsonParsed", "commitment": "confirmed"}]))
    data = val.get("data") if isinstance(val, dict) else None
    parsed = data.get("parsed") if isinstance(data, dict) else None
    info = parsed.get("info") if isinstance(parsed, dict) else None
    if not isinstance(info, dict):
        return None
    return info.get("state") == "frozen"


def wallet_like(owner: str) -> bool:
    """True when the owner is a plain system-owned wallet with some SOL."""
    v = value_of(rpc("getAccountInfo", [owner, {"encoding": "base64", "commitment": "confirmed",
                                                 "dataSlice": {"offset": 0, "length": 0}}]))
    return (isinstance(v, dict) and v.get("owner") == SYSTEM_PROGRAM
            and not v.get("executable") and (v.get("lamports") or 0) > 0)


def rc_report(mint: str) -> dict | None:
    if "feed" in BLOCKED or COUNT["rc"] >= RC_CAP:
        return None
    RC_PACER.wait()
    COUNT["rc"] += 1
    r = http("GET", f"{RC_URL}/v1/tokens/{mint}/report")
    RC_PACER.mark()
    note("feed", r)
    return r["json"] if r["status"] == 200 and isinstance(r["json"], dict) else None


def to_num(x: object) -> float | None:
    try:
        return round(float(x), 6)
    except (TypeError, ValueError):
        return None


def rc_extract(rep: dict) -> tuple[dict, list[tuple[str | None, str | None]], int | None]:
    """Printable facts (numbers and booleans only), private holder list, and decimals."""
    raw = rep.get("topHolders") if isinstance(rep.get("topHolders"), list) else []
    holders: list[tuple[str | None, str | None]] = []
    pcts: list[float] = []
    insiders = 0
    for h in raw:
        if not isinstance(h, dict):
            continue
        acct = h.get("address") if isinstance(h.get("address"), str) and B58.match(h["address"]) else None
        owner = h.get("owner") if isinstance(h.get("owner"), str) and B58.match(h["owner"]) else None
        holders.append((acct, owner))
        p = to_num(h.get("pct"))
        if p is not None:
            pcts.append(p)
        if h.get("insider") is True:
            insiders += 1
    facts: dict = {"rc_top_holders": len(holders), "rc_insider_holders": insiders}
    if pcts:
        facts["rc_top1_pct"] = pcts[0]
        facts["rc_top5_pct"] = round(sum(pcts[:5]), 3)
    if isinstance(rep.get("totalHolders"), int):
        facts["rc_total_holders"] = rep["totalHolders"]
    if isinstance(rep.get("score_normalised"), int):
        facts["rc_score_norm"] = rep["score_normalised"]
    if isinstance(rep.get("rugged"), bool):
        facts["rc_rugged"] = rep["rugged"]
    facts["rc_mint_auth"] = rep.get("mintAuthority") is not None
    facts["rc_freeze_auth"] = rep.get("freezeAuthority") is not None
    mk = rep.get("markets")
    facts["rc_markets"] = len(mk) if isinstance(mk, list) else 0
    tok = rep.get("token") if isinstance(rep.get("token"), dict) else {}
    dec = tok.get("decimals") if isinstance(tok.get("decimals"), int) else None
    return facts, holders, dec


def describe_err(err: object) -> str:
    """Describe a simulation error by shape and numbers only."""
    if err is None:
        return "none"
    if isinstance(err, str):
        return err if ENUM.match(err) else "other"
    if isinstance(err, dict) and len(err) == 1:
        (k, v), = err.items()
        k = k if ENUM.match(k) else "other"
        if k == "InstructionError" and isinstance(v, list) and len(v) == 2:
            idx = v[0] if isinstance(v[0], int) else None
            d = v[1]
            if isinstance(d, str):
                return f"InstructionError(idx={idx},{d if ENUM.match(d) else 'other'})"
            if isinstance(d, dict) and len(d) == 1:
                (k2, v2), = d.items()
                return f"InstructionError(idx={idx},{k2 if ENUM.match(k2) else 'other'}={v2 if isinstance(v2, int) else None})"
        return k
    return "other"


def quote_url(inp: str, out_mint: str, amount: int) -> str:
    qs = urllib.parse.urlencode({"inputMint": inp, "outputMint": out_mint,
                                 "amount": str(amount), "slippageBps": "500"})
    return f"{JUP_URL}/swap/v1/quote?{qs}"


def jup_quote(inp: str, out_mint: str, amount: int) -> dict | None:
    if "jup" in BLOCKED or COUNT["jup"] >= JUP_CAP:
        return None
    JUP_PACER.wait()
    COUNT["jup"] += 1
    r = http("GET", quote_url(inp, out_mint, amount))
    JUP_PACER.mark()
    note("jup", r)
    JUP_LAT.append(r["ms"])
    return r


def quote_ok(r: dict | None) -> bool:
    return bool(r) and isinstance(r["json"], dict) and "outAmount" in r["json"]


def quote_summary(r: dict) -> dict:
    js = r["json"]
    s: dict = {"status": r["status"], "ms": round(r["ms"])}
    if r["net_error"]:
        s["net_error"] = r["net_error"]
    if isinstance(js, dict) and "outAmount" in js:
        s["route"] = True
        s["impact_pct"] = to_num(js.get("priceImpactPct"))
        rp = js.get("routePlan") if isinstance(js.get("routePlan"), list) else []
        s["hops"] = len(rp)
        for hop in rp:
            info = hop.get("swapInfo") if isinstance(hop, dict) else None
            lab = info.get("label") if isinstance(info, dict) else None
            if isinstance(lab, str) and LABEL.match(lab):
                ROUTE_LABELS[lab] += 1
    elif isinstance(js, dict):
        code = js.get("errorCode")
        if isinstance(code, int):
            s["error_code"] = code
        else:
            s["error_code"] = code if isinstance(code, str) and CODE.match(code) else "other"
    elif r["status"]:
        s["non_json"] = True
    return s


def jup_swap(quote_json: dict, wallet: str) -> tuple[dict, str | None]:
    if "jup" in BLOCKED or COUNT["jup"] >= JUP_CAP:
        return {"skipped": "jupiter_blocked_or_capped"}, None
    JUP_PACER.wait()
    COUNT["jup"] += 1
    r = http("POST", f"{JUP_URL}/swap/v1/swap", {"quoteResponse": quote_json, "userPublicKey": wallet})
    JUP_PACER.mark()
    note("jup", r)
    JUP_LAT.append(r["ms"])
    js = r["json"] if isinstance(r["json"], dict) else {}
    tx = js.get("swapTransaction")
    tx = tx if isinstance(tx, str) and TX_B64.match(tx) else None
    s: dict = {"status": r["status"], "ms": round(r["ms"]), "tx": tx is not None}
    if tx is None:
        code = js.get("errorCode")
        s["error_code"] = code if isinstance(code, int) else (
            code if isinstance(code, str) and CODE.match(code) else "other")
    return s, tx


def simulate(tx: str) -> dict:
    res = rpc("simulateTransaction", [tx, {"encoding": "base64", "sigVerify": False,
                                            "replaceRecentBlockhash": True, "commitment": "processed"}])
    val = value_of(res)
    if not isinstance(val, dict):
        return {"sim": "rpc_error_or_stopped"}
    units = val.get("unitsConsumed")
    return {"sim_ok": val.get("err") is None, "sim_err": describe_err(val.get("err")),
            "sim_units": units if isinstance(units, int) else None,
            "sim_log_lines": len(val.get("logs") or [])}


def fmt(d: dict) -> str:
    return " ".join(f"{k}={str(v).lower() if isinstance(v, bool) else v}" for k, v in d.items())


def lat(vals: list[float]) -> str:
    if not vals:
        return "none"
    return f"min/med/max={min(vals):.0f}/{statistics.median(vals):.0f}/{max(vals):.0f}"


def burst_then_singles(call, n: int, offsets: list[float]) -> list[tuple[float, dict]]:
    """Fire n concurrent calls, then single calls at the given offsets from the burst start."""
    slots: list[dict | None] = [None] * n
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        barrier.wait()
        slots[i] = call()

    t0 = time.monotonic()
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    results = [(round(time.monotonic() - t0, 2), r) for r in slots if r is not None]
    for off in offsets:
        if any(r["status"] in (403, 429) or r["status"] >= 500 or r["status"] == 0 for _, r in results):
            break
        delay = t0 + off - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        results.append((round(time.monotonic() - t0, 2), call()))
    return results


def rpc_slot_call() -> dict:
    COUNT["rpc"] += 1
    r = http("POST", RPC_URL, {"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": []})
    track_rpc("getSlot", r)
    return r


def jup_control_call() -> dict:
    COUNT["jup"] += 1
    r = http("GET", quote_url(USDC, WSOL, 1_000_000))
    note("jup", r)
    return r


def run(out, lines: list[str], mode: str) -> int:
    t_start = datetime.now(timezone.utc)
    out(f"START {t_start.isoformat()} hosts=api.mainnet-beta.solana.com,lite-api.jup.ag,api.rugcheck.xyz")
    out(f"SLEEP_INHIBIT {mode}")

    RC_PACER.wait()
    COUNT["rc"] += 1
    feed = http("GET", RC_URL + "/v1/stats/new_tokens")
    RC_PACER.mark()
    note("feed", feed)
    mints: list[str] = []
    if isinstance(feed["json"], list):
        for it in feed["json"]:
            m = it.get("mint") if isinstance(it, dict) else None
            if isinstance(m, str) and B58.match(m) and m not in mints:
                mints.append(m)
    mints = mints[:N_MINTS]
    out(f"FEED status={feed['status']} valid_mints={len(mints)}")
    targets = [(f"m{i}", m) for i, m in enumerate(mints)] + [("ctrl", BONK)]

    ctrl = jup_quote(USDC, WSOL, 1_000_000)
    out("JUP control_usdc_to_sol " + (fmt(quote_summary(ctrl)) if ctrl else "skipped"))

    rows: list[dict] = []
    for label, mint in targets:
        row: dict = {"id": label}
        rep = rc_report(mint)
        facts, holders, rc_dec = rc_extract(rep) if rep else ({}, [], None)
        row.update(facts)
        mi = mint_info(mint)
        if mi:
            row.update(program=mi["program"], decimals=mi["decimals"], mint_auth=mi["mint_auth"],
                       freeze_auth=mi["freeze_auth"], exts=",".join(mi["exts"]) or "none")
            if rep:
                row["auth_agree"] = (mi["mint_auth"] == facts["rc_mint_auth"]
                                     and mi["freeze_auth"] == facts["rc_freeze_auth"])
        else:
            row["mint_info"] = "none"
        dec = mi["decimals"] if mi else (rc_dec if rc_dec is not None else 6)
        row["decimals_used"] = dec

        frozen, checked = 0, 0
        for acct in [a for a, _ in holders[:2] if a]:
            f = token_account_frozen(acct)
            if f is not None:
                checked += 1
                frozen += 1 if f else 0
        row["frozen_top_accounts"] = f"{frozen}/{checked}"
        wallet = None
        for owner in [o for _, o in holders[:6] if o][:4]:
            if wallet_like(owner):
                wallet = owner
                break
        row["holder_wallet"] = wallet is not None

        unit = 10 ** dec
        q1 = jup_quote(mint, WSOL, unit)
        row["q1_ok"] = quote_ok(q1)
        row.update({"q1_" + k: v for k, v in (quote_summary(q1) if q1 else {"skipped": "blocked"}).items()})
        if row["q1_ok"]:
            q2 = jup_quote(mint, WSOL, unit * 10_000)
            row.update({"q10k_" + k: v for k, v in (quote_summary(q2) if q2 else {"skipped": "blocked"}).items()
                        if k in ("status", "route", "impact_pct", "hops", "error_code")})
            if wallet:
                sw, tx = jup_swap(q1["json"], wallet)
                row.update({"swap_" + k: v for k, v in sw.items()})
                if tx:
                    row.update(simulate(tx))
        rows.append(row)
        out("TOKEN " + fmt(row))

    toks = [r for r in rows if r["id"] != "ctrl"]

    def n(pred) -> int:
        return sum(1 for r in toks if pred(r))

    out(f"TALLY tokens={len(toks)} rc_report={n(lambda r: 'rc_top_holders' in r)} "
        f"mint_info={n(lambda r: 'program' in r)} route={n(lambda r: r.get('q1_ok') is True)} "
        f"no_route={n(lambda r: r.get('q1_ok') is False)} swap_built={n(lambda r: r.get('swap_tx') is True)} "
        f"sim_ok={n(lambda r: r.get('sim_ok') is True)} sim_fail={n(lambda r: r.get('sim_ok') is False)} "
        f"no_holder_wallet={n(lambda r: r.get('holder_wallet') is False)}")
    out(f"XTAB route_but_sim_fail={n(lambda r: r.get('q1_ok') is True and r.get('sim_ok') is False)} "
        f"freeze_set={n(lambda r: r.get('freeze_auth') is True)} "
        f"freeze_set_with_route={n(lambda r: r.get('freeze_auth') is True and r.get('q1_ok') is True)} "
        f"token2022={n(lambda r: r.get('program') == 'spl-token-2022')} "
        f"transfer_fee_ext={n(lambda r: 'transferFeeConfig' in str(r.get('exts')))} "
        f"auth_agree_true={n(lambda r: r.get('auth_agree') is True)} "
        f"auth_agree_false={n(lambda r: r.get('auth_agree') is False)} "
        f"rc_rugged_true={n(lambda r: r.get('rc_rugged') is True)}")

    gaps = []
    for _ in range(3):
        s = {c: rpc("getSlot", [{"commitment": c}]) for c in ("processed", "confirmed", "finalized")}
        if all(isinstance(v, int) for v in s.values()):
            gaps.append((s["processed"] - s["finalized"], s["confirmed"] - s["finalized"]))
    out(f"RPC_COMMITMENT_GAP_SLOTS processed_minus_finalized/confirmed_minus_finalized={gaps}")
    if "rpc" not in BLOCKED and "getSlot" not in METHOD_STOP:
        res = burst_then_singles(rpc_slot_call, 8, [1.0, 2.0, 4.0])
        out("BURST rpc_getSlot " + " ".join(f"{r['status']}@{t}" for t, r in res))
    time.sleep(JUP_GAP)
    if "jup" not in BLOCKED:
        res = burst_then_singles(jup_control_call, 3, [2.5, 5.0])
        out("BURST jupiter " + " ".join(f"{r['status']}@{t}" for t, r in res))

    for svc in ("rpc", "jup", "feed"):
        names = " ".join(sorted(x for x in HDR_NAMES[svc] if ENUM.match(x)))
        out(f"HEADERS {svc} names: {names}")
        out(f"HEADER_VALUES {svc} " + "; ".join(f"{k}={v}" for k, v in sorted(HDR_VALUES[svc].items())))
    for m, st in sorted(RPC_STATS.items()):
        out(f"RPC_METHOD {m} statuses={dict(st)} latency_ms {lat(RPC_LAT.get(m, []))} "
            f"stopped={METHOD_STOP.get(m, 'no')} first_rl={RPC_HDR_FIRST.get(m)} last_rl={RPC_HDR_LAST.get(m)}")
    out(f"JUP_LATENCY_MS n={len(JUP_LAT)} {lat(JUP_LAT)}")
    out("ROUTE_LABELS " + ", ".join(f"{k}:{v}" for k, v in ROUTE_LABELS.most_common(12)))
    out("RPC_ERRORS " + ", ".join(f"{m}/http{s}/code{c}:{k}" for (m, s, c), k in RPC_ERRS.items()))
    out("BLOCKED " + (", ".join(f"{k}:{v}" for k, v in BLOCKED.items()) or "none"))
    out(f"REQUESTS rpc={COUNT['rpc']} jupiter={COUNT['jup']} rugcheck={COUNT['rc']}")
    out(f"END {datetime.now(timezone.utc).isoformat()}")

    path = Path(__file__).resolve().parent.parent / "data" / "survey" / f"jupiter_rpc_{t_start:%Y%m%dT%H%M%SZ}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"saved {path.name}")
    return 0


def main() -> int:
    sys.stdout.reconfigure(errors="replace")
    lines: list[str] = []

    def out(s: str) -> None:
        print(s)
        lines.append(s)

    mode = inhibit_sleep(True)
    try:
        return run(out, lines, mode)
    finally:
        inhibit_sleep(False)


if __name__ == "__main__":
    raise SystemExit(main())