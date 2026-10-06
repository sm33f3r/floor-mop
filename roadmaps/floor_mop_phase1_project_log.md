# Floor Mop — Phase 1 Project Log

**Phase:** 1 — Data source survey (desk research, then live measurement)
**Live measurement window:** 2026-10-01 to 2026-10-06 (KST)
**Status:** Measurement complete. Deferred items listed in section 11.
**Written:** 2026-10-06

> **Scope of this log.** It records what Phase 1 actually built and measured, and where that diverged from the plan. "The plan" means the Phase 1 roadmap, the data source catalogue (Draft v0.1) and the probe order agreed in session. The catalogue is left unchanged as a record of what was believed at the time; corrections to it are listed here (section 9). The roadmap file itself was not re-read while writing this log, so divergences are measured against the catalogue's probe plan and the in-session agreements.

> **Every measured number is specific to one host and one time window.** All runs came from the operator's home connection (Windows desktop, plus one run on the CachyOS laptop). No run came from a US-East host, where several relays recommend running. Floor Mop is run by many operators from many locations, so these numbers are a starting point, not constants. That is why the race test doubles as each operator's self-benchmark.

---

## 1. Outcome in brief

- **Primary launch relay: PumpDev.** In a 45-minute four-feed race, PumpDev saw 1,610 of 1,611 jointly-seen Pump.fun launches first or alone, at a median of 167 ms ahead of PumpPortal. It missed 0.06% of launches; PumpPortal missed 9.0%.
- **PumpPortal stays as fallback.** It is also the only relay that tags LetsBonk launches and both kinds of migration.
- **RugCheck works keyless across every endpoint tested.** Its new-token feed is a sample, about 7 seconds behind the relays. Its full report is the richest free enrichment source, and its rug stream works keyless.
- **Raydium is the LaunchLab and LP-data source.** Its LaunchLab feed loses nothing but runs about 19 seconds behind PumpPortal. API v3 gives fee, APR and volume data for LaunchLab graduate pools.
- **DexScreener lists Pump.fun curve tokens.** It does so about 45 seconds after creation, behind a 30-second cache. It is a cross-venue enrichment source, not a detection source.
- **Jupiter routes most fresh tokens, but a route is not proof of sellability.** 3 of 6 routed tokens failed sell simulation, cause unresolved. Sellability must be the last filter stage.
- **GMGN has the richest data, but only through a demo key so far.** A real key requires a deposit.

Provider roles in section 6 are **recommendations pending operator confirmation.**

---

## 2. What was built

All scripts live in `survey/`. They are never imported by `src/` and are not part of the package. Raw captures go to `data/survey/`, which is gitignored.

| File | How built | What it measures |
|---|---|---|
| `survey/probe_pumpportal.py` | Coding agent | PumpPortal keyless WebSocket: event shapes, platform field, timing |
| `survey/probe_pumpdev.py` | Coding agent | PumpDev anonymous WebSocket: schema, quote mints, optional capped trade sample |
| `survey/probe_rugcheck.py` | Coding agent (2 rounds) | RugCheck keyless HTTP and SSE: feed lag and continuity, summaries, full report, analytics, rug stream, rate-limit headers |
| `survey/probe_raydium.py` | Coding agent (3 rounds) | Raydium LaunchLab Mint and History APIs and API v3: shapes, continuity, join to pools, launch-pool survey, ETags, bursts |
| `survey/probe_dexscreener.py` | Coding agent | DexScreener: appearance lag for new tokens, pair freshness, profiles and boosts, metas, bursts |
| `survey/probe_jupiter_rpc.py` | Hand-written, run by hand (v2) | Jupiter quotes and swap build, RPC simulation, public RPC limits, authority cross-check |
| `survey/probe_gmgn.py` | Hand-written, run by hand | GMGN read-only structure with the public demo key |
| `survey/race_test.py` | Coding agent | Four-feed launch race, coverage cross-check, self-benchmark, offline analyze mode |

Also added: a `survey` dependency group (`websockets`) and `requirements-survey.txt`, so probe tooling stays out of Floor Mop's runtime dependencies.

**Commit state at time of writing:** the catalogue, PumpPortal, PumpDev, RugCheck, Raydium, DexScreener, Jupiter/RPC and race-test scripts are committed and pushed. `probe_gmgn.py`, plus lint fixes to `probe_gmgn.py` and `probe_jupiter_rpc.py`, were pending commit when this log was written. The existing 51 tests still pass.

