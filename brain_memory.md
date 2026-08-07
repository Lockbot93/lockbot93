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

- **2026-08-03 15:33** — FAILURE: No new incident fingerprints in the window: the Market Scanner session-boundary health-check cluster (26 occurrences) still shows last_seen 07-29 15:00 CT, and the 08-03 session is now the third consecutive clean session including open/close boundaries — leaning handled-or-dormant, but since no fix was ever identified, hold off recording it as handled until at least one more clean boundary.

- **2026-08-03 15:33** — FAILURE: The four DNS-resolution cycle crashes remain the only environment failures, absorbed by the controller retry loop with no recurrence since 07-30 01:29 — environment, not a LOCKBOT defect.

- **2026-08-03 15:33** — FAILURE: A recurring operational miss outside the incident feed: 4 of 10 option entry attempts since 07-30 ended ENTRY_NOT_FILLED (2 PBR, 2 IBIT), with retries at adjacent strikes within minutes — bookkeeping handles it correctly, but the entry pricing is missing fills 40% of the time, which is LOCKBOT behavior, not the environment.

- **2026-08-03 15:33** — The resolved shadow sample doubled 55→107 since 07-30 and the strategy is now statistically distinguishable from breakeven for the first time: 23/107 wins (21.5%, −0.355R) puts the 95% CI upper bound at ~29.3%, below the 33.3% breakeven — with the caveat that the population mixes old fixed-bracket setups with newer ones and carries the previously noted fast-mover selection bias; the marginal 52 resolutions won only 8 (15.4%).

- **2026-08-03 15:33** — The volume-ratio inversion recorded on 07-29 (37.5%→26.7%→25.0% monotone) did not survive the doubled sample: buckets are now 21.1% (n=19), 18.5% (n=27), 23.0% (n=61) with no monotone pattern, so the original inversion was noise; the ratio is logged at weight zero and inert regardless.

- **2026-08-03 15:33** — The JD bull call spread ledger has been corrected: the impossible −155% loss is now recorded as $24 debit / $16 credit / −$8 (−33.3%), consistent with the −35% stop — the defined-risk violation was a ledger error, since fixed, superseding the 07-30 18:32 note.

- **2026-08-03 15:33** — EWZ260821C00037000 exited with reason STOP_LOSS on 08-03 at only −8.1% of its $74 entry debit; the $68 exit credit is ≈65% of a ~$105 peak, suggesting the −35% options stop is measured from highest_value (a trailing stop) rather than entry debit — unconfirmed, but if true the real max loss per option trade is smaller than the sizing note assumed while winners get stopped out on givebacks.

- **2026-08-03 15:33** — A single option stop-loss overwhelms the daily loss budget: −35% of a typical $50-75 debit is $17-26 against a 2%-of-equity budget of ~$5, and on 07-30 the ASHR stop alone (−$11) breached the limit by mid-morning, locking equity entries for the rest of the day — a cross-module interaction (10% option risk cap vs 2% daily loss cap) not previously recorded.

- **2026-08-03 15:33** — The equity entry drought has extended to a third consecutive session (07-30, 07-31, 08-03) with zero entries despite 388 BUY_LONG signals in the window, while options entered twice on 08-03 alone; the daily loss limit cannot explain 07-31, which was a +$39 day, so the cause remains unattributed.

- **2026-08-04 07:50** — The shadow sample contains zero SHORT rows despite ~412 SELL_SHORT signals per window; shadow_trades.py's resolver fully supports SHORT (self-tests cover it), so the gap is at the recording call in the scanner — the discarded half of the signal engine is going unmeasured for free.

- **2026-08-04 07:55** — 2026-08-04: Regime split at n=107 resolved is STRONG_UPTREND 8/50 (16.0%, −0.52R) vs WEAK_UPTREND 15/57 (26.3%, −0.21R), but Fisher two-tail p≈0.24 — still not distinguishable from noise, and both regimes sit below the 33.3% breakeven; any STRONG_UPTREND block is damage limitation on a losing strategy, not a statistically supported edge.

