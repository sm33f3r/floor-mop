# Floor Mop — Phase 2 Roadmap: Ingestion

**Status:** Draft v0.1
**Date:** 6 October 2026
**Parent:** `roadmaps/floor-mop-roadmap.md`, Phase 2
**Inputs:** `roadmaps/floor_mop_phase0_project_log.md`, `roadmaps/floor_mop_phase1_project_log.md`

---

## 1. Objective

From the master roadmap: **a raw, unfiltered, persistent stream.** Connect the chosen feeds through the adapter interface, persist every event with no filtering, deduplicate where concurrent feeds overlap, handle reconnection, backpressure and gaps explicitly, and instrument latency across all three timestamps.

This roadmap adds what a session needs to start: the work owed by earlier phases, the decisions that must come first, and a task list sized at one file per coding-agent prompt.

As in the master roadmap, it contains no thresholds, intervals or durations. Those are set in session against real data.

---

## 2. Starting point

**Built and committed:**
- Repository scaffold, `uv`, `src/` layout, MIT license, public repository
- Layered configuration loader (`config.py`) with no code defaults and secret-safe errors
- Logging module (`logging_setup.py`), JSON lines to rotating files
- 51 passing tests, ruff clean
- Eight survey scripts in `survey/`, never imported by `src/`

**Learned in Phase 1 that shapes this phase** (details in the Phase 1 project log):
- Neither launch relay sends an on-chain event time. The earliest time known for a creation is Floor Mop's own receive time.
- PumpDev led on almost every Pump.fun launch and missed almost none. PumpPortal missed more but is the only relay tagging LetsBonk launches and both migration venues. All of this was measured from one host.
- A meaningful share of launches are not SOL-quoted, and Token-2022 is the majority.
- Event text (names, symbols, URIs) is attacker-controlled.
- PumpPortal bans clients that reconnect aggressively. PumpDev's anonymous tier allows one connection per IP.

---

## 3. Work owed by earlier phases

These belong to Phases 0 and 1 in the master roadmap and are not done. Ingestion cannot be built without them, so they open Phase 2.

| Owed item | Originally due | Why it blocks ingestion |
|---|---|---|
| Record schema | Phase 0 | Persisted events must have a defined shape. Changing it later invalidates the corpus |
| Definition of a hit | Phase 0 | The corpus must capture whatever a hit is later computed from |
| Storage backend | Phase 0 (open question) | Ingestion writes to it |
| Thin adapter interface specified | Phase 1 deliverable | Every feed connects through it |
| Confirmed primary and fallback feed | Phase 1 exit criterion | Phase 1 produced recommendations, not a confirmed choice |

---

## 4. Decisions (session, before any build task)

Each decision is settled in CHAT mode and recorded before the first task that depends on it.