---

## 3. Method and safety conventions

These held across every probe and should carry forward into ingestion code.

- **Structure-only output.** Probes print field names, types, counts, numbers, timings and short allow-listed enum values. They never print token names, symbols, URIs, descriptions or links, because that text is attacker-controlled and a prompt-injection risk. Addresses are redacted or counted, never printed, except well-known quote-asset and program IDs.
- **No credentials in code or output.** Only GMGN needed a key, the published demo key. It was supplied through a session environment variable and cleared afterwards.
- **One connection per WebSocket feed, no retry loops.** Polled feeds are paced, capped and stop on 403 or 429 without retrying.
- **Fake-server self-tests** with planted hostile strings, for every agent-built probe.
- **Sleep protection and clock-jump detection.** These were added after one Raydium run lost 6.1 hours to laptop sleep. On Windows the probes ask the OS not to sleep during the run. Any interval where wall and monotonic clocks disagree by more than 3 seconds is excluded from timing statistics.
- **Compact digest files**, at most 200 to 350 lines, written for pasting back into review.
- **The coding agent runs no git commands.** This rule was set after probe 1. All git operations are done by the operator.

---

## 4. Findings by source

### 4.1 PumpPortal (WebSocket relay)

- **Access.** Keyless `subscribeNewToken` and `subscribeMigration` work. This resolves the catalogue's blocking question; no wallet or key is needed.
- **Event shapes.**
  - Pump creates have 15 keys: `bondingCurveKey, initialBuy, is_mayhem_mode, marketCapSol, mint, name, pool, signature, solAmount, symbol, traderPublicKey, txType, uri, vSolInBondingCurve, vTokensInBondingCurve`.
  - Migrations have only 4: `mint, pool, signature, txType`.
  - LetsBonk creates mostly lack `name`, `symbol` and `uri` (10 of 11 in one run).
- **Platform discriminator.** The `pool` field: `pump` and `bonk` on creates, `pump-amm` and `raydium-cpmm` on migrations. The mapping (Pump.fun graduates to PumpSwap, LetsBonk to Raydium CPMM) is inferred from matching counts in two runs.
- **Missing fields.** No timestamp field and no quote-mint field. Amounts arrive as mixed integers and floats in SOL units, never as raw integers.
- **Timing (this host).** Connect about 1.4 s.
- **Race test (section 5).** Missed 9.0% of the Pump.fun launches PumpDev saw. Lagged PumpDev by a median of 167 ms. One feed silence of 26 s in 45 minutes. 12 duplicate events.

### 4.2 PumpDev (WebSocket relay)

- **Access.** Anonymous `subscribeNewToken` works. Subscription ack took about 280 ms and connect about 1.2 s.
- **Schema.** Matches the docs, plus `tokenProgram`.
  - Creates carry `quoteMint, quoteTokenDecimals, quoteAmount, quoteAmountRaw` (integer), `marketCapQuote, vQuoteInBondingCurve, isMayhemMode, isCashbackEnabled`.
  - Non-SOL pairs omit `solAmount`, `marketCapSol` and `vSolInBondingCurve`, inferred from matching counts.
- **`quoteContextResolved` never appeared.** The docs describe it as present only in a degraded case. Quote fields were never null.
- **Quote mints.** About 10% of creates in one sample (7 of 71) were non-SOL: USDC, PUMP's own token mint, and several unidentified mints.
- **Mayhem and cashback.** Mayhem mode was on for about 23% of SOL-paired creates in both samples (15 of 64, 13 of 56). Cashback was on for none.
- **Graduations.** `create_pool` events (`source: pumpswap`, carrying pool address, canonical flag and initial reserves) arrive on the free `subscribeNewToken` subscription. That makes it a free graduation feed.
- **No per-event timestamp.**
- **Trade stream is expensive on the anonymous quota.** 3 sampled mints produced 150 messages in about 19 s, roughly 8 per second. The anonymous quota of 10,000 per month would last about 21 minutes of watching 3 young tokens.
- **Coverage.** Pump.fun and PumpSwap only, no LetsBonk.

### 4.3 RugCheck (HTTP and SSE)