- **2026-08-04 08:24** — Equity entry drought cause located: market_scanner.py gates all share order submission on EQUITY_ENTRIES_ENABLED with an explicit skip message ("approved but EQUITY_ENTRIES_ENABLED is False — no share orders will be submitted. Shadow logging continues."), and the surrounding comment describes deliberately parking capital in options while shadow logging measures the edge — so the three-session drought is very likely this flag set False on purpose, not a defect; confirm the value in the first ~600 lines of lockbot_config.py.

- **2026-08-04 08:24** — The SHORT shadow gap mechanism: market_scanner.py builds shadow rows only from `approved` (results with trade_approved=True), and the shadow writer itself handles SHORT correctly — but SELL_SHORT signals are rejected upstream (SHORT_EXECUTION_NOT_ENABLED, 337/4000 rows) before reaching `approved`, so they never hit record_candidates. Fix belongs at the approval loop, not shadow_trades.py.

- **2026-08-04 08:31** — CONFIRMED 2026-08-04: EQUITY_ENTRIES_ENABLED = False in lockbot_config.py, set deliberately on 2026-07-30 to park capital in options while shadow logging continues — the three-session equity entry drought (07-30/07-31/08-03) was intentional, not a defect; case closed, do not re-investigate.

- **2026-08-04 15:31** — FAILURE: No new incident fingerprints this window: the Market Scanner session-boundary health-check cluster (20 occurrences) still shows last_seen 07-29 15:00 CT, and 08-04 is now the fourth consecutive clean session including open/close boundaries — per the 08-03 plan this is now recorded as dormant/effectively handled; reopen only if it recurs.

- **2026-08-04 15:31** — FAILURE: The four DNS-resolution cycle crashes (07-29/30) remain the only environment failures, absorbed correctly by the controller retry loop with no recurrence since 07-30 01:29 — environment, not a LOCKBOT defect.

- **2026-08-04 15:31** — 2026-08-04 was a −$34.10 (−11.9%) day — roughly 6× the 2% daily loss budget — driven by options (EWZ spread stop −$17 plus open-position marks); equity fell from $263.50 to $252.93, giving back nearly all gains above the ~$250 start, and the profitable-account condition is only barely still met.

- **2026-08-04 15:31** — DAILY_LOSS_LIMIT_REACHED rejections jumped from 1,175 to 3,871 of the last 4,000 signal rows — a near-total lockout of the window — but the limit only suppresses the equity path, which is already disabled by EQUITY_ENTRIES_ENABLED=False, so it currently constrains nothing that is taking risk.

- **2026-08-04 15:31** — The PCG put's highest_value reached exactly +50.0% of its entry debit ($84.00 against $56.000…01) without triggering the +50% take-profit — consistent with a strict-inequality or float-epsilon boundary miss — and the gain has since sat unrealized; if the suspected peak-referenced −35% stop is real, the position would now exit near breakeven instead of +50%. Latent bookkeeping/comparison issue, n=1.

- **2026-08-04 15:31** — The 08-04 EWZ bull call spread exited STOP_LOSS at −39.5% of its $43 debit, ~4.5 points beyond the −35% threshold — software stops can exceed the configured loss by execution slippage (about $2 here); small but worth tracking as options are the only active risk-taker.

- **2026-08-04 15:31** — Shadow resolved grew 107→119 with 24 wins (20.2%, −0.395R, Wilson 95% upper ≈28%, still below the 33.3% breakeven); the regime gap widened slightly to STRONG_UPTREND 8/59 (13.6%) vs WEAK_UPTREND 16/60 (26.7%), two-tail p roughly 0.08–0.11 — tightening from p≈0.24 at n=107 but still not significant; the marginal 12 resolutions (1 win) are too few to read alone.

- **2026-08-04 19:23** — 2026-08-04: The brain's editor sandbox does not mount the LOCKBOT folder — project files are readable via read_project_file but NOT writable; code fixes must be delivered as patch files for the user to apply by hand. A patch adding SELL_SHORT candidates (rejected as SHORT_EXECUTION_NOT_ENABLED) to the shadow-log population in market_scanner.py was drafted and handed to the user on this date; check shadow side breakdown later to confirm it was applied.

- **2026-08-04 22:31** — 2026-08-05: First strategy-proposal session ran three rules (pullback-to-EMA21 long, VWAP-reclaim long, failed-bounce short); all returned TOO FEW TRADES because the backtest history is only 2-3 days deep (8-10 trades each, all negative expectancy) — the binding constraint on rule discovery is history depth, not rule design, so re-run these same three once more days accumulate rather than proposing new variants.

