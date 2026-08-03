# LOCKBOT brain notes

Findings worth carrying between sessions. Written by lockbot_brain.py;
safe to edit or prune by hand.

- **2026-07-29 22:40** — The 27.3% win rate / −0.18R shadow result measures the OLD fixed 2%/4% bracket only. All 55 resolved setups have a 4.00% median target; all 102 unresolved have a 6.38% median target. Zero adaptive-bracket setups have resolved yet, so no evidence about the current configuration exists.
- **2026-07-29 22:40** — SHADOW_MAX_DAYS was 3, sized for the old bracket. With adaptive brackets a 6.4% target cannot be reached in 3 days by a name moving ~2.3%/day, so wide setups timed out unresolved and never entered the sample while tight ones did. Raised to 10. Any win rate computed before this change is biased toward fast movers.
- **2026-07-29 22:40** — Volume ratio, the tiebreaker that ranks which setups get taken, is inverted and monotonic on the fixed-bracket data: 1.10–1.25 → 37.5% wins, 1.25–1.75 → 26.7%, 1.75+ → 25.0%. The largest bucket is the worst. Unverified against adaptive-bracket data.
- **2026-07-29 22:40** — Confidence cannot rank setups. signals.csv shows real variance (20/40/60/80/100) but all 157 shadow setups scored exactly 100, so the surviving population has zero spread. The score works; the filter leaves nothing to discriminate between.
- **2026-07-29 22:40** — Alpaca removed daytrade_count on 2026-07-06; the old guard read the resulting None as 0 and never fired. day_trade_tracker.py now counts round trips locally and blocks on a counting failure rather than assuming room.
- **2026-07-29 22:40** — Options have no broker-side bracket on Alpaca (options support only simple and mleg, day-only TIF). options_manager.py holds the stop in software and is the sole exit authority. If it stops running, open option positions have no stop at all.
- **2026-07-29 22:40** — Shorts are disabled under $2,000 equity, so roughly 400 SELL_SHORT signals per session are generated and discarded. Half the signal engine's output is unusable on the small profile.

- **2026-07-30 00:13** — Regime breakdown is now available and splits the fixed-bracket shadow sample in two: STRONG_UPTREND 4/24 wins (16.7%, -0.50R) vs WEAK_UPTREND 11/31 wins (35.5%, +0.065R) against a 33.3% breakeven rate; at n=24 and n=31 this is suggestive but not distinguishable from noise (Fisher two-tail p is roughly 0.13), and both groups are entirely old 2%/4% bracket setups.

- **2026-07-30 00:13** — The first two completed equity trades (VTEB, CELH, entered 2026-07-28) both exited with reason EXTERNAL_CLOSE within 9 seconds of each other at 2026-07-29T13:33Z, at -1.39% and +0.03% — neither near its 2% stop nor its 4% target — so something outside LOCKBOT's exit path flattened both positions and no LOCKBOT-owned exit (bracket fill, stop, or target) has yet been exercised end to end.

- **2026-07-30 00:13** — The position tracker's trailing stop is 0.5% off the running high (NVO high 51.97 -> stop 51.71015; LVS high 49.22 -> stop 48.9739) while the adaptive bracket stop on those same trades is 3.47% and 3.90% wide with targets at 6.95% and 7.79%; the trail is roughly one seventh of the stop distance, so it would exit around +0.7% (about 0.20R) and, if ENABLE_PAPER_EXITS were ever flipped from its current false, would need an ~83% win rate to break even. Latent, not active.

- **2026-07-30 00:13** — Break-even and trailing-stop flags went true on both open positions at peak gains of only 1.19% (NVO) and 1.51% (LVS), confirming the activation thresholds were sized for the retired 2%/4% bracket and were not rescaled when adaptive brackets turned on.

