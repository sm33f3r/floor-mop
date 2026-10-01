# Floor Mop — Data Source Catalogue

**Status:** Draft v0.1
**Date:** 1 October 2026
**Phase:** 1 — Data Source Survey (desk research output)
**Constraint:** Free tier, fully free, or DIY only. No paid plans, no exceptions.

---

## 1. Scope and evidence grading

This catalogue is the desk-research output of Phase 1. It records what each candidate source offers **on its free tier**, what it costs in rate limits and credits, and what still needs measuring. It does not choose a winner — Phase 1 assembles a **portfolio of providers by role**.

### Evidence grades

Every claim in this document carries a grade. Nothing ungraded should be treated as fact.

| Grade | Meaning |
|---|---|
| **[P]** | **Primary.** Read directly from the provider's own documentation, spec, or repository. |
| **[S]** | **Secondary.** From a third-party article, wrapper library, competitor comparison, or vendor marketing. Plausible, unconfirmed. |
| **[U]** | **Unverified.** Not yet read, or inferred by the architect. Must be checked before any code depends on it. |

### Deliberately unread (to verify later)

The user has approved proceeding with these marked unverified:

- Solana Tracker official API docs
- GeckoTerminal / CoinGecko official API guide
- Public Solana RPC rate-limit documentation
- PumpSwap fee documentation (`PUMP_SWAP_README.md`)
- Raydium Python integration page, LP fee distribution page, CLMM APR page
- Birdeye free-tier terms
- Moralis launchpad endpoints
- Dune (for historical graduation base rates)

---

## 2. The three data purposes

Floor Mop consumes data for three distinct jobs. Keeping them separate matters because they have different latency requirements, different budgets, and different consumers.

### Purpose A — Detection and handoff

Find new tokens as they appear, filter them, and emit candidate records to Sylvester. **Latency-critical.** Feeds the configurable handoff stage (section 4).

### Purpose B — Enrichment and safety

Answer "what is this token and can it be exited?" for candidates that survive detection. **Latency-sensitive but not critical** — runs per candidate, so rate limits bind harder than latency.

### Purpose C — LP-pool intelligence

Track which meme tokens made it into liquidity pools, and what those pools are worth: TVL, trading fees, APR/APY. **Not latency-sensitive at all.**

**Sylvester will not take LP positions.** This data is collected because it is useful to the operator for their own decisions, and because the cost of collecting it is near zero once the rest of the pipeline exists. It is emitted as its own record stream, separate from the candidate handoff, and nothing downstream of Floor Mop is expected to act on it automatically.

---

## 3. Launch lifecycle primer

Understanding the stages is a prerequisite for reading the provider cards.

### Pump.fun

1. **Creation.** A coin is created and is instantly tradeable on a bonding curve — a pricing formula run by the Pump program itself, with no counterparty liquidity. **[P]**
2. **Curve trading.** Buyers pay SOL (or USDC) into the curve; price rises as tokens are sold. **[P]**
3. **Graduation.** When `real_token_reserves == 0` the curve's `complete` flag is set. A permissionless `migrate` instruction then moves liquidity into a PumpSwap pool and the LP tokens are burnt. **[P]**
4. **Post-graduation.** The token trades on PumpSwap. **[P]**

**Key constants and addresses [P]:**

| Item | Value |
|---|---|
| Pump bonding curve program | `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` |
| PumpSwap (Pump AMM) program | `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA` |
| Global config account | `4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf` |
| `token_total_supply` | 1,000,000,000,000,000 (1B at 6 decimals) |
| `initial_real_token_reserves` | 793,100,000,000,000 |
| `initial_virtual_token_reserves` | 1,073,000,000,000,000 |
| `initial_virtual_sol_reserves` | 30,000,000,000 (30 SOL) |
| `pool_migration_fee` | 15,000,001 lamports |
| `fee_basis_points` | 100 bps (1%) |
| Creation instructions | `create`, `create_v2` |
| Trade instructions | `buy`, `sell`, `buy_exact_quote_in` + new `buy_v2`, `sell_v2`, `buy_exact_quote_in_v2` |
| Migration instruction | `migrate` (permissionless, idempotent) |
| Bonding curve PDA seeds | `["bonding-curve", mint]` |

**Three details with schema consequences [P]:**

- New coins are **Token-2022** mints (`TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb`), initialized with 6 decimals.
- Coins can be **paired with USDC**, not just SOL. `bonding_curve.quote_mint` is `Pubkey::default()` for SOL-paired coins. Pump renamed `real_sol_reserves` → `real_quote_reserves` and `virtual_sol_reserves` → `virtual_quote_reserves`. **No threshold may assume SOL.**
- Creation carries `is_mayhem_mode` and `is_cashback_enabled` flags, plus `name` (≤32 chars), `symbol` (≤13), `uri` (≤200), and a `creator` pubkey that may differ from the transaction signer (free-creation flow: first buyer signs, original creator is named).

**Pump.fun has no hosted data API.** It publishes program docs and IDLs on GitHub only; the hosts its web app calls are undocumented with no stability promise. **[P]** Every feed below is a third-party relay or an indexer.

### LaunchLab (LetsBonk and others)

LetsBonk.fun tokens are created and traded on **Raydium LaunchLab**. **[P]**