| ID | Decision | Notes |
|---|---|---|
| D1 | Provider roles and concurrency | Confirm or amend the roles in the Phase 1 log, section 6. Decide whether both relays run concurrently (the master roadmap's open question) or one is authoritative. Decide whether the polled sources (RugCheck new tokens, Raydium LaunchLab) are ingested in Phase 2 or only used later. LetsBonk coverage depends on this |
| D2 | Record schema | Two shapes are likely: a **raw event record** (one per message, per feed) and the **candidate record** (one per token, enriched later). Phase 2 needs the first fully and the second's identity and timestamp core. Decide whether the schema is versioned from day one |
| D3 | Timestamp rules | The master roadmap requires three timestamps. Decide how "occurred on chain" is filled when no feed supplies it: leave it null at receipt and backfill it later with its source recorded, or derive it from chain data. Decide how wall and monotonic receive times are stored, given that monotonic time is not comparable across process restarts |
| D4 | Hit definition | Only needs to be precise enough to say which fields the corpus must capture. Evaluation mechanics stay in Phase 6 |
| D5 | Raw payload retention | Store each message verbatim in a quarantined field, or store only validated fields. "Persist every event with no filtering" argues for verbatim. The untrusted-text rule argues for keeping it out of any field later shown to Hermes |
| D6 | Deduplication identity | Which key makes two events the same event across feeds (mint plus event kind, signature, or both), and which copy wins |
| D7 | Storage backend | Flat files, SQLite, Postgres or other. Must suit an unattended run on an operator's own machine, and a collaborator who may not use `uv` |
| D8 | Run host and budget | Where the unattended run happens, and the budget ceiling (master open question 3). Phase 2 needs no paid service if D1 keeps to keyless relays |

---

## 5. Tasks

Each task is one file, or one coherent change where a test file must accompany it, and one coding-agent prompt or manual guide. Order follows dependencies. Tasks after T1 can begin as soon as their decisions are recorded.

| ID | Task | File(s) | Depends on | Done when |
|---|---|---|---|---|
| T1 | Adapter interface | `src/floor_mop/ingest/adapter.py` and its tests | D1, D2 | A documented interface every feed implements: connect, subscribe, yield validated events, report health, close. A fake adapter passes its tests. This also closes the Phase 1 deliverable |
| T2 | Schema module | `src/floor_mop/schema.py` and its tests | D2, D3, D5 | Raw event and candidate-core records construct, validate and round-trip through serialisation. Untrusted text is quarantined. Amounts are raw integers plus decimals |
| T3 | Configuration extension | `config.py`, `config/default.toml` and tests (the three-file exception, as in Phase 0) | D1, D7 | Feed selection, endpoints, reconnect waits, storage location and ingestion switches are all configurable, with no code defaults |
| T4 | Storage layer | `src/floor_mop/storage.py` and its tests | D2, D7 | Records are written and read back with all three timestamps present or explicitly null. This meets the master Phase 0 exit criterion |
| T5 | PumpDev adapter | `src/floor_mop/ingest/pumpdev.py` and its tests | T1, T2, T3 | Creates and pool-creation events become raw event records. One connection. Text is quarantined. Tested against a local fake server |
| T6 | PumpPortal adapter | `src/floor_mop/ingest/pumpportal.py` and its tests | T1, T2, T3 | Creates and migrations, including LetsBonk, become raw event records. Reconnect behaviour respects PumpPortal's ban rules |
| T7 | Polled adapters (only if D1 includes them) | One file each, for example `ingest/rugcheck.py`, `ingest/raydium_launchlab.py` | T1, T2, T3 | Paced polling, first-seen events only, a 403 or 429 stops the adapter without retry |
| T8 | Deduplicator | `src/floor_mop/ingest/dedup.py` and its tests | D6, T2 | Overlapping events across feeds collapse to one candidate. Every feed's sighting is kept, so per-feed latency stays measurable |
| T9 | Supervisor | `src/floor_mop/ingest/supervisor.py` and its tests | T5, T6, T8 | Runs adapters concurrently and handles disconnects, gaps and backpressure explicitly. A failed feed never stops the others. Feed health is logged |
| T10 | Ingest runner | `src/floor_mop/__main__.py` or `cli.py`, with tests | T4, T9 | One command starts unattended ingestion. Shuts down cleanly and flushes storage. Keeps the machine awake where the OS allows it. Detects wall-clock jumps |
| T11 | Corpus report | `src/floor_mop/report.py` or a `survey/` script, decided in session | T4 | From persisted records alone: launch volume, per-feed coverage and latency, gaps, duplicates, timestamp completeness. No token text in output |
| T12 | Unattended run and Phase 2 project log | No code | T10, T11 | The exit criterion is met and the run is written up |

---

## 6. Exit criterion

From the master roadmap: **the ingester runs unattended for a sustained period without data loss, and the corpus is large enough to characterise real launch volume and latency.**

What counts as "sustained", and how "without data loss" is judged, are set in session before T12. Feed health in T9 and the corpus report in T11 provide the evidence.

The master roadmap also asks that ingestion run for a meaningful stretch before Phase 4 begins. Phase 3 work may start while the ingester keeps running.

---

## 7. Carried-forward requirements

These come from Phases 0 and 1 and bind every task.

- **No secrets.** Phase 2's planned feeds are keyless. Any later key comes from the runtime environment, never from the repository.
- **Untrusted text.** Event text is validated, quarantined, never logged in clear and never placed in a field meant for Hermes.
- **Validated identifiers.** Mints and signatures are validated against their formats before use. Invalid messages are counted and discarded.
- **Polite connections.** One connection per WebSocket feed, bounded reconnects, no retry storms. Respect each provider's published limits.
- **Logging.** Through `logging_setup.py`. Rejects and failures are logged with reasons.
- **Requirements files.** Regenerate both after any dependency change.
- **Git.** The coding agent runs no git commands.

---

## 8. Out of scope

- Enrichment, sellability and any filtering. These are Phases 3 and 4.
- GMGN integration, which waits for a real key and is planned as a later improvement.
- The Phase 1 deferred items (project log, section 11). None blocks ingestion. They are revisited when the phase they serve begins.

---

## 9. Risks

| Risk | Nature |
|---|---|
| Schema churn | A schema change after the corpus exists invalidates it. D2 to D5 deserve the most care in this phase |
| Single-host evidence | Feed roles rest on one host's measurements. Another operator's location may change which relay leads |
| Relay availability and terms | Both relays are third-party, free services that can change or vanish. The adapter interface is the mitigation |
| Missing on-chain time | Latency measured against receive time understates true latency until the on-chain time is backfilled |
| Storage growth | An unfiltered corpus grows quickly at observed launch volumes. The backend choice must allow for that |

---

## 10. Open questions

1. Budget ceiling for data infrastructure (master open question 3). It becomes urgent only if a paid feed is added.
2. The collaborator's Python version, which governs whether `requires-python` is loosened (carried over from Phase 0).
3. Whether the corpus is ever shared between operators, which would affect D5 and D7.