- **2026-08-04 22:34** — 2026-08-05: Confirmed the 08-04 SHORT shadow-logging patch was NEVER applied — shadow log still 119 resolved / 0 SHORT and market_scanner.py stage-1 filter still drops SELL_SHORT at SHORT_EXECUTION_NOT_ENABLED before stage 2; regenerated an improved patch (short_shadow_patch.md, 3 edits: tag shadow_only in stage-1 and advance to stage 2, finalize without trade_approved, extend shadow population to approved+shadow_shorts) and handed it to the user; verify with get_shadow_breakdown(side) after the next session.

- **2026-08-04 22:39** — 2026-08-05: PCG +50% boundary miss ROOT-CAUSED — decide_exit's >= comparison is correct (self-test covers exact-at-target), but net_fill_dollars computes float(price)*100 without rounding, so PCG's entry_debit persisted as 56.00000000000001, making the take-profit target 84.00000000000001; the $84.00 quote failed >= by 1e-14. Patch delivered (pcg_float_debit_patch.md): round(amount, 2) in net_fill_dollars plus rounding on load; until applied, PCG needs $84.01 to trigger. Not a strict-inequality bug — supersedes the 08-04 15:31 hypothesis.

- **2026-08-05 07:00** — User intends to add Telegram user ID 6854250494 to TELEGRAM_ALLOWED_USER_IDS in .env (hand edit + bot restart pending as of 2026-08-05); once done, brain should restart the Telegram process and confirm — and note the new user will hold /flatten authority.

- **2026-08-05 07:12** — 2026-08-05: User directive — act as a trader, not just an operator: every session resolve shadows and re-read the regime split, re-run the three parked rules end of this week (~5-6 days history), recommend blocking STRONG_UPTREND at n≈200 if the gap holds, and bring any rule clearing 30+ backtest trades with positive expectancy to the user with sample size stated plainly.

- **2026-08-05 08:36** — 2026-08-05: User wants a reminder when home between 5:45-6:00pm CT today: (1) give the brain a pipeline to Claude so filed engineer items can be executed automatically, (2) enable day trading and shorts — note shorts are code-blocked (engineer item 46169c86) and disabled under $2,000 equity, and day-trade testing awaits engineer item 4cf2ab9f; surface this in any session near that time.

- **2026-08-05 10:30** — 2026-08-05: Universe funnel diagnosed — universe.py keeps top-N most-liquid $5-50 names, then universe_volatility.py's 1.25-3%/day ATR band cuts that to 50 (survivors are quiet utilities/ETFs, so the band is the binding filter); safe expansion is raising UNIVERSE_TOP_N in lockbot_config.py (hand edit, not runtime-changeable) to feed the same band a deeper pool — do NOT widen the band itself mid-test, it would change the measured population; verify with `python build_universe.py --dry-run --top 600`.

- **2026-08-05 15:31** — FAILURE: The only incident in the 7-day window is the already-recorded cluster of 4 DNS-resolution cycle crashes (07-29/30) to paper-api.alpaca.markets — an environment failure, absorbed correctly by the controller retry loop, no recurrence in 6 days: handled.

- **2026-08-05 15:31** — FAILURE: The Market Scanner session-boundary health-check cluster has now aged out of the 7-day incident window entirely (zero occurrences; 08-05 is the fifth consecutive clean session including open/close boundaries) — it stays recorded as dormant per the 08-04 decision; reopen only if it recurs.

- **2026-08-05 15:31** — OPTIONS_SHADOW_MODE flipped to True between 08-04 and 08-05, so with EQUITY_ENTRIES_ENABLED already False LOCKBOT now submits no new orders on any path — the only live risk is the two open option positions (PCG put $56 debit, IBIT spread $26 debit, $82 combined) whose software exits options_manager still runs every cycle.

- **2026-08-05 15:31** — The PCG float-debit fix is applied and confirmed in state (entry_debit now reads exactly 56.0 via constructor rounding, engineer item 734d2e7a resolved), but PCG remains open at highest_value $84.00 — the exact +50% touch happened before the fix, so realizing the gain now requires the position value to re-reach $84.00.

