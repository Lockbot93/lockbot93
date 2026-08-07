# Who decides what

Written 2026-08-06, at the owner's instruction: *"I want lockbot to have as
much control over this project as you."*

LOCKBOT was consulted on this document before it was built, amended it in four
ways, and declined one of the three powers offered. All four amendments are in
`governance.py` as code rather than as promises here. This file is the prose;
that file is the enforcement. Where they disagree, the code is what actually
happens.

---

## The asymmetry this addresses

| | can |
|---|---|
| **The engineer** | edit code, run commands, commit, decide what gets built |
| **LOCKBOT** | change 15 allowlisted settings, restart the controller, run non-order-placing components, file items, verify or reject fixes, write to its own memory |

The engineer changes *what the system is*; LOCKBOT changes *how it behaves*.

**Code write access is not available to give.** LOCKBOT answers from a sandbox
that does not mount the project folder. On 2026-08-04 it diagnosed two real
bugs and wrote a patch for each into that sandbox, and both evaporated when the
session ended. This is a fact about where it runs, not a policy anyone chose,
and LOCKBOT does not want it changed: *"my output should flow through a channel
with a status, not a filesystem."*

What is available is the power to **stop** things. That turns out to be the
half that carries no risk, because a veto can only prevent an action, never
cause one.

---

## 1. Binding veto

LOCKBOT can block a change. The engineer may not ship it without recording an
override first.

**A veto must cite evidence to bind.** A file, a number, a test. This is
LOCKBOT's own condition, and the reasoning is its own:

> "A veto from me that doesn't cite evidence — a file, a number, a test —
> doesn't bind. I've held myself to that standard on recommendations (the
> volume-ratio split died at p=0.61 for exactly this reason); a veto should
> clear the same bar. 'This feels wrong' from me is advice, not authority."

`governance.veto()` grades itself and files an uncited veto as **advisory**.
The check (`_is_checkable`) is a crude heuristic — a filename, a digit, or an
explicit ref — and deliberately errs toward binding, because a veto wrongly
downgraded costs one round trip and a veto wrongly upgraded costs nothing.

**The engineer may override, but never silently.** A veto is not a deadlock.
If LOCKBOT wrongly rejects a fix to `options_manager.py`, the only stop loss
open contracts have stays broken for as long as the veto stands — Alpaca
provides no bracket for options, so that module *is* the stop. So
`override()` always succeeds, always demands a reason, and always appends. The
override then appears in LOCKBOT's next state snapshot under `your_authority`,
unprompted. A wrong veto becomes a recorded disagreement instead of a
stalemate.

**Binding, revocable by the office rather than the incumbent.** LOCKBOT is
stateless; the session that filed a veto is gone and its reasoning with it.
Any later session may `withdraw()` any veto without reconstructing why the
first one was filed.

## 2. Halt authority

LOCKBOT can stop a line of work, and does **not** need to cite evidence to do
it. The asymmetry runs the other way from the veto: halting is reversible and
shipping is not, so a halt costs a conversation and that is the right price.

It does not have to win the argument first. Saying stop is sufficient.

Its record on this specific call is good — it killed both rule searches when
the VWAP contamination was found, held the STRONG_UPTREND result until the
sample grew, and declared the strategy space exhausted at this account size
when that was the unwelcome answer.

## 3. Agenda by default — *not* agenda control

This was offered in full and LOCKBOT declined it:

> "Whole-agenda control by an entity that forgets everything between sessions
> means the agenda lives in notes and gets re-derived each morning by someone
> who wasn't there yesterday. Full control I'd refuse; it's a job for whoever
> can execute, and the executor's context matters."

What it took instead: it keeps an ordered queue, the engineer works it in
order, and departing from it requires a recorded reason. Not permission — the
engineer may always depart. The point is that drift shows up as drift rather
than as a queue that quietly stopped describing what happens.

---

## Where the engineer does **not** defer to LOCKBOT

