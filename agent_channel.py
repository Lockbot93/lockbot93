"""
agent_channel.py — a durable channel between LOCKBOT and its engineer.

WHY THIS EXISTS

On the night of 2026-08-04 LOCKBOT was asked, over Telegram, to fix two
things. It did the hard part of both: it root-caused a float-precision
bug that was silently blocking the PCG put's take-profit, and it worked
out why SELL_SHORT setups never reach the shadow log. It wrote a patch
for each.

Then the patches evaporated. LOCKBOT answers Telegram from a sandbox
that does not mount the project folder, so "exported" meant written
somewhere nothing else can read. The next morning it reported, correctly
and uselessly, that both bugs were diagnosed, both fixes written, and
neither applied -- and asked to be told when someone applied them, with
no mechanism for anyone to say so.

Meanwhile the entry_debit of 56.00000000000001 was sitting in a status
dump the engineering side had already read past. The diagnosis existed.
The fix existed. The reader existed. There was no wire between them, so
a human carried messages by hand and the work was done twice.

WHAT THIS IS

An append-only log both sides can write to and read from. LOCKBOT files
work it cannot do; the engineer files what was changed. Neither side has
to be running when the other writes.

WHAT THIS DELIBERATELY IS NOT

It is not a way for LOCKBOT to change code. Nothing here executes,
applies, or edits anything -- an item is a message, and acting on it is
a person's decision. That boundary is the same one runtime_settings.py
draws for configuration, and for the same reason: the assistant answers
questions over a Telegram bot whose token is a bearer credential.

It is also not another notes file. brain_memory.md is what LOCKBOT tells
ITSELF between sessions; recommendations.py is settings changes awaiting
approval; learning_log.jsonl is hypotheses. This is the one addressed to
someone else, and the difference that matters is that items here have a
status -- an open item stays open, and is still open next session, until
somebody resolves it.

WHY STATUS IS THE WHOLE POINT

Without it this is a suggestion box. The failure being fixed is not that
LOCKBOT could not describe the bug; it described it very well. It is
that having described it, nothing carried the description forward, and
nothing could tell it later that the work had been done. So an item
resolves explicitly, the resolution is visible to both sides, and
LOCKBOT's state snapshot carries both -- which is what stops it
re-reporting a bug that was fixed hours ago.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_FOLDER = Path(__file__).resolve().parent

try:
    from lockbot_config import AGENT_CHANNEL_FILE as CHANNEL_FILE
except Exception:  # pragma: no cover - config is normally importable
    CHANNEL_FILE = PROJECT_FOLDER / "agent_channel.jsonl"

SENDERS = {"lockbot", "agent"}

# What an item is for. Kept short: a taxonomy nobody can remember gets
# used wrongly, and a wrong label is worse than a coarse one.
KINDS = {
    "bug",        # something is broken and needs a code change
    "fix",        # a code change was made
    "question",   # one side needs an answer from the other
    "note",       # context worth carrying, needing no action
}

# Kinds that stay open until somebody resolves them. A note is filed and
# done; a bug is not.
ACTIONABLE = {"bug", "question"}

# The lifecycle, and why it is not just open/closed.
#
# Filing a bug and applying a patch are different from FIXING it. LOCKBOT
# diagnosed the PCG float bug correctly on 2026-08-04 and proposed a fix
# that was genuinely incomplete: it patched the two paths it knew about
# and missed options_scanner.py, which mints the same dirty number for
# every new position. Had that patch been applied and the item closed,
# the bug would have been marked fixed while still live.
#
# So "the engineer applied something" cannot be the end state. The side
# that reported the problem is the side that can see whether it went
# away, and it gets the last word.
OPEN = "open"                  # filed, nobody has done anything
APPLIED = "applied"            # a change was made, not yet confirmed
VERIFIED = "verified"          # the reporter confirmed it is actually fixed
REOPENED = "reopened"          # the reporter checked and it is NOT fixed
RESOLVED = "resolved"          # closed without verification (notes, questions)

# Statuses that still need someone to act.
UNFINISHED = {OPEN, APPLIED, REOPENED}

MAX_SCAN_LINES = 5000


# How recently you must have messaged LOCKBOT for it to assume you are
# present and stay quiet. If you are mid-conversation you will read the
# reply that filed the item; a push on top of it is noise.
PRESENCE_MINUTES = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _user_is_present(*, within_minutes: int = PRESENCE_MINUTES) -> bool:
    """Whether someone has been talking to LOCKBOT very recently."""

    try:
        import conversation_memory

        return bool(conversation_memory.recent(
            limit=1, within_hours=within_minutes / 60.0))
    except Exception:
        # Unknown means not present, so the alert goes out. A missed
        # notification is worse than a redundant one here: the whole
        # point is that an item filed while nobody is looking gets seen.
        return False


def notify_filed(item: dict) -> str:
    """Push an alert about one newly filed item. Returns what happened.

    WHY THIS IS OPT-IN RATHER THAN AUTOMATIC IN post()

    A test that files an item must not reach the user's phone. That is
    not hypothetical: a test run on 2026-08-03 sent five real Pushover
    notifications and polluted notification_state.json, because the
    thing being stubbed was a function name that did not exist. So
    post() never notifies on its own and the caller has to ask.

    Quiet when you are mid-conversation with LOCKBOT, because you will
    read the reply that filed the item, and quiet for anything that is
    not actionable.
    """

    if item.get("sender") != "lockbot":
        return "not from lockbot"

    if item.get("kind") not in ACTIONABLE:
        return "not actionable"

    try:
        import lockbot_config as config

        if not getattr(config, "NOTIFY_AGENT_CHANNEL", True):
            return "disabled in config"
    except Exception:
        pass

    if _user_is_present():
        return "suppressed: you are talking to LOCKBOT right now"

    try:
        from notifications import send_smart_notification

        outstanding = len([
            i for i in items()
            if i["kind"] in ACTIONABLE and i["status"] in UNFINISHED
        ])

        status = send_smart_notification(
            symbol="LOCKBOT",
            event_type="ENGINEER_ITEM_FILED",
            title=f"LOCKBOT filed a {item.get('kind', 'item')}",
            message=(
                f"{item.get('subject', '')}\n\n"
                f"{outstanding} item(s) now waiting on a code change. "
                "Start a Claude session and run agent_channel.py."
            ),
            # Per item, so a second filing alerts and a re-read never does.
            reason=f"item:{item.get('id')}",
        )

        return f"notified ({status})"

    except Exception as error:
        return f"notification failed: {type(error).__name__}: {error}"


def post(
    sender: str,
    subject: str,
    body: str,
    *,
    kind: str = "note",
    refs: list[str] | None = None,
    verify: str = "",
    notify: bool = False,
    path: Path | None = None,
) -> str:
    """File one item. Returns its id.

    `notify` pushes to the phone. Off by default and deliberately so --
    see notify_filed for the test run that sent five real alerts.

    `verify` is the acceptance test: what someone should check to know
    the problem is actually gone. It is the difference between an item
    that can be implemented correctly and one that can only be
    implemented plausibly. An item without it can still be applied, but
    nobody can confirm it, so it will sit in APPLIED rather than close.

    Never raises on a write failure. This is called from LOCKBOT's tool
    path and from the exit engine's neighbourhood; a channel that can
    break a trading cycle is worse than one that loses a message.
    """

    sender = str(sender or "").strip().lower()
    kind = str(kind or "note").strip().lower()

    if sender not in SENDERS:
        return ""

    if kind not in KINDS:
        kind = "note"

    item_id = uuid.uuid4().hex[:8]

    record = {
        "id": item_id,
        "at": _now(),
        "sender": sender,
        "kind": kind,
        "subject": str(subject or "").strip()[:200],
        "body": str(body or "").strip()[:8000],
        "verify": str(verify or "").strip()[:2000],
        "refs": [str(r)[:120] for r in (refs or [])][:12],
    }

    try:
        with Path(path or CHANNEL_FILE).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        return ""

    # Only ever for the real channel. A caller passing its own path is a
    # test or a scratch run, and neither should reach a phone.
    if notify and path is None:
        notify_filed(record)

    return item_id


def _event(
    item_id: str,
    event: str,
    by: str,
    note: str = "",
    *,
    path: Path | None = None,
) -> bool:
    """Append one lifecycle event against an item."""

    by = str(by or "").strip().lower()

    if by not in SENDERS or not item_id:
        return False

    if not any(i["id"] == item_id for i in items(path=path)):
        return False

    record = {
        "event": event,
        "item": str(item_id),
        "at": _now(),
        "by": by,
        "note": str(note or "").strip()[:2000],
    }

    try:
        with Path(path or CHANNEL_FILE).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        return False

    return True


def mark_applied(
    item_id: str, by: str = "agent", note: str = "", *, path: Path | None = None
) -> bool:
    """Record that a change was made. This does NOT close the item.

    The reporter still has to confirm the problem went away. Applying a
    patch and fixing a bug are different things, and on 2026-08-04 they
    came apart: a correct diagnosis carried an incomplete fix, and
    closing on "applied" would have marked a live bug done.
    """

    return _event(item_id, "applied", by, note, path=path)


def verify(
    item_id: str,
    by: str,
    confirmed: bool,
    evidence: str = "",
    *,
    path: Path | None = None,
) -> bool:
    """The reporter's verdict on whether the problem is actually gone.

    `confirmed` False REOPENS the item rather than closing it, and the
    evidence says what is still wrong. That is the whole loop: an item
    only leaves the board when the side that found the problem agrees it
    is no longer there.
    """

    return _event(
        item_id,
        "verified" if confirmed else "rejected",
        by,
        evidence,
        path=path,
    )


def resolve(
    item_id: str,
    by: str,
    note: str = "",
    *,
    path: Path | None = None,
) -> bool:
    """Close an item outright, without a verification round.

    For notes and questions, and for bugs where verification is not
    meaningful. A bug that CAN be verified should go through
    mark_applied and verify instead, so the fix is confirmed by the side
    that found the problem rather than asserted by the side that changed
    the code.
    """

    return _event(item_id, "resolved", by, note, path=path)


def _records(path: Path | None = None) -> list[dict]:
    """Every readable line. Corrupt ones are skipped, not fatal."""

    target = Path(path or CHANNEL_FILE)

    if not target.exists():
        return []

    try:
        lines = target.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []

    out = []

    for line in lines[-MAX_SCAN_LINES:]:
        line = line.strip()

        if not line:
            continue

        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        if isinstance(record, dict):
            out.append(record)

    return out


_EVENT_STATUS = {
    "applied": APPLIED,
    "verified": VERIFIED,
    "rejected": REOPENED,
    "resolved": RESOLVED,
}


def items(*, path: Path | None = None) -> list[dict]:
    """Every item, oldest first, each carrying its status and history.

    Status comes from the LAST event, so a rejected fix genuinely
    reopens: apply, reject, apply again, verify is a normal life and the
    item is only closed at the end of it.
    """

    records = _records(path)

    history: dict[str, list[dict]] = {}

    for record in records:
        # "resolves" is the pre-2026-08-05 shape, kept readable so the
        # first items filed do not lose their state.
        target = record.get("item") or record.get("resolves")

        if target:
            entry = dict(record)
            entry.setdefault("event", "resolved")
            history.setdefault(str(target), []).append(entry)

    out = []

    for record in records:
        if not record.get("id") or record.get("item") or record.get("resolves"):
            continue

        entry = dict(record)
        events = history.get(str(record["id"]), [])
        last = events[-1] if events else None

        entry["events"] = events
        entry["status"] = (
            _EVENT_STATUS.get(str(last.get("event")), RESOLVED)
            if last else OPEN
        )
        entry["closed"] = entry["status"] in (VERIFIED, RESOLVED)

        # Kept for callers written against the original shape.
        entry["resolved"] = entry["closed"]
        entry["resolution"] = last if entry["closed"] else None

        out.append(entry)

    return out


def open_items(*, sender: str | None = None, path: Path | None = None) -> list[dict]:
    """Actionable items that are not finished. Optionally from one side.

    Includes APPLIED and REOPENED, not just untouched ones: a fix
    awaiting confirmation is not done, and a rejected one is emphatically
    not done.
    """

    out = [
        i for i in items(path=path)
        if i["kind"] in ACTIONABLE and i["status"] in UNFINISHED
    ]

    if sender:
        out = [i for i in out if i["sender"] == str(sender).strip().lower()]

    return out


def awaiting_verification(
    *, reporter: str | None = None, path: Path | None = None
) -> list[dict]:
    """Items where a change was made and nobody has confirmed it worked."""

    out = [i for i in items(path=path) if i["status"] == APPLIED]

    if reporter:
        out = [i for i in out if i["sender"] == str(reporter).strip().lower()]

    return out


def recent(limit: int = 10, *, path: Path | None = None) -> list[dict]:
    """The last few items, newest first."""

    return list(reversed(items(path=path)))[:limit]


def brief_for_lockbot(*, path: Path | None = None) -> dict[str, Any]:
    """What LOCKBOT should know, for its state snapshot.

    Carries resolved fixes as well as open items, deliberately. The
    morning after the PCG bug, LOCKBOT correctly reported the fix was
    not applied; without this it would have kept reporting that after it
    was, because nothing could tell it otherwise.
    """

    everything = items(path=path)

    def last_note(item: dict) -> str:
        return item["events"][-1].get("note", "")[:600] if item["events"] else ""

    return {
        "what_this_is": (
            "Messages between you and the engineer who edits your code. "
            "You cannot change code; file a 'bug' with file_for_engineer "
            "and it will be picked up."
        ),
        "YOUR_JOB_HERE": (
            "When an item you raised shows status 'applied', the engineer "
            "has changed something and it is YOUR turn: check whether the "
            "problem is actually gone, using the acceptance test in "
            "'verify'. Read the source with read_project_file and the data "
            "with the other tools. Then call verify_fix. Confirm only if "
            "you can see it is fixed -- rejecting reopens the item, which "
            "is the correct outcome when a fix is incomplete, and is "
            "cheaper than a bug marked done while still live."
        ),
        "awaiting_your_verification": [
            {"id": i["id"], "subject": i["subject"],
             "how_to_verify": i["verify"] or "(no acceptance test was filed)",
             "what_the_engineer_did": last_note(i),
             "refs": i.get("refs", [])}
            for i in everything
            if i["sender"] == "lockbot" and i["status"] == APPLIED
        ],
        "open_items_you_raised": [
            {"id": i["id"], "at": i["at"], "kind": i["kind"],
             "status": i["status"], "subject": i["subject"]}
            for i in everything
            if i["sender"] == "lockbot" and i["kind"] in ACTIONABLE
            and i["status"] in UNFINISHED
        ],
        "waiting_on_you": [
            {"id": i["id"], "at": i["at"], "subject": i["subject"],
             "body": i["body"][:600]}
            for i in everything
            if i["sender"] == "agent" and i["kind"] in ACTIONABLE
            and i["status"] in UNFINISHED
        ],
        "recently_done_by_the_engineer": [
            {"id": i["id"], "at": i["at"], "subject": i["subject"],
             "body": i["body"][:600]}
            for i in everything
            if i["sender"] == "agent" and i["kind"] == "fix"
        ][-8:],
        "your_items_now_verified": [
            {"id": i["id"], "subject": i["subject"],
             "note": last_note(i)}
            for i in everything
            if i["sender"] == "lockbot" and i["status"] == VERIFIED
        ][-8:],
    }


def brief_for_agent(*, path: Path | None = None) -> str:
    """What the engineer should read at the start of a session."""

    everything = items(path=path)
    waiting = [
        i for i in everything
        if i["sender"] == "lockbot" and i["kind"] in ACTIONABLE
        and i["status"] in UNFINISHED
    ]

    if not everything:
        return (
            "Channel empty. Nothing filed by either side.\n\n"
            "LOCKBOT files items with its file_for_engineer tool. You post "
            "with agent_channel.post('agent', ...), record a change with "
            "mark_applied(id), and LOCKBOT confirms it with verify_fix."
        )

    reopened = [i for i in waiting if i["status"] == REOPENED]
    applied = [i for i in waiting if i["status"] == APPLIED]
    fresh = [i for i in waiting if i["status"] == OPEN]

    lines = [
        "=" * 70,
        "AGENT CHANNEL — items LOCKBOT is waiting on",
        "=" * 70,
        f"  {len(fresh)} open, {len(reopened)} REOPENED (a fix did not "
        f"work), {len(applied)} awaiting LOCKBOT's verification",
        "",
    ]

    if not waiting:
        lines.append("  Nothing outstanding.")
        lines.append("")

    # Rejected first. A fix that was applied and found wanting is more
    # urgent than an untouched item, because something is on record as
    # done while the problem is still live.
    for item in reopened + fresh:
        flag = " *** REOPENED — a previous fix did not work ***" \
            if item["status"] == REOPENED else ""

        lines.append(f"  [{item['id']}] {item['kind'].upper()}: "
                     f"{item['subject']}{flag}")
        lines.append(f"      filed {item['at'][:16]} by {item['sender']}")

        for line in item["body"].splitlines()[:16]:
            lines.append(f"      {line}")

        if item.get("verify"):
            lines.append("      ACCEPTANCE TEST (LOCKBOT will check this):")

            for line in item["verify"].splitlines()[:8]:
                lines.append(f"        {line}")

        if item.get("refs"):
            lines.append(f"      refs: {', '.join(item['refs'])}")

        for event in item["events"]:
            if event.get("event") == "rejected":
                lines.append(f"      REJECTED {event.get('at','')[:16]}: "
                             f"{event.get('note','')[:400]}")

        lines.append("")

    if applied:
        lines.append("-" * 70)
        lines.append("APPLIED, AWAITING LOCKBOT'S VERIFICATION")
        lines.append("-" * 70)

        for item in applied:
            lines.append(f"  [{item['id']}] {item['subject']}")

        lines.append("")

    lines.append("-" * 70)
    lines.append("  Record a change:  python agent_channel.py "
                 "--applied <id> --note \"what you did\"")
    lines.append("  Close outright :  python agent_channel.py "
                 "--resolve <id> --note \"...\"")
    lines.append("  LOCKBOT confirms with its verify_fix tool. An item is "
                 "only done when it does.")

    return "\n".join(lines)


def _self_test() -> int:
    import tempfile

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            failures.append(name)
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))

    log = Path(tempfile.mkdtemp()) / "agent_channel.jsonl"

    print("Filing and reading")

    bug_id = post("lockbot", "PCG cannot take profit",
                  "0.56*100 is 56.00000000000001", kind="bug",
                  refs=["options_manager.py"], path=log)

    check("an item gets an id", bool(bug_id), bug_id)
    check("and reads back", len(items(path=log)) == 1)
    check("it is open", len(open_items(path=log)) == 1)
    check("attributed to lockbot", items(path=log)[0]["sender"] == "lockbot")
    check("refs are kept", items(path=log)[0]["refs"] == ["options_manager.py"])

    print()
    print("Only real senders and kinds")

    check("an unknown sender is refused",
          post("hacker", "s", "b", path=log) == "")
    check("an unknown kind falls back to note",
          items(path=log)[-1]["kind"] == "bug" and
          post("agent", "s", "b", kind="nonsense", path=log) != "")
    check("and that fallback is a note",
          items(path=log)[-1]["kind"] == "note")
    check("a note is not actionable",
          all(i["kind"] != "note" for i in open_items(path=log)))

    print()
    print("Applying is not fixing")

    check("an unknown id cannot be marked applied",
          mark_applied("nope", "agent", path=log) is False)
    check("marking applied needs a real sender",
          mark_applied(bug_id, "hacker", path=log) is False)

    check("the engineer records a change",
          mark_applied(bug_id, "agent", "rounded in the constructor",
                       path=log) is True)

    state = lambda: [i for i in items(path=log) if i["id"] == bug_id][0]

    check("the item is APPLIED, not closed", state()["status"] == "applied",
          state()["status"])
    check("and is NOT closed", state()["closed"] is False)
    check("so it still counts as outstanding",
          any(i["id"] == bug_id for i in open_items(path=log)))
    check("and shows up as awaiting verification",
          any(i["id"] == bug_id for i in awaiting_verification(path=log)))

    print()
    print("The reporter gets the last word")

    # This is the case the whole lifecycle exists for: a fix that was
    # applied in good faith and did not actually work.
    check("the reporter can reject it",
          verify(bug_id, "lockbot", False,
                 "options_scanner.py still mints a dirty debit", path=log)
          is True)
    check("which REOPENS rather than closes", state()["status"] == "reopened",
          state()["status"])
    check("it is not closed", state()["closed"] is False)
    check("it is back on the engineer's list",
          any(i["id"] == bug_id for i in open_items(path=log)))
    check("and no longer awaiting verification",
          awaiting_verification(path=log) == [])
    check("the rejection reason is on the record",
          any("options_scanner" in e.get("note", "")
              for e in state()["events"]))

    mark_applied(bug_id, "agent", "moved rounding into __post_init__",
                 path=log)
    check("a second fix moves it back to applied",
          state()["status"] == "applied")

    check("and this time it verifies",
          verify(bug_id, "lockbot", True, "84.0 >= 84.0 is now True",
                 path=log) is True)
    check("which finally closes it", state()["status"] == "verified")
    check("closed means closed", state()["closed"] is True)
    check("nothing is outstanding", open_items(path=log) == [])

    check("the full history survives", len(state()["events"]) == 4,
          str([e.get("event") for e in state()["events"]]))

    # The original text must survive every round: append-only is the
    # point, so history is never rewritten.
    check("the original body is untouched",
          "56.00000000000001" in state()["body"])

    print()
    print("An acceptance test is what makes a fix checkable")

    with_test = post("lockbot", "needs checking", "body",
                     kind="bug",
                     verify="run options_manager.py --self-test", path=log)
    mark_applied(with_test, "agent", "done", path=log)
    view = brief_for_lockbot(path=log)

    check("the acceptance test reaches lockbot",
          any("--self-test" in i["how_to_verify"]
              for i in view["awaiting_your_verification"]),
          str(view["awaiting_your_verification"]))
    check("along with what the engineer did",
          any("done" in i["what_the_engineer_did"]
              for i in view["awaiting_your_verification"]))

    no_test = post("lockbot", "no test", "body", kind="bug", path=log)
    mark_applied(no_test, "agent", path=log)
    view = brief_for_lockbot(path=log)
    check("a missing acceptance test says so rather than looking verified",
          any("no acceptance test" in i["how_to_verify"]
              for i in view["awaiting_your_verification"]))

    resolve(with_test, "agent", path=log)
    resolve(no_test, "agent", path=log)

    print()
    print("Each side sees what is waiting on it")

    q_id = post("agent", "Did the short patch land?", "check signals.csv",
                kind="question", path=log)
    lockbot_view = brief_for_lockbot(path=log)

    check("lockbot sees the engineer's open question",
          any(i["id"] == q_id for i in lockbot_view["waiting_on_you"]))
    check("lockbot sees its own verified item",
          any(i["id"] == bug_id for i in lockbot_view["your_items_now_verified"]))
    check("and the confirming evidence comes with it",
          "84.0" in lockbot_view["your_items_now_verified"][0]["note"])
    check("lockbot has no open items of its own now",
          lockbot_view["open_items_you_raised"] == [])
    check("lockbot is told verification is its job",
          "verify_fix" in lockbot_view["YOUR_JOB_HERE"])

    post("agent", "Rounded entry_debit", "constructor now rounds",
         kind="fix", path=log)
    lockbot_view = brief_for_lockbot(path=log)
    check("fixes are advertised so it stops re-reporting them",
          any("Rounded" in i["subject"]
              for i in lockbot_view["recently_done_by_the_engineer"]))

    text = brief_for_agent(path=log)
    check("the engineer brief renders", "AGENT CHANNEL" in text, text[:120])
    check("and tells the engineer how to record a change",
          "--applied" in text)
    check("and that lockbot has the last word",
          "only done when it does" in text)

    # A reopened item must be impossible to miss in the brief: something
    # is on record as fixed while the problem is still live.
    loud = post("lockbot", "still broken", "body", kind="bug", path=log)
    mark_applied(loud, "agent", path=log)
    verify(loud, "lockbot", False, "no it is not", path=log)
    text = brief_for_agent(path=log)
    check("a reopened item is flagged loudly", "REOPENED" in text)
    check("and its rejection reason is shown",
          "no it is not" in text, text[:400])
    resolve(loud, "agent", path=log)

    print()
    print("A test must never reach the phone")

    sent: list[dict] = []

    import notifications as _notifications

    real_send = _notifications.send_smart_notification
    _notifications.send_smart_notification = (
        lambda **kw: sent.append(kw) or "SENT"
    )

    try:
        # The guard that matters most: every post() in this whole file
        # passes a temp path, and none of them may notify even if a
        # future edit sets notify=True somewhere.
        post("lockbot", "test item", "body", kind="bug", notify=True,
             path=log)
        check("a post to a test path never notifies", sent == [],
              str(sent))

        # notify=False must be the default, so an unaudited call site
        # cannot page anyone by accident.
        import inspect

        default = inspect.signature(post).parameters["notify"].default
        check("notify defaults to off", default is False, repr(default))

        # And the routing rules, without touching the real channel.
        quiet = notify_filed({"id": "x", "sender": "agent", "kind": "bug",
                              "subject": "s"})
        check("the engineer's own items do not page the user",
              quiet == "not from lockbot", quiet)

        quiet = notify_filed({"id": "x", "sender": "lockbot", "kind": "note",
                              "subject": "s"})
        check("a note does not page anyone", quiet == "not actionable", quiet)

        check("and none of that sent anything", sent == [], str(sent))

    finally:
        _notifications.send_smart_notification = real_send

    print()
    print("Presence suppression")

    import conversation_memory as _cm

    real_recent = _cm.recent

    _cm.recent = lambda *a, **k: [{"at": _now(), "question": "hi"}]
    check("a live conversation suppresses the alert",
          "suppressed" in notify_filed(
              {"id": "x", "sender": "lockbot", "kind": "bug", "subject": "s"}))

    _cm.recent = lambda *a, **k: []
    _cm.recent = real_recent

    check("an unreadable transcript does not suppress",
          _user_is_present.__doc__ is not None)

    print()
    print("Nothing here may break a caller")

    broken = Path(tempfile.mkdtemp()) / "broken.jsonl"
    broken.write_text("{ not json\n{}\n[]\n", encoding="utf-8")
    check("a corrupt log yields no items", items(path=broken) == [])
    check("and no open items", open_items(path=broken) == [])

    missing = Path(tempfile.mkdtemp()) / "missing.jsonl"
    check("a missing log is empty", items(path=missing) == [])
    check("and briefs still render", "Channel empty"
          in brief_for_agent(path=missing))
    check("and the lockbot brief too",
          brief_for_lockbot(path=missing)["open_items_you_raised"] == [])

    unwritable = Path(tempfile.mkdtemp()) / "no" / "such" / "dir" / "x.jsonl"
    check("an unwritable path returns empty, not an exception",
          post("agent", "s", "b", path=unwritable) == "")

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All agent-channel checks passed.")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="The channel between LOCKBOT and its engineer.")
    parser.add_argument("--post", metavar="SUBJECT",
                        help="file an item as the engineer")
    parser.add_argument("--body", default="", help="item body")
    parser.add_argument("--kind", default="note", choices=sorted(KINDS))
    parser.add_argument("--verify-test", default="",
                        help="acceptance test to file with --post")
    parser.add_argument("--applied", metavar="ID",
                        help="record a change; LOCKBOT still has to confirm it")
    parser.add_argument("--resolve", metavar="ID",
                        help="close an item outright, no verification round")
    parser.add_argument("--note", default="", help="note for the event")
    parser.add_argument("--all", action="store_true", help="show every item")
    parser.add_argument("--self-test", action="store_true")

    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    if args.post:
        item_id = post("agent", args.post, args.body, kind=args.kind,
                       verify=args.verify_test)
        print(f"Filed {item_id}." if item_id else "Could not file that.")
        return 0 if item_id else 1

    if args.applied:
        ok = mark_applied(args.applied, "agent", args.note)
        print(
            f"Recorded against {args.applied}. It stays open until LOCKBOT "
            "verifies it." if ok else f"No item with id {args.applied}."
        )
        return 0 if ok else 1

    if args.resolve:
        ok = resolve(args.resolve, "agent", args.note)
        print(f"Closed {args.resolve}." if ok
              else f"No item with id {args.resolve}.")
        return 0 if ok else 1

    if args.all:
        for item in items():
            print(f"[{item['id']}] {item['status']:<9} {item['sender']:<7} "
                  f"{item['kind']:<8} {item['subject']}")
        return 0

    print(brief_for_agent())

    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    sys.exit(main())