| Item | Value | Grade |
|---|---|---|
| LaunchLab program | `LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj` | **[P]** (confirmed by Raydium's own program-addresses page) |
| LetsBonk platform config account | `FfYek5vEz23cMkWsdJwG2oa6EphsvXSHrGpdALN4g6W1` | **[S]** (Bitquery only) |
| Creation instruction | `initialize_v2` | **[S]** |
| Migration instructions | `migrate_to_amm`, `migrate_to_cpswap` | **[S]** |
| Reserved tokens | 206,900,000 | **[S]** |
| Initial real token reserves | 793,100,000 | **[S]** |
| LP Lock / Burn & Earn program | `LockrWmn6K5twhz3y9w1dQERbmgSaRkfnTeTKbpofwE` | **[P]** |

**Platform disambiguation matters:** multiple launchpads (LetsBonk, StonkFun, others) share the LaunchLab program. The platform config account in the instruction's account list is what distinguishes them. **[S]**

**Recent program churn [P]:** LaunchLab moved to Anchor 1.0.2 on 2026-09-09; `Initialize` now always fails, `MigrateToAmm` lost arguments and OpenBook accounts. As of 2026-08-17, **CPMM is mandatory for new LaunchLab launches**. LaunchLab accepts Token-2022 quote mints as of 2026-08-24. Any DIY decoder will need maintenance.

### Venue share — unresolved

Sources contradict each other on whether Pump.fun or LetsBonk leads. Several "2026" articles recycle 2025 events. **[U]** — needs a current Dune dashboard or equivalent.

### Scale

| Metric | Figure | Grade |
|---|---|---|
| Pump.fun launches/day | ~28,000–33,000 | **[S]** (derived from two third-party trackers: 776,306 births in 28 days; 1.03M in August 2026) |
| Graduation rate | 1.4%–3% | **[S]** |
| Graduations/day | ~400–900 | **[U]** (architect's arithmetic from the above) |

---

## 4. Configurable handoff stage

**The stage at which Floor Mop hands a candidate to Sylvester is a setting, not a design decision.** It must be adjustable without a rebuild, and eventually adaptive.

### Stage options

| Stage | What it means | Volume/day | Trade-off |
|---|---|---|---|
| **Birth** | Token created, still on curve | ~30,000 | Earliest entry, lowest price, almost no information, 97–99% never graduate |
| **Near-graduation** | Curve 80–95% full | Unknown | Demand already demonstrated, narrower window |
| **Graduation** | Pool opened, LP burnt | ~400–900 | Proven demand, LP rug removed, higher entry price |
| **Post-graduation** | Trading on AMM | — | Momentum entry rather than launch sniping |

### Default

**Both birth and graduation active**, as the configuration most likely to make money, accepting the risk. Each stage carries its own thresholds — for example a minimum graduation probability for birth-stage candidates, which can be set far looser than a conservative operator would choose.

### Requirements

- Stage selection and per-stage thresholds live in config, never in source
- Changeable at runtime without a restart, for fast experimentation
- Every emitted record carries the stage that triggered it, so outcomes can be attributed per stage
- **Later:** an adaptive mode that shifts stage weighting based on measured paper-run outcomes

### Related but distinct

**Reaction-time tier** asks *how fast* Floor Mop must react; **handoff stage** asks *at which moment in a token's life* it acts. Under the free-only constraint, Tier A (first blocks) is unreachable — every shred feed is paid (section 9). Tier B (seconds) is the working assumption. **[U]**

---

## 5. Provider cards

### 5A — Detection streams (Purpose A)

---

#### Helius — Free plan

**Role:** Detection (DIY decode), ground-truth audit, enrichment RPC
**Access:** JSON-RPC, WebSocket, Parsed Streams, webhooks
**Auth:** API key (provider key only — no wallet, no signing)

| Item | Value | Grade |
|---|---|---|
| Monthly credits | 1,000,000 | **[P]** |
| RPC rate limit | 10 req/s | **[P]** |
| DAS API | 2 req/s | **[P]** |
| Enhanced APIs | 2 req/s | **[P]** |
| Standard RPC call cost | 1 credit | **[P]** |
| `getProgramAccounts` | 10 credits | **[P]** |
| DAS API calls | 10 credits | **[P]** |
| LaserStream WSS (standard Solana methods) | **Included on Free** | **[P]** |
| LaserStream WSS (Helius extensions, `transactionSubscribe`) | **Not on Free** (Developer+) | **[P]** |
| LaserStream gRPC mainnet | Not on Free (Business, $499) | **[P]** |
| WebSocket data metering | 2 credits per 0.1 MB uncompressed | **[P]** |
| **Parsed Streams** (`parsedTransactionSubscribe`) | **Included on all plans**, 1 credit per delivered event | **[P]** |
| Webhook events | 1 credit each | **[P]** |
| Webhook management (create/edit/delete) | 100 credits per request | **[P]** |
| Webhooks on Free plan | **Implied available** — docs describe Free-plan webhook auto-disabling over a 24h window | **[S]** |
| Number of webhooks on Free | Not stated in current plan table; legacy "Free V3" allowed 1 | **[U]** |
| Preconfirmations, Preprocessed Transactions | Paid plans only | **[P]** |
| Raw shreds | $1,000/month/IP | **[P]** |

**Parsed Streams is the standout find.** It decodes transactions server-side and filters by program, account and instruction name — exactly what is needed to receive only Pump.fun `create`/`create_v2` events. Metering began 24 September 2026. **[P]**

**But the budget does not survive it as a permanent feed.** At ~30,000 launches/day × 1 credit = ~900,000 credits/month, which consumes essentially the entire free allowance with nothing left for enrichment. **[U — architect's arithmetic]** Suitable for a bounded measurement window, not continuous operation.

**Raw WebSocket is cheaper than first assumed.** Helius estimates a transaction at ~0.0006 MB; at 2 credits/0.1 MB, 1M credits ≈ 83M transaction-sized messages ≈ 2.8M/day. Pump.fun's daily transaction count is unknown, so whether a program-wide subscription fits is an open measurement. **[U]** Helius calls its size figures "rough estimates only."

**Risks:** single free API key; credits are the binding constraint, not rate limits.

---

#### PumpPortal

**Role:** Detection (Pump.fun + LetsBonk launches and migrations)
**Access:** WebSocket `wss://pumpportal.fun/api/data`
**Auth:** API key in query string or unclear whether free streams work keyless

| Item | Value | Grade |
|---|---|---|
| `subscribeNewToken` | **Free** | **[P]** |
| `subscribeMigration` | **Free** | **[P]** |
| `subscribeTokenTrade` | Metered: 0.01 SOL per 10,000 events | **[P]** |
| `subscribeAccountTrade` | Metered: 0.01 SOL per 10,000 events | **[P]** |
| Commitment level | **`processed`** | **[P]** |
| Latency | "typically less than 100 msec delayed behind gRPC if you run a server in New York" | **[P]** (vendor's own claim) |
| Trading/other endpoints | 25 req/s | **[P]** |
| Subscription messages | Max 200/second | **[P]** |
| Addresses per message | Max 5,000 | **[P]** |
| Connections | **One at a time.** Repeated multi-connection attempts cause temporary bans | **[P]** |
| Ban duration | Expires hourly | **[P]** |
| Historical data | None — live only | **[P]** |
| Event field schema | **Not documented** on either data page | **[P]** (absence confirmed) |
| Bonk vs Pump event distinction | Not documented | **[U]** |

**The wallet problem.** Metered subscriptions require an API key *and* a linked wallet funded with ≥0.02 SOL. PumpPortal Lightning wallets are real Solana keypairs; the API key **contains the wallet private key encrypted with AES-256**. **[P]** If a key is required even for the free streams, obtaining one means creating a wallet — which conflicts with Floor Mop's no-keys rule.

**First test:** do `subscribeNewToken` / `subscribeMigration` work with no key at all? The docs show the key in the URL but do not state it is required for free methods. **[U]**

**Note:** `processed` commitment means some delivered events may never finalise. Phantom rate must be measured (section 11).

---

#### PumpDev

**Role:** Detection (Pump.fun + PumpSwap only)
**Access:** WebSocket `wss://pumpdev.io/ws`
**Auth:** None required for anonymous tier

| Item | Anonymous | Free (key) | Grade |
|---|---|---|---|
| `subscribeNewToken` | **Free, unmetered, every tier** | Same | **[P]** |
| Live subscriptions (tokens + wallets) | 5 | 25 | **[P]** |
| Mints per `subscribeTokenTrade` call | 20 | 50 | **[P]** |
| Concurrent connections per IP | 1 | 1 | **[P]** |
| Monthly trade-message quota | 10,000 (per IP; IPv6 per /64) | 50,000 | **[P]** |
| Control messages | Max 40 per 10s (all tiers) | Same | **[P]** |
| Subscription key operations | Max 600 per 10s | Same | **[P]** |
| Paid tiers | Starter $49, Pro $129, Whale $299 | — | **[P]** (excluded) |

**Documented event fields [P]** — the most complete schema of any relay found:

- **Shared envelope:** `signature`, `mint`, `traderPublicKey`, `txType` (`buy`/`sell`/`create`/`complete`/`create_pool`), `quoteMint`, `quoteTokenDecimals`, `quoteAmount`, `marketCapQuote`, `quoteContextResolved`, `quoteLookupDisabled`
- **Creation (`txType: "create"`):** `name`, `symbol`, `uri`, `initialBuy`, `initialQuoteAmount`, `solAmount`, `bondingCurveKey`, `vTokensInBondingCurve`, `vQuoteInBondingCurve`, `vSolInBondingCurve`, `marketCapSol`, `isMayhemMode`, `isCashbackEnabled`
- **Curve trade:** adds `tokenAmount`, bonding-curve reserve fields
- **Migration:** `complete` then `create_pool`, carrying `pool`, `canonicalPool`, `isCanonicalPool`, `baseMint`, `poolBaseReserves(Ui)`, `poolQuoteReserves(Ui)`, `virtualQuoteReserves`, `poolEffectiveQuoteReserves(Ui)`
- **Non-SOL pairs:** normalized fields may be `null`; detect via `quoteContextResolved === false` and read raw fields. **Never assume 9 decimals for a non-SOL quote.**

**Lifecycle following:** one token subscription follows a mint through curve → migration → canonical PumpSwap pool with no re-subscribe. **[P]**

**Coverage limit:** Pump.fun and its canonical PumpSwap pool only. LetsBonk and Raydium are not covered. **[P]**

**Latency:** "within milliseconds of block confirmation" — implies confirmed-level, so no phantoms but slower than `processed` feeds. Commitment level not explicitly stated. **[P]** (vendor claim)

**Risk:** unknown operator, unknown reliability. A trading system should not depend on a single unknown aggregator without a chain-level cross-check.

---

#### Bitquery — reference only, not a production source

**Free offer:** 7-day trial, 1,000 API points, 100 MCP credits, 2 simultaneous streams; **trial is real-time only** (archive needs an add-on). Paid rate limits are 30/90/240 req/min by plan. **[P]**

**Excluded as a feed.** Retained as **free documentation** — its pages are where the LaunchLab program IDs, instruction names and curve constants in section 3 came from. GraphQL subscriptions, gRPC and Kafka all exist but are out of budget.

---

### 5B — Enrichment and safety (Purpose B)

---

#### RugCheck

**Role:** Safety / rug risk / rug labels — the closest thing to a Solana sellability proxy found so far
**Base URL:** `https://api.rugcheck.xyz`
**Auth:** **Core report endpoints require no key.** **[P]** (from RugCheck's own Swagger spec)

**Endpoints requiring no auth [P]:**

| Path | Returns |
|---|---|
| `GET /v1/tokens/{id}/report` | Full token report (response schema not declared in spec) |
| `GET /v1/tokens/{id}/report/summary` | `score`, `score_normalised`, `risks[]` (`name`, `level`, `description`, `score`, `value`), `lpLockedPct`, `tokenProgram`, `tokenType` |
| `GET /v1/tokens/{id}/insiders/graph` | Insider graph for a mint |
| `GET /v1/tokens/{id}/insiders/networks` | Insider networks for a mint |
| `GET /v1/tokens/{id}/metadata` | Name, symbol, image URL, attributes |
| `GET /v1/tokens/{id}/votes` | Community vote stats |
| `GET /v1/search` | Full-text search; `maxScore` filters out tokens above a risk score |
| `GET /v1/stats/new_tokens` | **Recently detected tokens:** `mint`, `creator`, `createAt`, `mintAuthority`, `freezeAuthority`, `decimals`, `program`, `symbol`, `events[]` |
| `GET /v1/stats/analytics` | **Launch counts, rug counts, time-to-rug by platform, rugged market cap** — windows `24h`/`1d`/`7d`/`30d` |
| `GET /v1/stats/rugs/stream` | **Server-Sent Events stream of live rug events as detected** |
| `GET /v1/stats/rugs/ticker` | Recent rugs (max 50) |
| `GET /v1/stats/rugs/top-liquidity` | Biggest rugs by USD liquidity pulled (max 25) |
| `GET /v1/stats/recent` | Most-viewed mints; windows `1m`–`1w` |
| `GET /v1/stats/trending` | Most-voted mints, 24h |

**Requiring a key (and some paid-only) [P]:** `POST /v1/bulk/tokens/report`, `POST /v1/bulk/tokens/summary`, `GET /v1/tokens/{id}/lockers`, `GET /v1/tokens/verified`, and the `refresh=true` parameter on reports ("paid API keys only").

**Why this matters most:** GMGN's honeypot flag is **EVM-only and always empty on Solana** (section below). RugCheck's rug stream and analytics give Floor Mop:
- **Labels** for supervised learning — a real rug/no-rug outcome per token
- **Base rates** per platform for calibrating indicators
- **A kill signal** for monitored candidates

**Unknowns:**
- **Rate limits: not stated in the spec.** 429 responses are documented but no numbers. **[U]** A third-party Rust client defaults to 60 req/min, which is that client's setting, not RugCheck's limit. **[S]** Other wrappers advertise ~1,000–1,080 calls/month free tiers but those are resellers, not RugCheck. **[S]**
- **Auth header:** spec says JWT in `Authorization`; a secondary write-up says `X-API-KEY` from a dashboard key. **[P]/[S] conflict**
- **Terms of use:** not in the spec. **[U]**
- **Full report field list:** schema blank in spec; a Rust client lists `mint`, `creator`, `detected_at`, `events`, `lockers`, `top holders`, `insider report`, `token extensions`, `creator balance`, `markets/LP`. **[S]**

**Limitation RugCheck states itself:** it evaluates on-chain structure only, cannot assess team credibility, and a clean report is not a guarantee of safety. Early-stage tokens with thin liquidity may score as elevated risk without malicious intent. **[S]**

---

#### GMGN

**Role:** Enrichment, scoring, trenches discovery, wallet intelligence
**Access:** `gmgn-cli` (npm) → GMGN OpenAPI
**Auth:** API key; `GMGN_PRIVATE_KEY` is a **request-signing key, not a wallet key** **[P]**

**Credential model [P]:** read routes (`token`, `market`, `portfolio`, `track`) need the API key alone. Only swap/order/quote routes need the signing key. Floor Mop uses read routes only — one provider credential, nothing that signs transactions. This satisfies the no-keys rule.

**Rate limiting [P]:** leaky bucket, `rate=20`, `capacity=20`, with per-route weights:

| Command | Route | Weight |
|---|---|---|
| `token info` / `security` / `pool` | `/v1/token/*` | 1 |
| `market trending` | `/v1/market/rank` | 1 |
| `market search` | `/v1/market/search` | 1 |
| `market kline` | `/v1/market/token_kline` | 2 |
| `market trenches` | `/v1/trenches` | 3 |
| `market signal` | `/v1/market/token_signal` | 3 |
| `market hot-searches` | `/v1/market/hot_searches` | 3 |
| `token holders` / `traders` | `/v1/market/token_top_*` | 5 |

**Conflict:** the user reports a free-tier limit of **1 request/second**; the docs describe 20/sec with weights. Whether "1/sec" is a plan cap layered on top, and whether it counts requests or weight units, is unresolved. **[U]** At 1 weight-unit/sec, `trenches` would be one call per 3 seconds.

**Trenches is the highest-value endpoint [P]** — one call returns up to 80 tokens per category across three lifecycle buckets (`new_creation`, `near_completion`, `completed`), with **server-side filtering** on:

`max_created` (token age, seconds/minutes), `min/max_progress` (bonding curve 0–1), `max_rug_ratio`, `max_bundler_rate`, `max_insider_ratio`, `max_entrapment_ratio`, `min_smart_degen_count`, `min_renowned_count`, `max_top_holder_rate`, `max_top70_sniper_hold_rate`, `max_fresh_wallet_rate`, `max_bot_degen_rate`, `min_volume_24h`, `min_swaps_24h`, `min_holder_count`, `min/max_marketcap`, `min/max_liquidity`, **`min/max_creator_created_count`**, **`min/max_creator_created_open_count`**, **`min/max_creator_created_open_ratio`**, `min_x_follower`, `max_twitter_rename_count`, `min_tg_call_count`.

The three creator fields in bold are **a creator's launch count, graduated count, and graduation ratio** — directly relevant to graduation-probability indicators (section 8).

Named filter presets: `safe` (rug ≤0.3, bundler ≤0.3, insider ≤0.3), `smart-money` (≥1 smart degen), `strict` (both plus ≥$1k 24h volume). **[P]**

**Response quirk [P]:** `near_completion` is always returned under the key `data.pump`, regardless of the requested `--type`.

**Critical Solana limitation [P]:** `is_honeypot` is **BSC/Base only** and returns an empty string on Solana. GMGN's own docs warn: *do not interpret an empty value as "not a honeypot" on Solana.* For Solana, GMGN offers `renounced_mint`, `renounced_freeze_account`, `burn_status`, `rug_ratio` (method undocumented), and `is_wash_trading`. **Sellability still requires a simulated sell.**

**Operational constraints [P]:**
- **IPv4 only.** 401/403 errors occur if outbound traffic uses IPv6. The deployment host must have IPv6 disabled.
- On `RATE_LIMIT_EXCEEDED` / `RATE_LIMIT_BANNED`, repeated requests during cooldown **extend the ban by 5s each time, up to 5 minutes**. Backoff must be strict.
- `X-RateLimit-Reset` header and `reset_at` body field give the retry time.

**Prompt-injection warning — from GMGN's own docs [P]:** token metadata fields (`name`, `symbol`, `link.description`, `link.website`, `link.twitter_username`, `link.telegram`, and on-chain URI content) are **fully attacker-controlled**. GMGN's CLI strips known injection framing and prints a neutralisation notice. This has direct schema consequences (section 10).

---

#### Jupiter

**Role:** **Sellability** — the load-bearing check
**Access:** `lite-api.jup.ag` (keyless) or `api.jup.ag` (keyed)

| Item | Value | Grade |
|---|---|---|
| Keyless rate limit | ~0.5 req/s | **[S]** |
| Free keyed rate limit | ~1 req/s | **[S]** |
| Quote endpoint | `GET /swap/v1/quote` | **[S]** |
| Swap build | `POST /swap/v1/swap` | **[S]** |
| Legacy `quote-api.jup.ag/v6` | Retired 1 October 2025 | **[S]** |
| Tokens under 24h old | Separate 0.5% fee rate on Ultra | **[S]** |

**Standard sellability pattern [S]:** Jupiter quote (token → SOL) → swap-build → one simulation. Jupiter routes across Raydium, Orca and pump.fun pools, so it checks real sellability regardless of which venue holds the liquidity.

**A quote alone may not prove sellability [U — architect's inference].** A token with an active freeze authority could still return a route. The simulation step is what makes the check meaningful.

**Rate limit is the hard constraint.** At ~1 req/s, sellability cannot be checked on every launch — only on candidates that survive cheaper filters. This shapes the entire filter ordering.

---

#### Public Solana RPC — DIY enrichment

**Role:** Mint/freeze authority, holder data, supply, deployer history, bundle detection
**Endpoints:** `https://api.mainnet-beta.solana.com`, `https://solana.publicnode.com` **[S]**

| Item | Value | Grade |
|---|---|---|
| Cost | Free, no key | **[S]** |
| Rate limit | ~100 requests per 10 seconds on public endpoints | **[S]** |
| Official limits page | Not read | **[U]** |

**What a working open-source implementation pulls from public RPC [S]** (ZendIQ extension): mint authority, freeze authority, top-1 and top-5 holder concentration, serial-deployer history (tokens launched by creator in last 30 days), and **Jito bundle detection at token creation** — identifying bundled launches.

This is the cheapest enrichment path and reduces dependence on provider credits. It also provides a free cross-check against GMGN and RugCheck.

---

#### Solana Tracker

**Role:** Supplementary token data, built-in rugcheck, Pump.fun data

| Item | Value | Grade |
|---|---|---|
| Free plan | 10,000 requests/month | **[S]** |
| Free rate limit | 1 request/second | **[S]** |
| WebSocket (Datastream) | Top tier only — excluded | **[S]** |
| Official docs | Not read | **[U]** |

~333 requests/day. Useful for spot checks and cross-validation only, not continuous operation.

---

### 5C — LaunchLab data (Purposes A, B)

---

#### Raydium LaunchLab Mint API

**Host:** `https://launch-mint-v1.raydium.io` (devnet: `launch-mint-v1-devnet.raydium.io`)
**Auth:** **None for read operations** **[P]**

| Endpoint | Parameters | Grade |
|---|---|---|
| `GET /get/list` | `sort` (required): `marketCap` \| `new` \| `lastTrade` \| `hotToken` | **[P]** |
| `GET /get/by/mints` | `ids` (required, comma-separated) | **[P]** |
| `GET /get/list-bonk-custom` | none | **[P]** |
| `GET /get-by-user/stats/create` | `wallet` (required) — **creator history** | **[P]** |
| `GET /get-by-user/stats/volume` | `wallet` | **[P]** |
| Search mints, featured top/recent | — | **[P]** |
| Vesting: by IDs, by pool, by owner | — | **[P]** |
| Platform config, platforms v2 | — | **[P]** |

**Response schemas are not declared in the OpenAPI spec** — every endpoint page shows paths and parameters but `200` responses carry only a description string. Field names require a live call. **[P]** (absence confirmed)

**`sort=new` is the LaunchLab new-launch feed** — API v3 has no creation-time sort, so this is where recent LaunchLab launches surface. **[P]**

**Vesting data** could reveal scheduled unlocks that signal future dev dumps. **[U — architect's inference]**

---

#### Raydium LaunchLab History API

**Host:** `https://launch-history-v1.raydium.io`
**Auth:** **None — all endpoints publicly accessible** **[P]**

| Endpoint | Parameters | Grade |
|---|---|---|
| `GET /trade` | `poolId` (required), `limit` 1–100 (default 50), `nextPageKey`, `owner` (trader filter), `minAmount`, `maxAmount` | **[P]** |
| `GET /kline` | OHLC at **1m / 5m / 15m**; `limit` 1–500 (default 300) | **[P]** |

**Trade-level data with a trader filter** supports buyer-count, buy/sell-ratio and momentum features, and potentially simulator replay. How far back history extends is not stated. **[U]** Response field names not declared. **[P]** (absence confirmed)

---

### 5D — LP-pool intelligence (Purpose C)

---

#### Raydium API v3 — pools, TVL, fees, APR

**Host:** `https://api-v3.raydium.io`
**Auth:** None **[P]**

| Endpoint | Purpose | Cache | Grade |
|---|---|---|---|
| `GET /pools/info/list-v2` | **Advanced pool listing, cursor pagination** | 60–90s | **[P]** |
| `GET /pools/info/ids` | Pool detail by ID (comma-separated) | 60s | **[P]** |
| `GET /pools/info/mint` | Pools by one or two token mints | 60s | **[P]** |
| `GET /pools/info/lp` | Pools by LP mint | 60s | **[P]** |
| `GET /pools/line/liquidity` | **Historical TVL, up to 30 days** | 300s | **[P]** |
| `GET /pools/line/position` | CLMM position history | 30s | **[P]** |
| `GET /pools/key/ids` | On-chain account keys | 60s | **[P]** |
| `GET /mint/price` | USD prices | **5s** | **[P]** |
| `GET /mint/ids` | Mint metadata | 20s | **[P]** |
| `GET /mint/list` | Default mint list with risk tags | 60s | **[P]** |
| `GET /main/info` | Protocol TVL and 24h volume | 60s | **[P]** |
| `GET /farms/info/lp` | Farms by LP mint (`pageSize` max 100) | 60s | **[P]** |
| `GET /main/cpmm-config`, `/main/clmm-config` | Fee tier configs | 60s | **[P]** |

**`list-v2` filters and sorts [P]:**
- `poolType`: `Concentrated` \| `Standard`
- `sortField`: `liquidity`, `volume24h`, `fee24h`, `apr24h`, `volume7d`, `fee7d`, `apr7d`, `volume30d`, `fee30d`, `apr30d`
- `sortType`: `asc` \| `desc`
- `size`: 1–1000 (required), `nextPageId` cursor, `mint1`, `mint2`, `mintFilter`, `hasReward`

**Declared pool response fields [P]:** `id`, `type`, `programId`, `lpMint`, `mint1`, `mint2`, `tvl`, `lpPrice`, `farmOngoingCount`, `day`, `week`, `month`.

The `day`/`week`/`month` objects are **untyped in the spec** — but the sort fields prove per-window `volume`, `fee` and `apr` values exist inside them. Exact inner field names need a live call. **[P]** (absence confirmed)

**`/pools/info/list` (v1) is marked legacy** — new integrations should use `list-v2`, or query by ID / mint pair / LP mint. `pageSize` capped at 1000. **[P]**

**LP economics [P]:** default CPMM `AmmConfig` index 0: `trade_fee_rate` 2500 (0.25% of volume), `protocol_fee_rate` 120000 (12% *of the fee*), `fund_fee_rate` 40000 (4% *of the fee*), `creator_fee_rate` 500 (0.05% of volume). **LPs retain roughly 0.21% of volume** — **[U — architect's arithmetic]**. Fee rates are admin-mutable; read them live rather than caching. Raydium also documents impermanent loss formulas and how it computes CLMM APR. **[U — unread]**

**Mint-pair ordering convention [P]:** endpoints taking `mint1`/`mint2` require `mint1 < mint2` in ascending pubkey byte order. Sort client-side.

**Rate limits [P]:** Cloudflare, progressive, per source IP. **No published numbers in the pages read.** 429 responses carry `Retry-After`. Raydium's guidance: never loop `/mint/price` in a bot; run an indexer or `programSubscribe` instead; anything over the ceiling should be served from your own cache.

**Staleness [P]:** the API v3 overview says "typically 1–5 minutes stale"; the REST overview says 5–60s edge caching; the per-endpoint pages give exact TTLs (above). The per-endpoint values are the most specific and should be trusted. Raydium warns of 1–2 slot divergence during congestion and says **RPC is always more current**.

---

#### The PumpSwap coverage gap

**Pump.fun graduates migrate to PumpSwap, not Raydium.** **[P]**

Raydium's pool data therefore covers **LaunchLab graduates (including LetsBonk) and other Raydium pools only** — not the largest single source of Solana meme tokens.

For PumpSwap pool TVL, fees and APR, the options are:

| Option | Notes | Grade |
|---|---|---|
| DexScreener | Pool liquidity, volume, txn counts | **[P]** |
| GeckoTerminal | Pool data, OHLCV, new pools | **[S]** |
| Decode PumpSwap pool accounts via public RPC | Full control, needs the IDL | **[U]** |
| PumpDev `create_pool` events | Reserves at pool creation | **[P]** |

**PumpSwap fee structure not yet read** (`PUMP_SWAP_README.md`). **[U]**

**PumpSwap quoting detail [P]:** pools carry a `virtual_quote_reserves` field; price against **effective quote reserves** = `pool_quote_token_account.amount + Pool::virtual_quote_reserves`, not the raw vault balance. Currently 0 on all pools, but code should use the effective value now. `BuyEvent`/`SellEvent` logs include the field.

---

#### DexScreener

**Base:** `https://api.dexscreener.com`
**Auth:** **No API key, no paid tier documented** **[P]**

| Endpoint | Rate limit | Grade |
|---|---|---|
| `/latest/dex/pairs/{chainId}/{pairId}` | 300/min | **[P]** |
| `/latest/dex/search` | 300/min | **[P]** |
| `/token-pairs/v1/{chainId}/{tokenAddress}` | 300/min | **[P]** |
| `/tokens/v1/{chainId}/{addresses}` (max 30) | 300/min | **[P]** |
| `/token-profiles/latest/v1` | 60/min | **[P]** |
| `/token-profiles/recent-updates/v1` | 60/min | **[P]** |
| `/token-boosts/latest/v1`, `/token-boosts/top/v1` | 60/min | **[P]** |
| `/community-takeovers/latest/v1`, `/ads/latest/v1` | 60/min | **[P]** |
| `/orders/v1/...` (paid orders check) | 60/min | **[P]** |

**13 endpoints, none requiring a key.** **[P]**

**WebSocket exists but is not useful for detection** — `wss://api.dexscreener.com` streams **token profiles, boosts, community takeovers and ads only**, not pair prices or trades. **[P]**

**Cache headers [S]:** pair/token/token-pairs go out with `max-age=30`; search, profiles and boosts with `max-age=60`. The cache answers before the rate limit does.

**Fields available [S]:** base/quote token name, symbol and address, price native and USD, liquidity, volume, txn counts, FDV, market cap, pair creation time, pair URL, image URL.

**Role:** PumpSwap and cross-venue pool intelligence; token age; cross-check on liquidity. Boost and ad data doubles as a paid-promotion signal.

---

#### GeckoTerminal

**Base:** `https://api.geckoterminal.com/api/v2`
**Auth:** None **[S]**

| Item | Value | Grade |
|---|---|---|
| Rate limit | 30 calls/min per IP, keyless, shared pool | **[S]** |
| CoinGecko Demo key (free) | Raises to ~100/min, 10k calls/month cap | **[S]** |
| `/networks/new_pools` | Newest pools across all networks | **[S]** |
| `/networks/solana/new_pools` | Newest Solana pools | **[S]** |
| `/networks/solana/trending_pools` | Trending | **[S]** |
| `/networks/solana/pools/{address}` | Pool detail | **[S]** |
| `/networks/solana/pools/{address}/ohlcv/{timeframe}` | OHLCV (minute/hour/day, max 1000 candles) | **[S]** |
| `/networks/solana/tokens/{address}/pools` | All pools for a token | **[S]** |
| Page size | ~20 pools per page | **[S]** |
| Daily OHLCV history | Reportedly ~6 months / ~184 candles | **[S]** |
| Official API guide | Not read | **[U]** |

**Role:** independent new-pool cross-check and PumpSwap pool data. The 30/min shared-IP limit makes it a sampling source, not a primary feed.

---

## 6. LP-data coverage map

Which source can answer "what is this pool worth?" for each venue.

| Venue | Raydium API v3 | DexScreener | GeckoTerminal | DIY RPC decode |
|---|---|---|---|---|
| Raydium CPMM / CLMM / AMM v4 | **Native, full** | Yes | Yes | Yes |
| LaunchLab graduates (LetsBonk etc.) | **Native, full** | Yes | Yes | Yes |
| **PumpSwap (Pump.fun graduates)** | **No** | Yes | Yes | Yes (needs IDL) |
| Meteora, Orca, others | No | Yes | Yes | Yes |

**Implication:** Raydium is the richest source (native APR, fee and farm data) but covers the minority of meme-token pools. DexScreener at 300 req/min is the broadest free cross-venue source. A complete LP picture needs both.

---

## 7. Roles and free-tier budget

### Portfolio by role

| Role | Primary | Fallback | Notes |
|---|---|---|---|
| **Detection — Pump.fun** | PumpDev (free, unmetered launches, documented fields) | PumpPortal | Both are third-party relays |
| **Detection — LaunchLab** | PumpPortal | Raydium Mint `sort=new`, or DIY decode | Raydium is polled, not streamed |
| **Ground-truth audit** | Helius free WebSocket / Parsed Streams (sampled) | Public RPC | Credit-bounded |
| **Enrichment — scoring** | GMGN trenches | Public RPC | Server-side filters save many calls |
| **Safety / rug risk** | RugCheck (keyless) | GMGN + public RPC | RugCheck covers Solana where GMGN does not |
| **Rug labels** | RugCheck SSE rug stream | — | Unique capability |
| **Sellability** | Jupiter quote + simulate | — | No alternative identified |
| **Market data** | DexScreener | GeckoTerminal, relay events | |
| **LP intelligence — Raydium venues** | Raydium API v3 | DexScreener | |
| **LP intelligence — PumpSwap** | DexScreener | GeckoTerminal, DIY decode | **Gap: no native source** |
| **Base rates** | RugCheck analytics | Dune | |

### Budget constraints, ranked by how hard they bind

1. **Jupiter ~1 req/s** — the tightest. Sellability can only run on survivors of cheaper filters. This dictates filter ordering.
2. **GMGN ~1 req/s (unresolved)** — at weight 3, trenches polls every ~3s. Server-side filtering is what makes this workable.
3. **Helius 1M credits/month** — cannot carry a continuous per-launch stream. Reserve for bounded measurement and targeted enrichment.
4. **GeckoTerminal 30/min** — sampling only.
5. **Solana Tracker 10k/month** — spot checks only.
6. **DexScreener 300/min** — the most generous; use it as the workhorse for pool data.
7. **Raydium** — unpublished Cloudflare limits; cache locally and respect 60s TTLs.
8. **RugCheck** — unknown; must be measured.

### Design consequence

**Cheap filters must run first, on data that arrives inside the detection event.** The PumpDev creation event alone carries mint, creator, name, symbol, URI, initial buy, quote mint, curve reserves and market cap — enough for a first-pass reject at zero marginal cost. Only survivors earn an enrichment call, and only their survivors earn a sellability check.

---

## 8. Graduation-probability and rug indicators

The user's requirement: *"develop indicators that show probability of an infant token's potential graduation."*

### Candidate features, by availability

**Available free at detection time (zero marginal cost) [P]:**
- Creator pubkey; whether creator ≠ signer (free-creation flow)
- `initialBuy` / `initialQuoteAmount` — creator's opening buy
- `quoteMint` — SOL vs USDC pairing
- `isMayhemMode`, `isCashbackEnabled`
- Initial curve reserves and starting market cap
- Name, symbol, URI (**untrusted — see section 10**)

**Available from GMGN trenches, server-side filtered [P]:**
- `creator_created_count`, `creator_created_open_count`, **`creator_created_open_ratio`** — the creator's historical graduation rate, the single most promising feature
- `progress` (bonding curve 0–1), `complete_cost_time` (creation → completion in seconds)
- `bundler_trader_amount_rate`, `rat_trader_amount_rate`, `suspected_insider_hold_rate`, `sniper_count`, `fresh_wallet_rate`, `bot_degen_rate`
- `smart_degen_count`, `renowned_count`
- `top_10_holder_rate`, `dev_team_hold_rate`, `creator_balance_rate`
- `swaps_1m` / `swaps_1h` / `swaps_24h`, `volume_1h` / `volume_24h`, `buys_24h` / `sells_24h`, `net_buy_24h`, `holder_count`
- `has_at_least_one_social`, `x_user_follower`, `twitter_rename_count`, `tg_call_count`
- `is_wash_trading`, `rug_ratio`

**Available from public RPC (free, DIY) [S]:**
- Mint and freeze authority status
- Holder concentration
- Serial-deployer history (creator's launches in last 30 days)
- **Jito bundle detection at creation** — bundled launch signal

**Available from RugCheck (keyless) [P]:**
- Risk score and named risks with severity
- `lpLockedPct`
- Insider graph and networks
- Per-platform launch counts, rug counts and time-to-rug

**Available from trade streams [P]:**
- Buyer count and unique-buyer growth
- Buy/sell ratio
- Curve progress velocity
- Time-to-first-N-buyers

### Labels

| Label | Source | Grade |
|---|---|---|
| Graduated (yes/no) | PumpDev `complete`/`create_pool`; PumpPortal `subscribeMigration`; LaunchLab migration instructions | **[P]** |
| Rugged (yes/no, when) | **RugCheck SSE rug stream** | **[P]** |
| Time to graduation | Creation timestamp → migration timestamp | **[P]** |
| Peak multiple after entry | Reconstructed from OHLCV | **[P]** |

### Modelling plan

1. **Phase 2 logs everything** — every launch, with all features available at the time, and the eventual outcome.
2. **Build the label set** from migration events and the rug stream.
3. **Start simple** — logistic regression or gradient boosting on the features above. Interpretability matters more than accuracy at first, because the features themselves are being evaluated.
4. **Handle class imbalance deliberately.** At a 1.4–3% base rate, a model predicting "never graduates" is 97–99% accurate and completely useless. Evaluate on precision/recall at the operating threshold, not accuracy.
5. **Calibrate, don't just rank.** The handoff stage needs a *probability* to threshold on, so the output must be calibrated against observed base rates.
6. **Measure against the reject log.** Track what happened to rejected candidates (Phase 6 requirement), otherwise a too-strict filter is indistinguishable from a good one.

**Nothing here is trusted until measured.** These are candidate features, not a validated model.

---

## 9. Excluded sources

| Source | Reason | Grade |
|---|---|---|
| Helius Developer ($49) / Business ($499) / Professional ($999) | Paid | **[P]** |
| Helius LaserStream gRPC mainnet | Business plan minimum | **[P]** |
| Helius Enhanced WebSockets (`transactionSubscribe`) | Developer plan minimum | **[P]** |
| Helius Raw Shreds | $1,000/month/IP ($800 on Pro) | **[P]** |
| Helius Preconfirmations | Professional only | **[P]** |
| Helius Preprocessed Transactions | Paid plans only | **[P]** |
| **Jito ShredStream** | **Service shut down 5 September 2026.** Jito directs users to DoubleZero Edge | **[P]** |
| DoubleZero Edge | Licensed/paid multicast shred feed | **[S]** |
| OrbitFlare ShredStream | Paid, or free slot tied to hosting with them | **[S]** |
| Triton Shred Streaming | Paid; also "requires custom software development on your end" | **[P]** |
| Yellowstone gRPC providers (Subglow $99, Solana Tracker $247, Chainstack ~$49, QuickNode, Triton) | Paid | **[S]** |
| Bitquery (as a feed) | 7-day trial only, real-time only, 2 streams | **[P]** |
| PumpDev paid tiers (Starter/Pro/Whale) | Paid | **[P]** |
| PumpPortal metered trade streams | 0.01 SOL per 10k events + requires funded wallet | **[P]** |
| GMGN paid plans | Paid | **[P]** |
| Jupiter paid API key | Paid | **[S]** |
| Solana Tracker paid tiers | Paid | **[S]** |
| Pump.fun undocumented web hosts | No reference, no published limits, no schema, no stability promise | **[P]** |

**Consequence of the shred exclusions:** reaction-time Tier A (first blocks after launch) is unreachable on a free budget. Tier B (seconds) is the working assumption, and Tier C (minutes, momentum entry) remains a fallback if measurement shows Tier B is also out of reach.

---

## 10. Schema implications

Findings from this research that the record schema must accommodate. The schema itself is designed after Phase 1 (deferred from Phase 0).

### 1. Quote mint is mandatory, not assumed

Pump.fun coins can be paired with USDC. PumpDev events may return `quoteAmount`, `marketCapQuote` and `poolQuoteReservesUi` as `null` for non-SOL pairs, flagged by `quoteContextResolved: false` and `quoteLookupDisabled: true`. **Every record carries `quote_mint` and `quote_decimals`; no threshold assumes SOL; raw fields are retained alongside normalized ones.** **[P]**

### 2. Commitment level is a first-class field

PumpPortal delivers at `processed`. PumpDev implies confirmed. Helius offers several levels. A candidate detected at `processed` carries different certainty from one detected at `confirmed`, and downstream logic must be able to tell. **Record the commitment level and track the phantom rate per feed.** **[P]**

### 3. Token metadata is untrusted input

`name`, `symbol`, `uri`, and description fields are attacker-controlled. Floor Mop's records feed Hermes, an LLM — this is a direct prompt-injection path. **Tag these fields as untrusted in the schema; consider not forwarding them to Hermes at all unless required; never let their content influence control flow.** GMGN's own docs issue this warning explicitly. **[P]**

### 4. Both creation instructions

Pump.fun launches use `create` **and** `create_v2`. A decoder handling only one silently misses launches. The same applies to the new `buy_v2` / `sell_v2` / `buy_exact_quote_in_v2` trade instructions. **[P]**

### 5. Token-2022

New Pump.fun mints are Token-2022, not legacy SPL Token. Extensions (transfer fees, transfer hooks, permanent delegate, default-frozen) affect both sellability and enrichment. LaunchLab also accepts Token-2022 quote mints as of 2026-08-24. **[P]**

### 6. Amounts as integers

Raw base units plus decimals, never floats. Required by the USDC-pairing case and by general precision discipline.

### 7. Stage provenance

Every candidate record records which handoff stage triggered it, so per-stage outcomes can be measured separately (section 4).

### 8. Pool-intelligence record is separate

Purpose C data (TVL, fees, APR per pool) has a different shape, a different cadence and a different consumer from candidate records. **Separate record type, separate stream.**

### 9. Effective quote reserves

PumpSwap pricing uses `pool_quote_token_account.amount + Pool::virtual_quote_reserves`, not the raw vault balance. Currently 0 everywhere, but encode it correctly now. **[P]**

---

## 11. Race-test and live-probe plan

### Live probes (cheap, do first)

These resolve the **[U]** items that matter most, each with a handful of requests:

| Probe | Question | Cost |
|---|---|---|
| PumpPortal keyless connect | Do `subscribeNewToken` / `subscribeMigration` work with no API key? | 1 connection |
| RugCheck limits | What is the actual rate limit on `/v1/tokens/{id}/report/summary`? Binary-search until 429, read headers | Tens of requests |
| RugCheck report shape | What fields does the full report actually return? | 1 request |
| RugCheck SSE | Does the rug stream connect keyless, and what is its event shape? | 1 connection |
| GMGN limit reconciliation | Does "1 req/s" count requests or weight units? Issue weight-1 then weight-3 calls and observe | ~10 requests |
| Helius free webhooks | Does the Free plan allow webhooks, and how many? | Dashboard check |
| Raydium response shapes | What are the actual field names in `day`/`week`/`month`, Mint API, History API? | ~5 requests |
| Raydium rate limits | Where does Cloudflare start returning 429? | Tens of requests |
| DexScreener freshness | How stale is pair data vs chain truth? | ~10 requests |

### Race test (the core measurement)

**Method [P-grade methodology, from the Phase 1 roadmap]:**
- Run **every detection candidate simultaneously** on **one machine** with a synchronised clock
- Key every event by **transaction signature**
- Record which feed delivered first, and each other feed's margin behind
- One shared timing log, one row per feed per event

**Candidates in the race:** PumpDev, PumpPortal, Helius Parsed Streams (sampled, credit-bounded), Helius standard WebSocket (DIY decode, sampled).

**Metrics:**
- **Latency percentiles: p50, p95, p99.** Never averages — the tail is what loses the trade.
- **Coverage / miss rate**, both directions, per venue
- **Phantom rate** — proportion of `processed`-level events that never finalise
- **Rate-limit behaviour under burst** — throttle, drop, queue, or disconnect
- **Reliability** — disconnects, gaps, backpressure over the full window
- **Cost consumption** — credits and quota burned per hour at observed volume

### End-to-end budget

Detection latency alone is misleading. Measure the full path:

```
detection → cheap filter → enrichment calls → sellability check → verdict
```

Record each stage's contribution. This determines whether the reaction-time target is achievable at all, and which enrichment calls can sit in the hot path versus running asynchronously.

### Harness rules

- Lives **outside `src/`** (e.g. `survey/`), clearly marked throwaway
- Provider keys from environment only — the public-repo rule applies identically
- One shared timing-log format across all candidates
- Nothing graduates into `src/` except the adapter interface

---

## 12. Open questions

### Blocking

1. **Deployment location.** Where will Floor Mop run? PumpPortal explicitly recommends a server in New York or the eastern US for lowest latency. Network distance is a first-order latency term and invalidates measurements taken elsewhere. **Railway region matters.**
2. **Does PumpPortal's free stream need a key?** If yes, obtaining one means creating a wallet, which conflicts with the no-keys rule.

### Important

3. **GMGN rate limit** — requests or weight units?
4. **RugCheck rate limits and terms of use.**
5. **Helius free-plan webhook count.**
6. **Venue share** — Pump.fun vs LetsBonk, current.
7. **Stop rule for the survey.** The provider list is open-ended. Recommendation: coverage-based — proceed once every role has a measured primary and critical roles have a fallback.

### Carried forward

8. Storage backend (decided with the record schema)
9. Does the existing scanner's pipeline genuinely overlap? (Phase 0 log §7)
10. Does the salvaged Python scraper contain reusable adapters?
11. `requires-python` fix, pending collaborator's Python version
12. How far back does Raydium's LaunchLab trade history extend?
13. PumpSwap fee structure

---

## 13. Sources

All retrieved 1 October 2026.

**Primary (provider's own documentation):**

- Pump public docs — https://github.com/pump-fun/pump-public-docs
- Pump program README — https://raw.githubusercontent.com/pump-fun/pump-public-docs/main/docs/PUMP_PROGRAM_README.md
- Pump coin creation — https://raw.githubusercontent.com/pump-fun/pump-public-docs/main/docs/instructions/COIN_CREATION.md
- Helius plans and pricing — https://www.helius.dev/docs/billing/plans
- Helius credits — https://www.helius.dev/docs/billing/credits
- Helius webhooks — https://www.helius.dev/docs/webhooks
- PumpPortal Pump.fun & PumpSwap data — https://pumpportal.fun/data-api/real-time
- PumpPortal LetsBonk.fun data — https://pumpportal.fun/data-api/bonk-fun-data-api
- PumpPortal FAQ — https://pumpportal.fun/FAQ
- PumpDev real-time data API — https://pumpdev.io/data-api
- GMGN OpenAPI wiki — https://github.com/GMGNAI/gmgn-skills/wiki
- GMGN README — https://raw.githubusercontent.com/GMGNAI/gmgn-skills/main/Readme.md
- GMGN market skill — https://raw.githubusercontent.com/GMGNAI/gmgn-skills/main/skills/gmgn-market/SKILL.md
- GMGN token skill — https://raw.githubusercontent.com/GMGNAI/gmgn-skills/main/skills/gmgn-token/SKILL.md
- GMGN skills market — https://gmgn.ai/ai?chain=sol&tab=skills_market
- RugCheck Swagger spec — https://api.rugcheck.xyz/swagger/doc.json
- Raydium docs index — https://docs.raydium.io/llms.txt
- Raydium API reference — https://docs.raydium.io/api-reference
- Raydium API v3 overview — https://docs.raydium.io/api-reference/api-v3/overview
- Raydium REST API surface — https://docs.raydium.io/sdk-api/rest-api
- Raydium program addresses — https://docs.raydium.io/reference/program-addresses
- Raydium LaunchLab Mint API — https://docs.raydium.io/api-reference/launch-mint-v1/overview
- Raydium LaunchLab History API — https://docs.raydium.io/api-reference/launch-history-v1/overview
- Raydium list mints with sorting — https://docs.raydium.io/api-reference/launch-mint-v1-endpoints/discovery/list-mints-with-sorting.md
- Raydium get mint details — https://docs.raydium.io/api-reference/launch-mint-v1-endpoints/details/get-mint-details.md
- Raydium list Bonk mints — https://docs.raydium.io/api-reference/launch-mint-v1-endpoints/discovery/list-bonk-mints.md
- Raydium user creation stats — https://docs.raydium.io/api-reference/launch-mint-v1-endpoints/user-activity/get-user-creation-stats.md
- Raydium trade history — https://docs.raydium.io/api-reference/launch-history-v1-endpoints/trades/get-trade-history.md
- Raydium pools list v2 — https://docs.raydium.io/api-reference/api-v3-endpoints/pools/list-pools-with-advanced-filters-v2.md
- Raydium pool info by IDs — https://docs.raydium.io/api-reference/api-v3-endpoints/pools/get-pool-info-by-ids.md
- DexScreener API reference — https://docs.dexscreener.com/api/reference
- Bitquery Pump.fun API — https://docs.bitquery.io/docs/blockchain/Solana/Pumpfun/Pump-Fun-API/
- Bitquery LetsBonk.fun API — https://docs.bitquery.io/docs/blockchain/Solana/letsbonk-api/
- Jito ShredStream sunset notice — https://docs.jito.wtf/lowlatencytxnfeed/
- Triton shred streaming — https://docs.triton.one/chains/solana/shred-streaming
- CoinGecko keyless public API — https://docs.coingecko.com/docs/keyless-public-api.md

**Secondary (third-party, unconfirmed):**

- Chainstack: pump.fun migrations to PumpSwap — https://docs.chainstack.com/docs/solana-listening-to-pumpfun-migrations-to-raydium
- DexScreener rate limits explained — https://coinpaprika.com/education/dexscreener-api-rate-limits-explained/
- GeckoTerminal rate-limit issue — https://github.com/dcccrypto/percolator-launch/issues/2578
- RugCheck project review — https://solanacompass.com/projects/rugcheck
- RugCheck Rust client — https://docs.rs/riglr-web-tools/0.3.0/src/riglr_web_tools/rugcheck.rs.html
- Solana Tracker Data API — https://www.solanatracker.io/data-api
- ZendIQ rug-detection extension — https://github.com/ZendIQ/ZendIQ-Extension-Lite
- Solana copy-trading bot guide (Jupiter sellability pattern) — https://yavorovych.medium.com/how-to-build-a-solana-copy-trading-bot-2026-guide-559448259e96
- Pump.fun graduation rate analysis — https://medium.com/coinmonks/pump-fun-api-how-to-track-bonding-curves-graduations-and-pumpswap-on-chain-879b689fbedb

---

*This catalogue records what the free tier offers. Thresholds, provider choices and role assignments are decided in session against measured data. The master roadmap is intentionally left unamended; divergence is recorded in the Phase 1 project log.*