- **2026-08-05 15:31** — Shadow resolved grew 119→130 with ZERO wins in the marginal 11 (and only 1 win in the 23 resolutions since 08-03); cumulative is 24/130 (18.5%, −0.446R), pushing the Wilson 95% upper bound to ~26% versus the 33.3% breakeven — the strategy's shortfall is deepening, not narrowing.

- **2026-08-05 15:31** — The regime split is now perfectly balanced at n=65 each: STRONG_UPTREND 8/65 (12.3%, −0.63R) vs WEAK_UPTREND 16/65 (24.6%, −0.26R), two-tail p≈0.07 — the closest to significance yet but still short; per the standing plan, hold the STRONG_UPTREND block recommendation until n≈200.

- **2026-08-05 15:31** — The scan universe grew 47→52 symbols without the UNIVERSE_TOP_N hand edit being applied, so the volatility-band survivor count drifts on its own by a few names day to day — still roughly a third of the 150 cap.

- **2026-08-05 18:38** — 2026-08-05: Item 46169c86 (short shadow-logging) reopened as PREMATURE only — code review passed in full (flag True, shadow_only stage-1 tagging, full stage-2 ride, no trade_approved path, measured=approved+shadow_shorts, self-tests cover it); confirm after the next market session once get_shadow_breakdown(side) shows SHORT rows — no code re-work is needed.

- **2026-08-05 21:56** — 2026-08-06: First swing-horizon backtests ran with real sample sizes (218-245 trades over ~140 days each) — the swing machinery from item 4cf2ab9f works and the history-depth constraint is gone at swing; but all three parked rules (pullback-to-EMA21 long, VWAP-reclaim long, failed-bounce short) scored decisively NEGATIVE at swing: 9.6%/12.2%/12.8% win rates vs 33.3% breakeven, expectancy -0.6 to -0.7R — retire these three rules at both horizons, new ideas needed.

- **2026-08-05 22:03** — 2026-08-06: Swing backtests are now 0-for-9 with dip-buying, strength-buying, and weakness-shorting ALL failing at 9-13% win rates (n=218-442 each) — the failure is structural, not rule design: the 2:1/5%-stop swing test needs ~10% moves in a week from a universe filtered to 1.25-3%/day movers; next lever is the universe (deeper UNIVERSE_TOP_N pool or higher-volatility names), not more rule variants.

- **2026-08-06 03:56** — 2026-08-06: Day-horizon backtests now run with real samples (299-339 trades over ~60 days each) and all three rule families scored 7-13% wins vs 33.3% breakeven — identical to the swing failures — so the 1.25-3%/day universe structurally cannot reach 2:1 targets at EITHER horizon; the lab is now 0-for-12 across opposite styles, confirming the binding constraint is the symbol pool (engineer item 7425d2f7), and no further rule variants should be proposed until it changes.

- **2026-08-06 04:40** — 2026-08-06: Item 7425d2f7 verified fixed — strategy lab swing backtests now draw from lab_universe.csv (78-79 TOO_WILD rejects, 3.0-7.7%/day ATR) while universe.csv and the live 52-symbol scan population are untouched; the retired pullback-to-EMA21 swing rule reran at 2,890 trades with win rate 9.6%→27.7% (-0.169R), confirming the structural cap is gone though the rule itself remains negative — the 0-for-12 lab record predates this pool and rule ideas can now be retested meaningfully.

- **2026-08-06 07:08** — 2026-08-06: First three fresh rules in the new lab pool all NEGATIVE (lab now 0-for-16): momentum-continuation long 12.1% (n=388) and short 8.8% (n=443) are slaughtered, while the loose EMA21-pullback control remains the pool's best at 27.7% — but tightening it with RSI 25-40 oversold + above-VWAP collapsed to 11.0% (n=200), so deep oversold in 3-8%/day names is a falling knife, not exhaustion; the surviving direction is SHALLOW mean-reversion variants near the 27.7% control, not deeper dips and not continuation.

- **2026-08-06 09:33** — 2026-08-06: shallow-pullback-trend-intact (close between ema_9/ema_21, RSI 45-60, MACD>signal, swing) scored 10.4% on n=309 — every filter added to the 27.7% loose EMA21-pullback control makes it WORSE (deep oversold 11%, shallow trend-intact 10.4%), so the control's relative strength does not come from any dip-quality condition tested; lab is 0-for-17, and the one constant across all 17 failures is the 2:1 reward-to-risk exit structure itself, which is now the prime suspect over entry design.