- **2026-07-30 00:13** — The first two adaptive-bracket equity entries have actually reached the broker: NVO registered with a 3.47% stop / 6.95% target and LVS with 3.90% / 7.79%, both risking $1.78-$1.89 on ~$249 equity (0.71-0.76%, inside the 1% cap), so the adaptive path is live even though the resolved shadow sample is still 55 and still contains zero adaptive-bracket outcomes.

- **2026-07-30 00:13** — The scanner covered only 47/47 symbols against a max_scan_symbols of 150 while universe.csv is 20.8 hours old, so the effective scan universe has shrunk to under a third of capacity and the universe file is not being refreshed daily.

- **2026-07-30 00:30** — CAUSE FOUND for the cancelled brackets on NVO and LVS: a self-test in lockbot_telegram.py fed a valid challenge word through handle_message, which routed to _do_flatten and called close_all_positions against the live broker. Not an unknown process and not related to the VTEB/CELH external closes. Fixed by making the executor injectable plus an EXECUTION_DISABLED interlock. Do not re-investigate this as a mystery.

- **2026-07-30 00:30** — The VTEB and CELH EXTERNAL_CLOSE exits on 2026-07-29 were close_all.py being run manually, not a LOCKBOT exit path failing. trade_manager.py could not resolve them because it only checked whether the PARENT order was terminal, and a filled parent with cancelled legs is neither terminal nor fillable — that is why completed_trades.csv sat empty for eight days. Fixed with _bracket_was_dismantled plus _find_external_exit.

- **2026-07-30 00:30** — Distinguish REGISTERED from PROTECTED. A row in lockbot_pending_trades.csv means LOCKBOT recorded a bracket, not that one is live at the broker. Those diverged when the legs were cancelled and the rows stayed. rearm_brackets.py checks the broker for a working stop; lockbot_hud.py uses it and shows a third state, "unknown", for an unreachable broker rather than assuming safety.

- **2026-07-30 08:36** — User wants Pushover briefings (--brief) used to alert when the account turns profitable, i.e. equity above the ~$250 starting line, rather than routine status pings.

- **2026-07-30 09:53** — User asked (2026-07-30) to keep a close eye on NVDA and AMD — personal watchlist items; both are outside the $5-$50 universe so the scanner never sees them, check on request via web search or quotes.

- **2026-07-30 11:07** — 2026-07-30: PBR260821C00019000 recorded as -100% CLOSED_AT_BROKER five minutes after entry, but the broker still shows the buy order open with status new — the entry likely never filled and the -$56 loss is a bookkeeping artifact of an unfilled order, which is still live and could fill.

- **2026-07-30 11:07** — 2026-07-30: day_trade_tracker returned 0 round trips despite the ASHR bull call spread entering 14:10Z and exiting 14:35Z the same day — the local counter is not counting options round trips, so PDT headroom may be overstated.

- **2026-07-30 12:34** — User is out of town from 2026-07-30 until Sunday 2026-08-02; wants proactive updates via Pushover briefings while away, and can ask questions through the read-only Telegram channel.

- **2026-07-30 18:32** — FAILURE: Market Scanner failed self-repair 33 times in 7 days across two distinct clusters: the 07-23 cluster (exit code 1, execution failure) looks like a real crash, but the 07-27 to 07-29 cluster is health-check failures whose first/last timestamps land exactly at market open (08:30 CT) and close (15:00 CT), all three repair attempts fail, and yet the scanner reports HEALTHY the next cycle — that pattern points to a LOCKBOT defect, a health check that fails on a normal session-boundary condition and a self-repair that does nothing, and it is recurring and unaddressed, not handled.

- **2026-07-30 18:32** — FAILURE: Four cycle crashes overnight 07-29/30 were DNS resolution failures to paper-api.alpaca.markets; the controller stayed up and retried on schedule. That is the environment failing, not LOCKBOT, and the retry behavior handled it correctly.

- **2026-07-30 18:32** — NVO and LVS both exited EXTERNAL_CLOSE simultaneously at 2026-07-30T15:51:55Z (-0.60% and +2.50%), so all four completed equity trades to date ended via EXTERNAL_CLOSE and zero LOCKBOT-owned exits (stop fill or target fill) have completed end to end.