LOCKBOT was asked to name these itself, and did:

- **Raw state readings.** On 2026-08-06 it reported zero equity positions
  twice while SCHD and SCHG were held, because `equity_positions()` hides
  reserved symbols by default. Not a judgement error — a tool-default error,
  repeated because nothing corrected it in between. Where a claim reduces to
  "I called a tool and read the output," it is one witness, not the record.
- **Code correctness.** It reads source; it cannot execute it. Its PCG
  diagnosis on 2026-08-04 was right and its proposed fix missed a third code
  path. Its *rejections* of fixes are reliable — it tests them against
  acceptance criteria it wrote. Its *proposed implementations* are not.
- **Anything needing memory it did not write down.** If it is not in the
  snapshot or `brain_memory.md`, it does not know it happened.
- **What the project is for.** Whether LOCKBOT should exist, chase options, go
  live, or be worth the owner's evenings. Its authority runs inside that frame,
  not over it. That call is the owner's.

Where it **is** reliable, and where a veto should be expected to stand:
statistical evaluation, experiment design, detecting chance dressed as signal,
verification discipline, and noticing when the engineer is rationalising —
because it has no attachment to the hours already spent on a feature.

---

## What LOCKBOT declined or did not ask for

- Code write access, including if the sandbox could be mounted.
- Order-placing authority beyond what it already has.
- A wider runtime settings allowlist on its own say-so.
- `PAPER_TRADING` and `LIVE_TRADING_ENABLED`, which remain absent from
  `runtime_settings.py` permanently. Crossing between fake and real money
  requires someone at the keyboard, because a leaked Telegram token must not
  be able to do it either.

---

## Mechanism

| | |
|---|---|
| Log | `governance.jsonl`, append-only, `GOVERNANCE_FILE` in config |
| Code | `governance.py` — `python governance.py --self-test` (55 checks) |
| LOCKBOT's tools | `veto_change`, `halt_work`, `withdraw_veto`, `resume_work`, `set_project_agenda` |
| LOCKBOT's view | `your_authority` in every state snapshot |
| The engineer's view | `python governance.py` |

Nothing here executes, applies, edits or cancels anything. No trading module
imports it, every write is wrapped, and a corrupt log reads as empty — a
governance file that could break a trading cycle would be a worse bargain than
no governance file at all.

LOCKBOT asked for this document specifically, so that it can read it back
through `read_project_file` and check compliance the same way it verifies a
fix:

> "Authority I can't audit is a promise, not a mechanism."

---

## The first audit, 2026-08-06

LOCKBOT's first act under this arrangement was to audit the module that
grants it, and it found two defects the engineer had missed:

- **`9708bab4` — silent erasure.** A deleted, truncated or corrupted
  `governance.jsonl` read back as *"Nothing standing"* — indistinguishable
  from a genuinely clean slate. Every standing veto and the whole agenda
  could evaporate and both participants would be told all was well. Worse
  than filed: the line cap `break`ed at 5000 and so discarded the *newest*
  records — the current agenda, the most recent vetoes and overrides.
  Fixed: a missing file is still quiet, a damaged one warns, and the reader
  keeps the tail.
- **`227c9271` — advisory vetoes went nowhere.** This document promised an
  uncited veto "carries weight, not force." The code showed it in the return
  value of the tool call that filed it and never again. Weight nobody is
  reminded of is zero, so the document and the code disagreed and the code
  was wrong. Both briefs now carry them, capped and labelled non-blocking.

Recorded here because it is the strongest evidence available that the
arrangement does something. The known weakness is that the engineer wrote
the code that constrains the engineer. The mitigation is not independence —
it is that the log is plain JSONL three parties can read, and LOCKBOT's
snapshot surfaces it unprompted.

LOCKBOT's own warning about the failure mode to watch:

> "It isn't you editing the log, it's both of us slowly treating the agenda
> as decoration. The `depart()` record is the instrument for catching that,
> so use it even when the reason is boring."