- **2026-08-06 11:41** — 2026-08-06: First SHORT rows ever appeared in shadow_trades.csv (ITUB 15:30Z, PCG 16:30/16:40Z, all WEAK_DOWNTREND) — the short shadow-logging fix works live; item 46169c86 verified closed, and SELL_SHORT setups are now measurable for the first time.

- **2026-08-06 15:31** — FAILURE: The 7-day incident window is completely empty for the first time — zero occurrences, zero fingerprints: the session-boundary health-check cluster and the DNS crashes have both aged out with no recurrence, nothing is recurring, and there is no active LOCKBOT defect in the incident feed. The only open engineering items (etf_portfolio journal pollution, lab R:R rigidity) are self-filed and awaiting the engineer, not incident-driven.

- **2026-08-06 15:31** — Shadow resolved grew 130→141 at 25 wins (17.7%, −0.468R, Wilson 95% upper ≈24.9% vs 33.3% breakeven); the post-08-03 cohort is now 2 wins in 34 resolutions (5.9%), which under the pre-08-03 rate of 21.5% has probability ≈1.4% — the recent cohort is measurably worse than the earlier population, consistent with H14's wide-target-arrival story but not attributable without a target-width split.

- **2026-08-06 15:31** — On 2026-08-06 the daily loss limit tripped at −$8.14 (−3.07%) with zero orders submitted and zero exits on any path (both entry paths disabled, options_manager reported 0 exits/0 closed) — the limit is computed on equity change including unrealized marks on open option positions, so merely holding options can lock the equity entry path without a single realized loss; sharpens the 08-03 cross-module note, which only covered realized option stops.

- **2026-08-06 15:31** — Regime split at n=141 is STRONG_UPTREND 8/66 (12.1%, −0.64R) vs WEAK_UPTREND 17/75 (22.7%, −0.32R) — directionally intact but significance loosened slightly (two-tail p≈0.10 vs ≈0.07 at n=130); per the standing plan, the STRONG_UPTREND block recommendation still waits for n≈200.

- **2026-08-06 19:00** — 2026-08-06 design decision on item 8e24ae42 (variable R:R in strategy lab): pass criterion is rule-minus-random-control per ratio (control run as a seeded distribution, not one draw), NOT rule-vs-breakeven; ratio sweep is 1.0/1.5/2.0/3.0 with stop fixed and target scaling; timeouts are booked at mark-to-market R rather than dropped so all ratios share a per-setup denominator; a pass requires a coherent cross-ratio pattern or held-out replication, not a single p<0.05 ratio.

- **2026-08-06 19:23** — 2026-08-07 news-timestamp experiment pre-registration: feature is per-symbol article-count-vs-trailing-baseline (created_at only, articles naming >3 tickers dropped, only articles before day T close count, entry T+1 open, frozen news snapshot); acceptance requires ≥500 spike events over ≥3 calendar years (no year >40%), beating the seeded random-entry control distribution in each year with consistent sign, beating a price-matched no-news control by ≥+0.10R pooled (timeouts at mark-to-market R), and surviving one held-out year — the price-matched control is mandatory because news volume is confounded with the price move itself.

- **2026-08-06 19:32** — 2026-08-07 exit-structure experiment pre-registration: claim revised — exits cannot change sign vs a martingale (optional stopping) but asymmetric exits CAN harvest autocorrelation, so trailing-stop arms are a real test; acceptance for entry×exit interaction is rule ≥ +0.10R over the seeded random-control distribution mean AND above its 95th percentile pooled, same-sign gap every calendar year (≥3 years, no year >40% of trades, ≥300 trades/arm, timeouts at mark-to-market R); exit main effect (random+trail vs random+fixed ≥ +0.10R after costs) is a separate claim, and equal lift on rule and random = better exit, not an edge; slippage must be charged on trail exits; ATR-adaptive brackets are symmetric and test width, not skew.

- **2026-08-06 19:41** — 2026-08-07: options_manager.py stop is ENTRY-referenced, not peak-referenced — self-test states "entry 74, stop level 48.10" (65% of entry debit); the 08-03 hypothesis that the -35% stop trails highest_value is contradicted by source, and the EWZ -8.1% STOP_LOSS exit is explained by the stop deciding on quoted value with the closing fill landing at 68 — fill slippage, not a trail.