- **Access.** Keyless on every endpoint tested. The catalogue's question about the auth header doesn't arise for these.
- **Rate limit.** Every response carries `x-rate-limit-limit: 15`. A 5-request concurrent burst took `remaining` from 14 to 10, and it was back to 14 by 1.86 s. No 429 was ever triggered. Window inferred as about 15 requests per 1 to 2 seconds.
- **Latency.** About 0.8 to 1.0 s per request from this host.
- **New-token feed.**
  - Returns 10 items and refreshes every 8 to 12 seconds. It is a sample, not a stream: 1 in 20 polls had no overlap with the previous one.
  - Newest-item lag behind `createAt`: median 6.5 s, maximum 11.8 s.
  - Against the relays in the race test: median 7.1 s behind, p95 12.4 s, maximum 15.6 s.
- **Authority and program checks barely discriminate.**
  - 170 of 200 items were Token-2022.
  - 11 of 200 had a mint authority, 6 of 200 a freeze authority.
  - RugCheck and the chain agreed on authorities in 8 of 8 tokens plus a control.
- **Summaries.**
  - Raw score is unbounded (up to 53,501). `score_normalised` is bounded, with a median of 1 and a maximum of 72 across samples.
  - Most new tokens score clean, and the median risk list is empty.
  - 5 of 8 and 8 of 12 sampled tokens were already scored before the first request.
- **Full report.** 36 top-level keys, including:
  - `rugged` and `detectedAt`
  - `topHolders[]` with an `insider` flag
  - `markets[]` with LP lock data (`lpLockedPct`, `lpLockedUSD`)
  - `creatorTokens[]`, the creator's earlier tokens with market cap and creation time
  - `launchpad`, `deployPlatform` and `transferFee`
  - every Token-2022 extension
  - `knownAccounts`, which is address-keyed

  `creatorTokens` is a free creator-history feature.
- **Analytics.** Launch and rug counts are usable as rough base rates. USD aggregates and average time-to-rug are implausible and not usable.

  | Platform | 24h launches | 24h rug events | ratio | 7d launches | 7d rug events | ratio |
  |---|---|---|---|---|---|---|
  | pump_fun | 53,418 | 5,076 | 9.5% | 319,378 | 32,641 | 10.2% |
  | pump_fun_amm | 2,295 | 42 | 1.8% | 14,329 | 210 | 1.5% |
  | raydium_launchlab | 2,063 | 60 | 2.9% | 15,534 | 536 | 3.5% |

  Rug counts may include older tokens, so the ratios are rough.
- **Rug stream (SSE).**
  - The first attempt timed out under a 15-second open timeout, a probe flaw. The second delivered headers in 4.7 s and returned 15 events in 240 s, with a keepalive every 30 s.
  - Event fields: `mint, platform, ruggedAt` (integer), `timeToRugMinutes` (null in 11 of 15), `peakMarketCapUSD, liquidityPulledUSD`.
  - RugCheck's definition of a rug is not stated.
  - The rug ticker and top-liquidity endpoints returned empty lists, which is unexplained.

### 4.4 Raydium (LaunchLab Mint, LaunchLab History, API v3)

- **Access and limits.** No auth, no rate-limit headers, served behind Cloudflare. Bursts of 5 concurrent plus 4 singles returned all 200s on every host.
- **ETags are ignored.** Every host sends an ETag, but `If-None-Match` always returned 200, never 304.
- **Creator stats is broken.** `/get-by-user/stats/create` returns HTTP 500 ("startTime type error"). It probably needs an undocumented `startTime` parameter.
- **`sort=new` is a lossless LaunchLab feed.**
  - Each call returns 100 items, with 98 to 100 overlapping between polls 5 to 6 s apart and no gaps.
  - Rate 0.016 to 0.027 per second (about 1,400 to 2,300 a day), consistent with RugCheck's 2,063 in 24 hours.
  - In the race test it trailed PumpPortal by a median of 19 s (range 14 to 28.5 s, 6 joint sightings).
  - Its first poll returns a 100-item backlog, which must be excluded when comparing coverage.
- **Mint API item fields.**
  - Present: `mint, poolId, creator, createAt` (epoch ms), `marketCap`, volumes, `finishingRate` (likely curve progress, unverified), a large `platformInfo` block, `migrateType: cpmm`, `transferFeeBasePoints`.
  - **No completion flag.**
  - **Transfer fees are common.** Median `transferFeeBasePoints` is 100 (1%) and the maximum 300 (3%).
  - **Quote tokens vary.** 26 to 46 distinct quote mints per 100 launches, about 42% of them Token-2022.