- **2026-07-30 18:32** — The JD bull call spread ($29 debit, max possible loss $29) was recorded closing 5 minutes after entry for a NEGATIVE exit credit of -$16, booking -$45 (-155%) — a defined-risk structure lost more than its defined risk, which is impossible unless the closing order's legs were inverted or the ledger is wrong.

- **2026-07-30 18:32** — The 2026-07-30 11:07 note about the PBR -$56 artifact is superseded: both PBR long-call attempts are now recorded as ENTRY_NOT_FILLED with $0 P&L, so an unfilled-entry detection path exists and corrected the bookkeeping.

- **2026-07-30 18:32** — DAILY_LOSS_LIMIT_REACHED is the second-largest rejection reason at 1,175 of the last 4,000 signal rows, meaning the 2% daily loss limit fired and blocked entries during part of the window — its firing is not recorded anywhere in the existing notes.

- **2026-07-30 18:32** — Equity is $263.50, above the ~$250 starting line — the profitable-account condition the user asked to be alerted on via Pushover is now met.

- **2026-07-30 18:32** — universe.csv has been refreshed (now 10.6h old versus 20.8h before) but the scan universe is still 47 symbols against a 150 cap, so the shrunken universe is produced by the universe builder itself, not by file staleness.

- **2026-07-31 18:31** — FAILURE: The recurring Market Scanner session-boundary health-check failure (26-27 occurrences, previously flagged as a LOCKBOT defect) shows no occurrences during the 07-30 or 07-31 sessions — last_seen is 07-29 15:00 CT — so it has either been fixed or gone quiet; two clean sessions is suggestive but watch the next few open/close boundaries before recording it as handled.

- **2026-07-31 18:31** — FAILURE: The four DNS-resolution cycle crashes remain the only environment failures in the window and were absorbed by the controller retry loop as already noted; no new incident fingerprints appeared in this pass.

- **2026-07-31 18:31** — EWZ260821C00036500 long call exited TAKE_PROFIT at +55.2% (+$37) on 2026-07-31 with correct bookkeeping — the first LOCKBOT-owned exit of any kind (equity or options) to complete end to end, confirming the software +50% take-profit path works for single-leg longs.

- **2026-07-31 18:31** — LOCKBOT opened its first bearish position ever, a PCG260821P00017500 LONG_PUT in STRONG_DOWNTREND on 2026-07-31, so the options path expresses bearish signals that equities cannot (shorts disabled under $2,000 equity).

- **2026-07-31 18:31** — Options sizing appears to measure risk against the -35% software stop, not max loss: the EWZ call's $74 debit is 27% of the $270 equity but 35% of it is $25.90 (9.6%), just under the 10% cap, and the two open positions' combined debit of $130 is 48% of equity with only a software stop behind it — latent while options_manager is healthy, but the JD trade already showed an exit exceeding defined risk.

- **2026-07-31 18:31** — risk_state trade_date is stuck at 2026-07-29 with trades_submitted_today=2 despite four option entries on 07-30/31, so option entries do not touch the daily trade counter — a second options-blind counter distinct from the already-noted day_trade_tracker gap.

- **2026-07-31 18:31** — Zero equity entries or pending equity trades occurred across the full 07-30 and 07-31 sessions while BUY_LONG signals kept firing (374 in the last 4,000 rows) and options entered four times — the equity entry path has gone quiet for two sessions with no attributed cause.

- **2026-08-02 22:29** — User prefers the en-GB-RyanNeural edge voice over the default en-GB-ThomasNeural; set via LOCKBOT_EDGE_VOICE in .env (brain cannot write .env itself, user must edit it).

- **2026-08-02 23:38** — User asked (2026-08-03) for hourly profit updates during the trading session via Pushover briefings — wants P&L pushed every hour while the market is open, not just the profitable-account alert.