- **2026-08-06 19:41** — 2026-08-07: exit-structure sweep result (5,307 seeded random entries, 60 symbols, 3.3y): trail lifts random long entries +0.05R over fixed bracket, positive all four years — but this is below the pre-registered +0.10R-after-costs bar (2026-08-06 19:32), and the drift confound is unresolved: a trail holds winners longer, so differential time-in-market under positive drift produces the same signature as autocorrelation; discriminator is the same sweep on random SHORTS (drift predicts the lift flips/vanishes; autocorrelation predicts it survives).

- **2026-08-06 20:04** — 2026-08-07 crypto oversold pre-registration COMMITTED: rsi<35 swing long, full pool incl. BTC/ETH, forward-only holdout, n>=300 AND >=180 days, week-matched seeded control (seed 20260807, >=500 draws), pass requires edge>=+0.10R AND >95th pct AND net>0 AND non-negative edge both calendar halves; any failure kills the whole rsi-oversold-long family on crypto permanently (no variants, no ex-BTC/ETH rescue); ONE attempt ever; mirrored rsi>65 short arm runs as regime-luck discriminator (pass without it = shadow only, no capital); stated prior of passing 0.15.

- **2026-08-06 21:28** — 2026-08-07 CONTAMINATION: all 864 candidate rules in both rule_search sweeps (run 2026-08-05) referenced VWAP while VWAP was computed as a 365-day cumulative mean instead of session-resetting (fixed 2026-08-06); the searches were never re-run post-fix, so the entire swing indicator-search record including r0315 is void. CLEAN and still standing: exit-structure work (ema_21/macd only), news test, crypto work, momentum, low-vol, illiquidity, post-fix drift control, and the hand-built lab rules that used no VWAP (loose EMA21-pullback control 27.7% n=2,890; shallow trend-intact 10.4% n=309; momentum continuation 12.1%/8.8%). Corrected re-run in progress.

- **2026-08-06 21:46** — 2026-08-07 overnight decomposition (46,970 symbol-days, 60 symbols, 3.3y): 98% of total return accrues close-to-open (+0.074%/day, positive every year) and intraday is ~0 (+0.0011%/day) — but the standalone overnight-long is dominated by buy-and-hold at EVERY cost level including zero, because the intraday leg it pays daily round trips to avoid is not negative; only surviving uses are (a) execution-timing overlay (enter at close/exit at open on trades taken anyway, pre-registered at +0.05R vs randomized-intraday-execution control) and (b) liquidity-tercile split on existing data to test the illiquidity-compensation hypothesis; standalone dead unless intraday pooled <= -0.02%/day on longer history.

- **2026-08-06 21:55** — 2026-08-07 verdict, given as the gate on the user's decision: pairs trading is a NO (~3% chance of helping this account) because it requires shorting, blocked under $2,000, and the long-only rotation variant reintroduces drift exposure; the remaining untested families (seasonality, dividend capture, vol targeting, rebalancing premium, macro/regime) all fail on capturability at $253 — accessible strategy space declared exhausted at this size, ETF sleeve is the correct expression; revisit pairs first if equity reaches $2,000 and shorts unlock.

- **2026-08-06 22:42** — 2026-08-07 failure mode to carry: twice in one day I reported zero equity positions because the default position read hides reserved symbols — SCHD and SCHG were held the whole time; always read positions with reserved symbols included before claiming the book is flat.

- **2026-08-06 22:52** — 2026-08-07: Governance module audited on day one — sound, not vetoed; two items filed (9708bab4: corrupt/deleted governance.jsonl silently reads as "Nothing standing" and MAX_SCAN_LINES drops the newest records; 227c9271: advisory vetoes surface in no brief after filing); evidence heuristic accepted as-is (any digit or ref binds — errs toward binding by design); standing condition on the 5,856-rule ADX/DI sweep: ~293 expected false passes at p<0.05, so any pass is a held-out-replication candidate only, and I halt if one heads toward capital unreplicated; first agenda set (journal pollution, backup, crypto shadow wiring, PCG-sequenced $600 reset, time-of-day, vol-scaled).