- **History API.**
  - The `poolId` value from the Mint API works (11 of 11 calls). The mint used as `poolId` returns nothing, despite the docs saying "mint address".
  - Candles have `t, o, h, l, c` and **no volume**.
  - Trades have `side, owner, amountA, amountB, blockTime, txid`. `txid` is a signature, so trades can join to other feeds.
  - The newest trade was about 9 s old at request time. History reaches back at least 246 days.
- **API v3 pools.**
  - Fields: `tvl, lpPrice, mintAmountA, mintAmountB, burnPercent, openTime, launchMigratePool` (boolean), `config` (trade, protocol, fund and creator fee rates), and `day`, `week`, `month` objects (`apr, feeApr, rewardApr[], volume, volumeFee, volumeQuote, priceMin, priceMax`).
  - **LaunchLab graduates dominate busy pools.** Of the top 500 Standard pools by 24h volume, 338 (68%) are launch-migrated. Their medians: TVL about $16.3k, day volume about $32.7k, day APR about 165%, age 81 hours. Reward APR was almost always empty (2 of 338).
  - **Graduate pools carry a 1% creator fee** (`creatorFeeRate` median 10,000 in a 1e6 scale) on top of the 0.25% trade fee. In one pool checked, `volumeFee` equalled 0.25% of volume, so it excludes the creator fee.
  - 298 of 338 graduate pools contain a token with a Token-2022 transfer-fee extension.
  - **Don't sort by APR.** Half the top 20 by APR had zero TVL.
  - TVL history is daily, 30 points.
- **Join from LaunchLab token to v3 pool.** Only 1 of 16 sampled launch tokens had a v3 pool, while 5 of 5 control tokens did. The join works; the sampled tokens had most likely not graduated (`finishingRate` is the untested link).
- **Cache headers.** `max-age=5` on `/mint/price` and `max-age=60` on `/main/info`, but the SOL price changed only once in 150 s.
- **Latency (laptop run).** Median about 100 ms for v3, 116 ms for history and 300 to 350 ms for the Mint API.

### 4.5 DexScreener

- **Access.** Keyless. Pair endpoints send `max-age=30`, others `max-age=60`, with `cf-cache-status: HIT` and `age` between 5 and 29 s. Every reading can be 30 to 60 s old before any upstream lag.
- **Appearance lag for new tokens.** Median 45.2 s after RugCheck's `createAt` (range 41.7 to 52.7 s, 9 of 10 appeared within 5 minutes). A second run gave a median of 53.3 s, with 12 of 15 appearing within 7 minutes. The tight spread suggests the cache drives it.
- **`pairCreatedAt` precedes RugCheck's `createAt` by 0.7 to 1.7 s.** So RugCheck's `createAt` is close to chain time.
- **Venue coverage.** Pump.fun curve tokens are listed immediately (`dexId: pumpfun`, 8 of 9; `meteoradbc`, 1 of 9). PumpSwap pairs are listed too, which covers the PumpSwap gap in Raydium's LP data.
- **Fields.** Liquidity in USD, base and quote; FDV and market cap; buys and sells, volume and price change at 5m, 1h, 6h and 24h; `labels`; socials; boosts.
- **Quote assets vary.** Pairs are quoted in USDC, USDT, USD1 and SOL, so thresholds must use USD fields.
- **Rate limits.** Published at 300 and 60 per minute. Bursts of 8+3 and 12+3 returned all 200s, with no rate-limit headers. Latency about 0.37 s.
- **Inconclusive.** Pair freshness (the sampled pairs were too quiet to show changes), and the arrival rate of profiles and boosts (polled for less than one cache window).

### 4.6 Public Solana RPC (`api.mainnet-beta.solana.com`)

- **Free tier, metered per method.**
  - Headers report `x-ratelimit-tier: free`, 250 requests per second, 40 connections and 10 pubsub.
  - Method limits: `getAccountInfo` 50, `getSlot` and `simulateTransaction` 150.
  - `getTokenLargestAccounts` drew a 429 with a method limit of 0, seen once and not retested.
- **Load.** 66 requests at about 2.5 per second drew no 429. Median latency about 350 ms.
- **Commitment gap.** Finalized lags processed by about 25 to 30 slots (about 10 to 12 s). The figure is skewed by reading the slots sequentially.
- **Holder data.** Top holders can't come from this RPC. RugCheck's report supplies them.

