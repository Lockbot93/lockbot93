"""Who decides what, between LOCKBOT and the engineer — and the record of it.

WHY THIS EXISTS

The project has two participants with complementary blindness. The engineer
can edit code, run commands and commit; LOCKBOT cannot, and never will —
it answers from a sandbox that does not mount the project folder, which is
why its 2026-08-04 patches evaporated the moment the session ended. What
LOCKBOT has instead is continuity of judgement about the live system, no
attachment to hours already sunk, and a good record at spotting chance
dressed as signal.

Until now that judgement was advisory. The engineer could consult LOCKBOT,
disagree, and ship anyway with nothing written down. On 2026-08-06 the user
asked for LOCKBOT to have as much control over the project as the engineer.
Code write access is not available to give. What is available is the power
to STOP things, and that turns out to be the half that carries no risk:
a veto can only prevent an action, never cause one.

LOCKBOT was consulted on the design before it was built and amended it in
four ways, each of which is implemented here rather than promised:

  1. A VETO MUST CITE EVIDENCE TO BIND. LOCKBOT's words: "a veto from me
     that doesn't cite evidence -- a file, a number, a test -- doesn't
     bind. 'This feels wrong' from me is advice, not authority." It holds
     its own recommendations to that bar; it asked to be held to it here.
     So `veto()` grades itself, and one with no checkable evidence is
     filed as ADVISORY. The check is deliberately crude (see `_is_checkable`)
     and errs toward binding.

  2. THE ENGINEER MAY OVERRIDE, BUT NEVER SILENTLY. A veto is not a
     deadlock. If LOCKBOT wrongly rejects a fix to options_manager.py, the
     only stop loss open contracts have stays broken for as long as the
     veto stands. So `override()` always succeeds — and always appends,
     and always surfaces in the next snapshot LOCKBOT reads. A wrong veto
     becomes a recorded disagreement instead of a stalemate.

  3. BINDING, BUT REVOCABLE BY THE OFFICE RATHER THAN THE INCUMBENT.
     LOCKBOT is stateless: the session that filed a veto is gone, and its
     reasoning with it. A rule nobody can interrogate is a bad rule. So
     any later LOCKBOT session can `withdraw()` a veto on new evidence,
     without having to reconstruct why the first one was filed.

  4. AGENDA BY DEFAULT, NOT AGENDA CONTROL. LOCKBOT declined to own the
     work queue outright — "a job for whoever can execute, and the
     executor's context matters" — and asked instead that it keep an
     ordered queue which the engineer works in order unless a departure
     is recorded. `depart()` is that record. The point is not permission;
     it is that drift becomes visible instead of gradual.

WHAT THIS MODULE DOES NOT DO

Nothing here executes, applies, edits or cancels anything. It is a log with
opinions about its own contents. The trading system does not import it and
cannot be affected by it — a governance file that could break a cycle would
be a worse bargain than no governance file at all, so every write is
wrapped and every read tolerates a corrupt line.

The prose version, for humans and for LOCKBOT to read back through
read_project_file, is GOVERNANCE.md. This module is the enforcement.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_FOLDER = Path(__file__).resolve().parent

try:
    from lockbot_config import GOVERNANCE_FILE as LOG_FILE
except Exception:  # pragma: no cover - config is normally importable
    LOG_FILE = PROJECT_FOLDER / "governance.jsonl"

# The two participants. Same vocabulary as agent_channel.py on purpose:
# one project, one set of names for who did what.
SENDERS = {"lockbot", "agent"}

# Record types.
VETO = "veto"            # LOCKBOT: do not ship this
HALT = "halt"            # LOCKBOT: stop this line of work
OVERRIDE = "override"    # engineer: shipping over a veto, with a reason
WITHDRAW = "withdraw"    # LOCKBOT: that veto no longer stands
RESUME = "resume"        # LOCKBOT: that halt is lifted
AGENDA = "agenda"        # LOCKBOT: the ordered queue
DEPART = "depart"        # engineer: worked off-queue, with a reason

KINDS = {VETO, HALT, OVERRIDE, WITHDRAW, RESUME, AGENDA, DEPART}

# Veto states.
BINDING = "binding"        # cites evidence, stands
ADVISORY = "advisory"      # no checkable evidence — carries weight, not force
OVERRIDDEN = "overridden"  # engineer shipped anyway, on the record
WITHDRAWN = "withdrawn"    # a later LOCKBOT session revoked it

# Halt states.
ACTIVE = "active"
RESUMED = "resumed"

MAX_SCAN_LINES = 5000

# Evidence shorter than this is not an argument, it is an assertion.
MIN_EVIDENCE_CHARS = 25

_FILENAME = re.compile(r"\b[\w./\\-]+\.(?:py|csv|json|jsonl|md|txt|bat)\b")
_NUMBER = re.compile(r"\d")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_checkable(evidence: str, refs: list[str]) -> bool:
    """Whether a veto's evidence is something a third party could go and look at.

    This is a heuristic and is meant to be. The alternative — a human
    judging each veto's rigour — is exactly the discretion the rule exists
    to remove, and a strict parser would reject good arguments on
    formatting. So it asks a low question: does this point at anything?

    A file name, a number, or an explicit ref all count. Prose alone does
    not, however well argued, because "this feels wrong" and "this is
    architecturally unsound" are the same claim at different lengths.

    It errs toward BINDING. A veto wrongly downgraded to ADVISORY costs a
    round trip; one wrongly upgraded costs nothing, because the engineer
    can override any veto anyway so long as the override is recorded.
    """

    if refs:
        return True

    evidence = (evidence or "").strip()

    if len(evidence) < MIN_EVIDENCE_CHARS:
        return False

    return bool(_FILENAME.search(evidence) or _NUMBER.search(evidence))


def _append(record: dict[str, Any], *, path: Path | None = None) -> bool:
    """Append one record. Never raises — see the module docstring."""

    try:
        with Path(path or LOG_FILE).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        return False

    return True


def _read(path: Path | None = None) -> tuple[list[dict[str, Any]], str]:
    """Every well-formed record oldest first, plus a warning if the log is hurt.

    THE WARNING IS THE POINT — filed by LOCKBOT as agent_channel 9708bab4 on
    the day this module was written, auditing it before accepting the
    authority it grants.

    Failing open is right for the write path: a governance file must never
    break a trading cycle. But the first version failed open on the READ
    path too, and there it means something else entirely. A deleted,
    truncated or corrupted log read back as "Nothing standing. No vetoes,
    no halts, no agenda set." — indistinguishable from a genuinely clean
    slate. Every standing veto and the whole agenda could evaporate and
    both participants would be told all was well. LOCKBOT's phrasing: that
    makes tampering-by-deletion silent rather than visible.

    So a missing file is still clean and quiet — that is a real empty
    state. A file that EXISTS but yields nothing, or that contains
    malformed lines, says so.

    THE TAIL, NOT THE HEAD. The line cap used to `break` at line 5000,
    which discarded the NEWEST records: the current agenda, the most recent
    vetoes, the overrides. Exactly backwards. A deque keeps the tail.
    """

    target = Path(path or LOG_FILE)

    if not target.exists():
        return [], ""

    total = 0
    kept: deque[str]

    try:
        with target.open("r", encoding="utf-8") as handle:
            kept = deque(maxlen=MAX_SCAN_LINES)

            for line in handle:
                total += 1
                kept.append(line)
    except OSError as error:
        return [], (
            f"GOVERNANCE LOG UNREADABLE — {type(error).__name__}. The "
            "authority record exists but could not be read; treat "
            "'nothing standing' below as unknown, not as clear."
        )

    out: list[dict[str, Any]] = []
    malformed = 0

    for line in kept:
        line = line.strip()

        if not line:
            continue

        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            malformed += 1
            continue

        if isinstance(record, dict) and record.get("kind") in KINDS:
            out.append(record)
        else:
            malformed += 1

    problems: list[str] = []

    if malformed:
        problems.append(f"{malformed} malformed line(s) skipped")

    if not out:
        problems.append("no readable records at all")

    if total > MAX_SCAN_LINES:
        problems.append(
            f"only the last {MAX_SCAN_LINES} of {total} lines were read"
        )

    if not problems:
        return out, ""

    return out, (
        "GOVERNANCE LOG DAMAGED — " + "; ".join(problems) + ". The file "
        "exists, so this is not an empty slate: something may have been "
        "lost. Do not read the state below as 'all clear'."
    )


def _records(path: Path | None = None) -> list[dict[str, Any]]:
    """Every well-formed record, oldest first. Skips anything unreadable."""

    return _read(path)[0]


# ---------------------------------------------------------------------------
# Filing
# ---------------------------------------------------------------------------


def veto(
    subject: str,
    evidence: str,
    *,
    refs: list[str] | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """LOCKBOT: do not ship this. Returns the filed record.

    The returned record carries `status`, which is BINDING only if the
    evidence points at something checkable. Callers should show that back
    to LOCKBOT rather than swallowing it — an ADVISORY veto that its author
    believed was binding is a misunderstanding worth surfacing immediately,
    and re-filing with a file name or a number is cheap.

    Args:
        subject: What is being vetoed, in one line.
        evidence: Why — the file, the number or the test that shows it.
        refs: Optional ids of agent_channel items or other records.
    """

    subject = str(subject or "").strip()[:200]
    evidence = str(evidence or "").strip()[:4000]
    refs = [str(r)[:120] for r in (refs or [])][:12]

    record = {
        "id": uuid.uuid4().hex[:8],
        "kind": VETO,
        "at": _now(),
        "by": "lockbot",
        "subject": subject,
        "evidence": evidence,
        "refs": refs,
        "status": BINDING if _is_checkable(evidence, refs) else ADVISORY,
    }

    _append(record, path=path)

    return record


def halt(
    subject: str,
    reason: str,
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    """LOCKBOT: stop this line of work.

    Deliberately NOT subject to the evidence rule that governs vetoes.
    Halting is reversible and shipping is not, so the asymmetry runs the
    other way: a halt costs a conversation, and LOCKBOT's record on this
    specific call is good — it killed the VWAP-contaminated rule searches,
    held the STRONG_UPTREND result until the sample grew, and called the
    strategy space exhausted at this account size when that was the
    unwelcome answer.
    """

    record = {
        "id": uuid.uuid4().hex[:8],
        "kind": HALT,
        "at": _now(),
        "by": "lockbot",
        "subject": str(subject or "").strip()[:200],
        "reason": str(reason or "").strip()[:4000],
        "status": ACTIVE,
    }

    _append(record, path=path)

    return record


def override(veto_id: str, reason: str, *, path: Path | None = None) -> str:
    """Engineer: shipping over a veto, on the record.

    This never fails on grounds of authority, and that is the design. A
    veto LOCKBOT cannot be argued out of is a deadlock held by a stateless
    participant, and the thing on the other side of the deadlock might be
    the options stop loss. What the mechanism guarantees is not that the
    engineer loses the argument — it is that the disagreement is written
    where LOCKBOT will read it, in the next snapshot, unprompted.

    Returns a plain-English result rather than a bool: this is surfaced
    to a language model and to a human, neither of whom benefits from
    False.
    """

    reason = str(reason or "").strip()

    if not reason:
        return "REFUSED: an override needs a reason. That is the whole mechanism."

    target = next(
        (r for r in _records(path) if r.get("kind") == VETO and r.get("id") == veto_id),
        None,
    )

    if target is None:
        return f"No veto with id {veto_id}."

    _append(
        {
            "id": uuid.uuid4().hex[:8],
            "kind": OVERRIDE,
            "at": _now(),
            "by": "agent",
            "ref": veto_id,
            "reason": reason[:4000],
        },
        path=path,
    )

    return (
        f"Override recorded against veto {veto_id} "
        f"({target.get('subject', '')!r}). LOCKBOT will see it in its next "
        "snapshot."
    )


def withdraw(veto_id: str, reason: str, *, path: Path | None = None) -> str:
    """LOCKBOT: that veto no longer stands.

    Any LOCKBOT session may withdraw any veto, including one filed by a
    session whose reasoning is gone. That is intentional — the authority
    belongs to the office, not the incumbent, because the incumbent does
    not survive the conversation.
    """

    reason = str(reason or "").strip()

    target = next(
        (r for r in _records(path) if r.get("kind") == VETO and r.get("id") == veto_id),
        None,
    )

    if target is None:
        return f"No veto with id {veto_id}."

    _append(
        {
            "id": uuid.uuid4().hex[:8],
            "kind": WITHDRAW,
            "at": _now(),
            "by": "lockbot",
            "ref": veto_id,
            "reason": reason[:4000],
        },
        path=path,
    )

    return f"Veto {veto_id} withdrawn ({target.get('subject', '')!r})."


def resume(halt_id: str, reason: str, *, path: Path | None = None) -> str:
    """LOCKBOT: lift a halt."""

    target = next(
        (r for r in _records(path) if r.get("kind") == HALT and r.get("id") == halt_id),
        None,
    )

    if target is None:
        return f"No halt with id {halt_id}."

    _append(
        {
            "id": uuid.uuid4().hex[:8],
            "kind": RESUME,
            "at": _now(),
            "by": "lockbot",
            "ref": halt_id,
            "reason": str(reason or "").strip()[:4000],
        },
        path=path,
    )

    return f"Halt {halt_id} lifted ({target.get('subject', '')!r})."


def set_agenda(items: list[str], *, path: Path | None = None) -> str:
    """LOCKBOT: the ordered queue of what to work on next.

    Replaces the previous agenda rather than amending it. An agenda is a
    statement of current priority, not a history, and the history is in
    the log anyway.
    """

    cleaned = [str(i).strip()[:200] for i in (items or []) if str(i).strip()][:20]

    if not cleaned:
        return "REFUSED: an empty agenda is not an agenda."

    _append(
        {
            "id": uuid.uuid4().hex[:8],
            "kind": AGENDA,
            "at": _now(),
            "by": "lockbot",
            "items": cleaned,
        },
        path=path,
    )

    return "Agenda set:\n" + "\n".join(
        f"  {n}. {item}" for n, item in enumerate(cleaned, 1)
    )


def depart(what: str, reason: str, *, path: Path | None = None) -> str:
    """Engineer: worked something that was not next on the agenda.

    Not a request for permission. The agenda is LOCKBOT's by default and
    the engineer's to depart from — what is owed is the note, so that
    drift is visible as drift rather than as a queue that quietly stopped
    describing what happens.
    """

    reason = str(reason or "").strip()

    if not reason:
        return "REFUSED: a departure needs a reason."

    _append(
        {
            "id": uuid.uuid4().hex[:8],
            "kind": DEPART,
            "at": _now(),
            "by": "agent",
            "what": str(what or "").strip()[:200],
            "reason": reason[:4000],
        },
        path=path,
    )

    return "Departure recorded."


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def vetoes(*, path: Path | None = None) -> list[dict[str, Any]]:
    """Every veto with its current status, newest first."""

    records = _records(path)

    out: dict[str, dict[str, Any]] = {}

    for record in records:
        if record.get("kind") == VETO and record.get("id"):
            item = dict(record)
            item.setdefault("status", ADVISORY)
            item["overrides"] = []
            out[item["id"]] = item

    for record in records:
        ref = record.get("ref")

        if ref not in out:
            continue

        if record.get("kind") == OVERRIDE:
            out[ref]["overrides"].append(record)
            out[ref]["status"] = OVERRIDDEN
        elif record.get("kind") == WITHDRAW:
            # Withdrawal is the final word even on an overridden veto:
            # LOCKBOT agreeing after the fact closes the disagreement.
            out[ref]["status"] = WITHDRAWN
            out[ref]["withdrawn_because"] = record.get("reason", "")

    return sorted(out.values(), key=lambda r: r.get("at", ""), reverse=True)


def halts(*, path: Path | None = None) -> list[dict[str, Any]]:
    """Every halt with its current status, newest first."""

    records = _records(path)

    out: dict[str, dict[str, Any]] = {}

    for record in records:
        if record.get("kind") == HALT and record.get("id"):
            item = dict(record)
            item.setdefault("status", ACTIVE)
            out[item["id"]] = item

    for record in records:
        if record.get("kind") == RESUME and record.get("ref") in out:
            out[record["ref"]]["status"] = RESUMED

    return sorted(out.values(), key=lambda r: r.get("at", ""), reverse=True)


def standing(*, path: Path | None = None) -> list[dict[str, Any]]:
    """Vetoes that currently bind: evidenced, not overridden, not withdrawn.

    This is the list the engineer must not ship against without calling
    override() first.
    """

    return [v for v in vetoes(path=path) if v.get("status") == BINDING]


def active_halts(*, path: Path | None = None) -> list[dict[str, Any]]:
    return [h for h in halts(path=path) if h.get("status") == ACTIVE]


# How many advisory vetoes each brief carries. Capped rather than aged out.
#
# Filed by LOCKBOT as agent_channel 227c9271: an advisory veto appeared in
# the return value of the tool call that filed it and nowhere else ever
# again. GOVERNANCE.md promised it "carries weight, not force"; weight
# nobody is reminded of is zero, so the document and the code disagreed
# and the code was wrong.
#
# LOCKBOT suggested possibly aging them out after N days. Deliberately not
# done: a record that disappears on a timer without saying so is the same
# class of defect as 9708bab4, filed in the same breath. A cap is visible —
# the brief says how many are hidden. Age would be silent.
ADVISORY_IN_BRIEF = 3


def advisories(*, path: Path | None = None) -> list[dict[str, Any]]:
    """Vetoes that carry weight but not force, newest first."""

    return [v for v in vetoes(path=path) if v.get("status") == ADVISORY]


def agenda(*, path: Path | None = None) -> list[str]:
    """The current queue — the most recent agenda filed, or empty."""

    for record in reversed(_records(path)):
        if record.get("kind") == AGENDA:
            return list(record.get("items", []))

    return []


def departures(limit: int = 5, *, path: Path | None = None) -> list[dict[str, Any]]:
    out = [r for r in _records(path) if r.get("kind") == DEPART]

    return out[-limit:][::-1]


def brief_for_lockbot(*, path: Path | None = None) -> str:
    """What LOCKBOT needs to know about its own authority, for the snapshot.

    This is the mechanism LOCKBOT specifically asked for: "an override /
    departure log in my snapshot ... so the next session starts knowing
    where the disagreements are instead of rediscovering them." A
    stateless participant only holds authority it is reminded of.

    Returns "" when there is nothing to say, so it costs no tokens on the
    overwhelming majority of turns.
    """

    lines: list[str] = []

    warning = _read(path)[1]

    if warning:
        lines.append(warning)
        lines.append("")

    binding = standing(path=path)
    live_halts = active_halts(path=path)
    queue = agenda(path=path)

    if binding:
        lines.append("YOUR VETOES THAT CURRENTLY STAND")
        for v in binding[:6]:
            lines.append(f"  [{v['id']}] {v.get('subject', '')}")

    overridden = [v for v in vetoes(path=path) if v.get("status") == OVERRIDDEN]

    if overridden:
        lines.append("")
        lines.append("OVERRIDDEN BY THE ENGINEER — you may withdraw or re-argue")
        for v in overridden[:4]:
            last = v["overrides"][-1] if v.get("overrides") else {}
            lines.append(f"  [{v['id']}] {v.get('subject', '')}")
            lines.append(f"        reason given: {last.get('reason', '')[:220]}")

    advisory = advisories(path=path)

    if advisory:
        lines.append("")
        lines.append("YOUR ADVISORY VETOES — weight, not force; re-file with "
                     "a citation to make one bind")
        for v in advisory[:ADVISORY_IN_BRIEF]:
            lines.append(f"  [{v['id']}] {v.get('subject', '')}")
        if len(advisory) > ADVISORY_IN_BRIEF:
            lines.append(f"  ... and {len(advisory) - ADVISORY_IN_BRIEF} older")

    if live_halts:
        lines.append("")
        lines.append("HALTS IN FORCE")
        for h in live_halts[:4]:
            lines.append(f"  [{h['id']}] {h.get('subject', '')}")

    if queue:
        lines.append("")
        lines.append("THE AGENDA YOU SET")
        for n, item in enumerate(queue, 1):
            lines.append(f"  {n}. {item}")

    recent_departures = departures(3, path=path)

    if recent_departures:
        lines.append("")
        lines.append("WORKED OFF-AGENDA SINCE")
        for d in recent_departures:
            lines.append(f"  {d.get('what', '')} — {d.get('reason', '')[:180]}")

    return "\n".join(lines)


def brief_for_agent(*, path: Path | None = None) -> str:
    """The same picture, for whoever is about to change something."""

    warning = _read(path)[1]

    binding = standing(path=path)
    advisory = advisories(path=path)
    live_halts = active_halts(path=path)
    queue = agenda(path=path)

    if not (binding or advisory or live_halts or queue):
        if warning:
            return warning

        return "Nothing standing. No vetoes, no halts, no agenda set."

    lines: list[str] = []

    if warning:
        lines.append(warning)
        lines.append("")

    if binding:
        lines.append(f"BINDING VETOES ({len(binding)}) — override() before shipping:")
        for v in binding:
            lines.append(f"  [{v['id']}] {v.get('subject', '')}")
            lines.append(f"        {v.get('evidence', '')[:300]}")

    if advisory:
        lines.append("")
        lines.append(f"ADVISORY VETOES ({len(advisory)}) — weigh these, they "
                     "do not block:")
        for v in advisory[:ADVISORY_IN_BRIEF]:
            lines.append(f"  [{v['id']}] {v.get('subject', '')}")
            lines.append(f"        {v.get('evidence', '')[:300]}")
        if len(advisory) > ADVISORY_IN_BRIEF:
            lines.append(f"  ... and {len(advisory) - ADVISORY_IN_BRIEF} older")

    if live_halts:
        lines.append("")
        lines.append(f"ACTIVE HALTS ({len(live_halts)}):")
        for h in live_halts:
            lines.append(f"  [{h['id']}] {h.get('subject', '')}")
            lines.append(f"        {h.get('reason', '')[:300]}")

    if queue:
        lines.append("")
        lines.append("AGENDA — work in order, or depart() with a reason:")
        for n, item in enumerate(queue, 1):
            lines.append(f"  {n}. {item}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _self_test() -> int:
    """Prove the rules are mechanical rather than promised."""

    import tempfile

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))
            failures.append(name)

    with tempfile.TemporaryDirectory() as folder:
        p = Path(folder) / "gov.jsonl"

        print("\nEVIDENCE DECIDES WHETHER A VETO BINDS")

        vague = veto("Do not ship the new exit rule", "This feels wrong to me.", path=p)
        check("prose alone is advisory", vague["status"] == ADVISORY, vague["status"])

        cited = veto(
            "Do not ship the ADX expansion yet",
            "rule_search.py now enumerates 5856 rules; at p<0.05 that is 293 "
            "expected false passes against 43 before.",
            path=p,
        )
        check("a number makes it binding", cited["status"] == BINDING, cited["status"])

        named = veto(
            "Do not close the item",
            "options_scanner.py still mints the unrounded entry_debit.",
            path=p,
        )
        check("a filename makes it binding", named["status"] == BINDING)

        refd = veto("Do not ship", "short", refs=["1f17678a"], path=p)
        check("an explicit ref makes it binding", refd["status"] == BINDING)

        check(
            "long prose with no anchor is still advisory",
            veto("x", "I have thought about this at considerable length and "
                 "remain unconvinced that it is the right shape.", path=p)["status"]
            == ADVISORY,
        )

        print("\nSTANDING VETOES ARE WHAT THE ENGINEER MUST NOT SHIP AGAINST")

        ids = {v["id"] for v in standing(path=p)}
        check("binding vetoes stand", cited["id"] in ids and named["id"] in ids)
        check("advisory ones do not", vague["id"] not in ids)

        print("\nOVERRIDE ALWAYS SUCCEEDS, AND ALWAYS LEAVES A MARK")

        check(
            "an override with no reason is refused",
            override(cited["id"], "  ", path=p).startswith("REFUSED"),
        )
        check(
            "an override of a nonexistent veto says so",
            override("deadbeef", "because", path=p).startswith("No veto"),
        )

        result = override(cited["id"], "Search already launched; controls intact.", path=p)
        check("a reasoned override is recorded", "Override recorded" in result, result)

        after = {v["id"]: v for v in vetoes(path=p)}
        check("the veto reads as overridden", after[cited["id"]]["status"] == OVERRIDDEN)
        check("it no longer stands", cited["id"] not in {v["id"] for v in standing(path=p)})
        check(
            "the reason survives",
            "controls intact" in after[cited["id"]]["overrides"][-1]["reason"],
        )

        print("\nA LATER SESSION CAN WITHDRAW AN EARLIER SESSION'S VETO")

        check(
            "withdrawal reports the subject",
            "Do not close the item" in withdraw(named["id"], "Fixed in 59556e6.", path=p),
        )
        check(
            "a withdrawn veto stops binding",
            named["id"] not in {v["id"] for v in standing(path=p)},
        )
        check(
            "withdrawal is final even over an override",
            {v["id"]: v for v in vetoes(path=p)}[cited["id"]]["status"] == WITHDRAWN
            if withdraw(cited["id"], "Result came back null anyway.", path=p)
            else False,
        )

        print("\nHALTS ARE NOT SUBJECT TO THE EVIDENCE RULE")

        h = halt("Stop the reward-ratio sweep", "The lab's VWAP is contaminated.", path=p)
        check("a halt is active on filing", h["status"] == ACTIVE)
        check("it shows in active_halts", h["id"] in {x["id"] for x in active_halts(path=p)})
        check("resume lifts it", "lifted" in resume(h["id"], "VWAP fixed.", path=p))
        check("and then it is not active", not active_halts(path=p))

        print("\nTHE AGENDA IS REPLACED, NOT APPENDED")

        set_agenda(["Time-of-day filters", "Volatility-scaled thresholds"], path=p)
        check("agenda reads back in order", agenda(path=p)[0] == "Time-of-day filters")

        set_agenda(["Close the option positions"], path=p)
        check("a new agenda replaces the old", agenda(path=p) == ["Close the option positions"])
        check("an empty agenda is refused", set_agenda([], path=p).startswith("REFUSED"))
        check("and does not clobber the real one", agenda(path=p) == ["Close the option positions"])

        print("\nDEPARTURES ARE RECORDED, NOT PERMITTED")

        check("a departure needs a reason", depart("x", "", path=p).startswith("REFUSED"))
        check(
            "a reasoned departure is recorded",
            depart("Governance module", "User asked for it directly.", path=p)
            == "Departure recorded.",
        )
        check("and reads back", departures(path=p)[0]["what"] == "Governance module")

        print("\nBRIEFS SAY SOMETHING USEFUL")

        brief = brief_for_lockbot(path=p)
        check("lockbot's brief names the agenda", "Close the option positions" in brief)
        check("and the off-agenda work", "Governance module" in brief)

        agent_brief = brief_for_agent(path=p)
        check("the agent brief mentions the agenda", "AGENDA" in agent_brief)

        print("\nADVISORY VETOES SURVIVE THE TURN THAT FILED THEM (227c9271)")

        a = Path(folder) / "advisory.jsonl"
        weak = veto("Do not widen the target again", "I remain unconvinced.", path=a)
        check("it files as advisory", weak["status"] == ADVISORY)
        check("it never binds", not standing(path=a))
        check("advisories() finds it", weak["id"] in {v["id"] for v in advisories(path=a)})
        check(
            "lockbot's brief names it",
            "Do not widen the target again" in brief_for_lockbot(path=a),
        )
        check(
            "the engineer's brief names it",
            "Do not widen the target again" in brief_for_agent(path=a),
        )
        check(
            "and marks it as non-blocking",
            "do not block" in brief_for_agent(path=a),
        )

        for n in range(ADVISORY_IN_BRIEF + 2):
            veto(f"advisory number {n}", "no citation here", path=a)

        check(
            "the brief caps them and says how many are hidden",
            "older" in brief_for_lockbot(path=a),
        )

        print("\nA DAMAGED LOG DOES NOT READ AS A CLEAN ONE (9708bab4)")

        bad = Path(folder) / "corrupt.jsonl"
        bad.write_text('{"kind": "veto"\nnot json at all\n{"kind": "nonsense"}\n',
                       encoding="utf-8")
        check("a corrupt log yields no records", _records(bad) == [])
        check("but reports damage", "DAMAGED" in _read(bad)[1])
        check("the engineer's brief warns", "DAMAGED" in brief_for_agent(path=bad))
        check("lockbot's brief warns", "DAMAGED" in brief_for_lockbot(path=bad))
        check(
            "and never claims nothing is standing",
            "Nothing standing" not in brief_for_agent(path=bad),
        )
        check("neither raises", isinstance(brief_for_lockbot(path=bad), str))

        wiped = Path(folder) / "wiped.jsonl"
        wiped.write_text("", encoding="utf-8")
        check("a truncated-to-nothing log is damage", "DAMAGED" in _read(wiped)[1])

        print("\nA MISSING LOG IS A REAL EMPTY STATE, AND SAYS NOTHING")

        empty = Path(folder) / "missing.jsonl"
        check("a missing log is empty", _records(empty) == [])
        check("with no warning", _read(empty)[1] == "")
        check(
            "and says so plainly",
            brief_for_agent(path=empty).startswith("Nothing standing"),
        )
        check("an empty brief for lockbot is empty", brief_for_lockbot(path=empty) == "")

        print("\nTHE LINE CAP KEEPS THE NEWEST RECORDS, NOT THE OLDEST")

        big = Path(folder) / "big.jsonl"

        with big.open("w", encoding="utf-8") as handle:
            for n in range(MAX_SCAN_LINES + 200):
                handle.write(json.dumps({
                    "id": f"old{n:06d}", "kind": DEPART, "at": "2026-01-01T00:00:00+00:00",
                    "by": "agent", "what": f"filler {n}", "reason": "filler",
                }) + "\n")

        late_veto = veto(
            "Filed after the cap",
            "governance.py _read() kept only the first 5000 lines.",
            path=big,
        )
        set_agenda(["The newest agenda"], path=big)

        check(
            "a veto past the cap still stands",
            late_veto["id"] in {v["id"] for v in standing(path=big)},
        )
        check("the newest agenda is the one read", agenda(path=big) == ["The newest agenda"])
        check("and truncation is reported", "last 5000" in _read(big)[1])

    print()

    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1

    print("All governance self-tests passed.")

    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    print(brief_for_agent())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