### 4.7 Jupiter (keyless `lite-api.jup.ag`)

- **Routing.** 6 of 8 fresh tokens got a sell route at both 1 and 10,000 tokens. Route labels were mostly the Pump.fun curve, with one PumpSwap (inferred).
- **Route failures differ.**
  - `TOKEN_NOT_TRADABLE` for a token with no market.
  - `NO_ROUTES_FOUND` for a token RugCheck says has a market. "No route" does not mean "no market".
- **Sell simulation.**
  - Method: an unsigned swap transaction built for a wallet-like top holder (from RugCheck), run through the RPC's `simulateTransaction`.
  - The BONK control and 3 of 6 routed tokens simulated cleanly, consuming 88k to 107k compute units.
  - 3 of 6 failed early with custom error 6025, after only 1.4k to 14.8k compute units. In PumpSwap's IDL, 6025 is `DivisionByZero`. The bonding-curve program's error table couldn't be read.
  - **Unresolved and possibly a method flaw:** the holder's token balance was never checked against the amount sold.
- **Rate limits.** 29 requests at about 0.48 per second plus a 3-concurrent burst drew no 429 and no rate-limit headers. Latency 0.46 to 0.59 s. The published keyless rate (about 0.5 per second) was never exceeded, so it remains untested.
- **Cost.** Each candidate costs two Jupiter calls (quote plus swap build). That makes Jupiter the tightest budget in the design.

### 4.8 GMGN (public demo key only)

- **Access.**
  - The published demo key `gmgn_solbscbaseethmonadtron` works for read-only endpoints. GMGN labels it testing-only, and it is probably shared.
  - **A real key requires a deposit** of at least 100 USDC or SOL (operator-reported).
  - Read-only requests need only the API key (`X-APIKEY`, a timestamp and a fresh UUID). Swap and order routes add a request-signing key.
  - IPv4 only.
- **Latency and limits.** About 0.55 s for token info and security, 1.0 to 1.4 s for the rest. No rate-limit headers. The documented leaky bucket is 20, with route weights of 1 to 3.
- **Data richness, the highest of any source.**
  - Token info has `launchpad_progress`, holder count, top-10 holder rate, price, volume and buy/sell counts per window from 1 minute to 24 hours, pool reserves, initial liquidity and `image_dup_count`.
  - It also has `dev.creator_open_count` (the creator's graduated count), `dev.ath_token_info` and `dev.fund_from_ts`.
  - `created_tokens` returns a creator's recent tokens with an `is_open` flag.
  - Smart-money signals return 50 per call. The newest was 40 s old in one run and 416 s in another.
- **Security fields.** `renounced_mint` and `renounced_freeze_account` are meaningful on Solana. `honeypot` and `can_sell` are null or 0 on Solana and must not be read as "safe".
- **Freshness on a tiny sample.** New tokens were 7 to 143 s old at fetch. One graduated token was seen about 20 to 24 s after it graduated.
- **Unresolved, partly from probe bugs.**
  - Item shapes for rank, trenches and holders collapsed, because a field name failed the probe's safety pattern. The field list comes from GMGN's docs instead.
  - K-line returned 0 candles because the probe sent seconds where the raw API expects milliseconds.
  - `near_completion` was empty in both runs.
  - Trenches returned 60 per category against a limit of 50.
  - Both server-side filter tests had no effect, so the filter body keys are unverified.

---

## 5. Race test results

**Setup.** Full run `full1`: 2,700 s from 2026-10-05 10:27 UTC on the Windows desktop, with PumpPortal, PumpDev, RugCheck and Raydium. There was also a 300-s quick run.

**Run health.** No clock jumps. Sleep protection active. No disconnects. No polling failures.

### 5.1 Pump.fun launches, PumpPortal against PumpDev (joined by mint)

| Metric | full1 | quick1 |
|---|---|---|
| Seen by both | 1,610 | 183 |
| Seen only by PumpPortal | 1 | 0 |
| Seen only by PumpDev | 160 | 16 |
| PumpPortal first | 0.25% (4) | 0% |
| Median gap (PumpDev ahead) | 167 ms | 239 ms |
| p5 / p95 gap | −2,217 ms / −102 ms | −1,699 ms / −120 ms |
| Largest PumpPortal lead | 428 ms | none |
| PumpPortal miss rate | 9.0% | 8.0% |
| PumpDev miss rate | 0.06% | 0% |

**Lag over time.** Lag wasn't steady. In the first three 9-minute slices PumpPortal sometimes trailed by 2 to 3 s (p5 −2.1 to −2.9 s). In the last two slices it settled to a tight gap of about 160 ms.

### 5.2 Migrations

- 21 pairs, with PumpDev ahead by a median of 124 ms. PumpPortal was first once (4.8%).
- About 24 migrations in 45 minutes against about 1,771 launches: roughly 1.4% of launches graduating, or about 770 a day at that window's rate. That is a thin sample.

### 5.3 Independent check against RugCheck

- **Pump-like mints.** Of 530 RugCheck-listed mints ending in "pump" (a heuristic for Pump.fun), PumpDev saw 504 (95.1%) and PumpPortal 401 (75.7%). 26 (4.9%) were seen by neither relay; whether they are vanity addresses from other launchpads or real misses is unresolved.
- **All mints.** Of 1,920 RugCheck mints of all kinds, 1,310 were seen by a relay. The rest are mostly other launchpads, which the relays don't cover.
- **Lag.** RugCheck trailed the first relay sighting by a median of 7.1 s.

### 5.4 LetsBonk

- PumpPortal reported only 10 LetsBonk launches in 45 minutes, while Raydium listed about 65 new ones (net of its backlog). PumpPortal's LetsBonk completeness is unresolved.
- For the 6 launches both saw, Raydium trailed PumpPortal by 14 to 28.5 s.

### 5.5 Feed health and output quality

- **Event rates.** About 40 events per minute for PumpDev and 37 for PumpPortal.
- **Silences.** The longest was 17.5 s on PumpDev and 26.3 s on PumpPortal; whether they coincided is unknown.
- **Burstiness figures are suspect.** They are identical for both relays, which points to a computation bug.
- **PumpDev ack time reads 0.3 ms**, probably timing an earlier "connected" frame.
- **The digest's 200-line cap cut sections R5 to R7.** They are present in the summary JSON.

---

## 6. Provider roles (recommended, pending operator confirmation)

| Role | Provider | Basis |
|---|---|---|
| Launch detection, primary | **PumpDev** (`subscribeNewToken`) | First on 99.75% of joint launches, 0.06% misses, carries quote mint and token program, free graduation events |
| Launch detection, fallback and LetsBonk | **PumpPortal** | Independent path. Only relay tagging LetsBonk launches and both migration venues. Misses about 9% of Pump.fun launches |
| Detection cross-check | **RugCheck** `new_tokens` | Independent, about 7 s behind, carries `createAt` |
| LetsBonk and LaunchLab launches | **Raydium** LaunchLab `sort=new` | Lossless but about 19 s behind |
| Graduation labels | PumpDev `create_pool`, PumpPortal `migrate` | Both free. PumpDev was first on 20 of 21 |
| Rug labels | RugCheck rug SSE | Keyless and working, but RugCheck's definition of a rug is unstated |
| Base rates | RugCheck analytics (counts only) | USD and time fields unusable |
| Safety and creator enrichment | **RugCheck** full report | Authorities (agree with chain), top holders with insider flag, LP locks, `creatorTokens` |
| Chain checks | Public Solana RPC | Authorities, token-account state, simulation. Free tier metered per method |
| Market and pair enrichment | **DexScreener** | Cross-venue including the Pump.fun curve and PumpSwap. About 45 s to list, 30 s cache |
| LP intelligence | **Raydium API v3** (LaunchLab graduates, Raydium pools), **DexScreener** (PumpSwap) | Fees, APR, volume, TVL. Daily TVL history |
| Sellability | **Jupiter** quote and swap build, plus RPC simulation, **last stage** | Tightest budget, and a quote alone is not proof |
| Future enrichment | **GMGN**, once a real key exists | Creator graduation history, smart money, curve progress. Planned as a later improvement |

---

## 7. Rate-limit budget (tightest first)

| Source | Limit | Observed |
|---|---|---|
| Jupiter keyless | About 0.5 req/s published | 0.48/s and a 3-concurrent burst fine; never exceeded |
| PumpDev anonymous trades | 10,000 messages a month | About 8 messages/s for 3 young tokens. Unusable for monitoring |
| GMGN | Leaky bucket 20, weights 1 to 3 (docs) | 14 requests at 1.2 s spacing fine. Demo key only |
| Public RPC free tier | 250 rps; per-method 50 (`getAccountInfo`), 150 (`getSlot`, `simulateTransaction`), 0 (`getTokenLargestAccounts`, seen once) | About 2.5/s with no 429 |
| RugCheck | 15 per window of about 1 to 2 s (inferred) | Burst of 5 left 10 remaining, recovered by 1.86 s |
| DexScreener | 300/min pairs, 60/min profiles, metas and boosts | Bursts of 8+3 and 12+3 fine |
| Raydium | Unpublished (Cloudflare) | Bursts of 5+4 fine on all three hosts |
| PumpPortal | Free streams; bans for reconnect storms or more than 200 subscribe messages a second | One connection per run, no issues |

---

## 8. Inputs for the record schema

Phase 1 was meant to inform the record schema and the "hit" definition, which are deferred to the next phase. These facts constrain them.

- **Timestamps.**
  - Neither relay sends an event-time field, so the earliest time known for a creation is our own receive time. Record `received_wall_ns`, `received_mono_ns` and the receiving host per feed.
  - Chain-near times come from other sources: RugCheck `createAt`, DexScreener `pairCreatedAt`, Raydium `createAt` (epoch ms), RugCheck `ruggedAt` and trade `blockTime`. They come later, with different meanings, so store them as separate fields with their source.
- **Identity.** The mint and signature are the join keys across feeds. Signatures join PumpPortal, PumpDev and Raydium trades (`txid`).
- **Quote assets.** Quote mint and decimals are mandatory. About 10% of Pump.fun launches and most LaunchLab launches are not SOL-quoted, so SOL-named fields (`solAmount`, `marketCapSol`) must never be relied on.
- **Amounts.** Store raw integers plus decimals where available (`quoteAmountRaw`, curve reserves). PumpPortal supplies only floats, which must be flagged as lossy.
- **Token program and extensions.** Token-2022 is the majority. Transfer-fee extensions are common on LaunchLab tokens and graduate pools, so fees must be recorded per token and per pool.
- **Platform and venue.** Record the launchpad (PumpPortal `pool`, PumpDev `source`, RugCheck `deployPlatform`, GMGN `launchpad_platform`) and the venue after graduation.
- **Stage provenance.** Each record carries its triggering stage, as already decided. Graduation events are available free from both relays.
- **Fee structure.** Separate the trade fee, creator fee, protocol fee and token transfer fee.
- **Untrusted text.** Name, symbol, URI, description, socials and links stay quarantined from any LLM context unless sanitised.
- **Confidence flags.** Several signals are not booleans of truth. RugCheck's rug definition is unknown, a Jupiter route is not sellability, and GMGN's `honeypot` is null on Solana. Record them as evidence with their source.

---

## 9. Corrections to the catalogue (Draft v0.1)

The catalogue stays as written. These are the measured corrections.

| Catalogue said | Measured |
|---|---|
| About 28,000 to 33,000 Pump.fun launches a day | RugCheck counted about 45,600 a day over 7 days. The race-test rate implies about 57,000 a day in that window |
| Graduation rate 1.4 to 3%, about 400 to 900 a day | About 1.4% and about 770 a day in the race window. An in-session extrapolation of 1,500 to 2,500 a day, made mid-phase, was wrong |
| PumpPortal: do free streams need a key? (blocking) | No key needed |
| Deployment location (blocking) | Resolved by decision: no fixed location. Each operator self-benchmarks with `race_test.py` |
| RugCheck auth header conflict; rate limits not stated | No auth on tested endpoints. `x-rate-limit-limit` is 15 |
| RugCheck rug stream as training labels | Works keyless, but "rug" is undefined. Ticker endpoints were empty |
| GMGN: API key only, no wallet | A real key requires a deposit (operator-reported). The read-only key alone can't sign trades |
| Raydium response schemas undeclared | Now known (section 4.4). Mint-as-`poolId` fails, despite the docs |
| Jupiter rate dictates filter ordering | Confirmed, and each candidate costs two calls |
| `quote_context_resolved === false` when quote is null | The field is absent in normal operation, and quote fields were never null in samples |

---

## 10. Divergences from plan

- **Added beyond the planned probe order.**
  - A Jupiter and public RPC probe. Sellability is the load-bearing check, so it needed measuring.
  - Both were hand-written scripts run by the operator, not coding-agent prompts, to cut cost and keep credentials away from the agent. Neither has fake-server self-tests.
- **GMGN measured differently.** It was probed with the public demo key instead of the operator's own key, because a real key requires a deposit. The planned "GMGN limit reconciliation" (requests against weight units) was not done.
- **RugCheck rate limits.** The planned binary search until a 429 was replaced by reading the rate-limit headers and running one bounded burst. The ramp mode exists in the script but was never run.
- **Raydium rate limits** were measured only with bounded bursts, since Raydium publishes none and no 429 was triggered.
- **Self-benchmark.** It was planned as a separate command. Instead it is built into `race_test.py` (any operator can run it and select feeds), with an offline analyze mode.
- **Not done.**
  - Helius free webhook count, and any Helius measurement.
  - GeckoTerminal and Solana Tracker probes.
  - The unread-provider list (Birdeye, Moralis, Dune, PumpSwap fee docs, Raydium Python, LP fee distribution and CLMM APR pages, public RPC docs).
  - DexScreener freshness, which was attempted but inconclusive.
- **Probe scripts were committed** even though they were first described as throwaway. "Throwaway" only ever meant outside `src/`. They were kept because operators must be able to rerun them from their own locations.
- **Dependencies.** A `survey` dependency group and `requirements-survey.txt` were added.
- **The coding agent was barred from all git commands** after probe 1.
- **Unplanned rounds of work** from probe flaws:
  - RugCheck: digest truncation, header-prefix mismatch, an SSE timeout.
  - Raydium: 5xx handling, wrong history id, a run lost to sleep.
  - DexScreener: inactive pairs chosen.
  - Jupiter: one 429 stopping the whole RPC host.
  - GMGN: collapsed shapes, k-line units.
- **Record schema and the "hit" definition** remain deferred, as planned. Section 8 supplies their inputs.

---

## 11. Open questions and deferred work

None of these blocks the next phase. They are Phase 3 and 4 inputs.

1. **PumpPortal's 9% misses.** Hypothesis: it doesn't emit non-SOL-quoted launches (PumpDev showed about 10% non-SOL). Test: record quote class per PumpDev create in a `race_test` v2.
2. **The 26 pump-like mints neither relay saw.** Query RugCheck's report for their `deployPlatform` (about 26 keyless calls).
3. **PumpPortal's LetsBonk completeness.** Rerun the race with Raydium items listed before the run excluded, and match by exact platform.
4. **`race_test` v2 fixes.** One-line-per-metric digest so R5 to R7 always fit, burstiness computation, PumpDev ack timing.
5. **RugCheck.** The definition of a rug, and why the ticker endpoints are empty.
6. **Raydium.** Join `finishingRate` against pool presence. Retry creator stats with a `startTime` parameter.
7. **Jupiter custom error 6025.** Record holder balance against amount sold and the failing program's ID. Answer this properly when designing the sellability filter.
8. **DexScreener.** Rerun freshness on active pairs. Poll profiles and boosts for longer than a cache window.
9. **GMGN, with a real key.** Verify trenches filter body keys (from the CLI source), k-line in milliseconds, why `near_completion` is empty, the wide item shapes, and the real key's limits and terms.
10. **A run from a US-East host**, to compare relay lead and miss rates.
11. **Helius, GeckoTerminal, Solana Tracker** and the unread-provider list, only if a later phase needs them.
12. **Carried over from Phase 0:** loosening `requires-python`, pending the collaborator's Python version.
13. **Housekeeping:** commit `probe_gmgn.py` and the survey lint fixes, so `ruff check .` passes.

---

## 12. Process notes

- **Rigor was over-applied.** Fake-server suites and multi-round fixes for low-risk read-only sources made Phase 1 slow and costly. For later phases, use full self-tests only where secrets, money or ban risk are involved. Hand-run scripts and single real runs suffice elsewhere.
- **Pasteable digests saved the most review time.** Paraphrased agent reports and terminal output cut off by scroll limits caused several wasted rounds. Always ask for the digest file itself.
- **Long runs need explicit sleep protection** and a check for wall-clock jumps.
- **Case-insensitive leak searches can produce false alarms** inside base58 addresses ("evil" inside "ReViLi"). Treat any match in address-bearing files as a prompt to inspect, not as a leak.

---

## 13. Hand-off to the next phase

Phase 1 is ready for its operator decisions:

1. Confirm or amend the provider roles in section 6.
2. Define the record schema and the "hit", using section 8.
3. Decide which deferred items in section 11 are needed before ingestion begins, if any.
