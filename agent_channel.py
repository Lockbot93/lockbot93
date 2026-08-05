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

MAX_SCAN_LINES = 5000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def post(
    sender: str,
    subject: str,
    body: str,
    *,
    kind: str = "note",
    refs: list[str] | None = None,
    path: Path | None = None,
) -> str:
    """File one item. Returns its id.

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
        "refs": [str(r)[:120] for r in (refs or [])][:12],
    }

    try:
        with Path(path or CHANNEL_FILE).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        return ""

    return item_id


def resolve(
    item_id: str,
    by: str,
    note: str = "",
    *,
    path: Path | None = None,
) -> bool:
    """Mark an item done. Appended, never overwritten."""

    by = str(by or "").strip().lower()

    if by not in SENDERS or not item_id:
        return False

    if not any(i["id"] == item_id for i in items(path=path)):
        return False

    record = {
        "resolves": str(item_id),
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


def items(*, path: Path | None = None) -> list[dict]:
    """Every item, oldest first, each carrying its resolution state."""

    records = _records(path)

    resolutions: dict[str, dict] = {}

    for record in records:
        if record.get("resolves"):
            resolutions[str(record["resolves"])] = record

    out = []

    for record in records:
        if record.get("resolves") or not record.get("id"):
            continue

        entry = dict(record)
        resolution = resolutions.get(str(record["id"]))

        entry["resolved"] = resolution is not None
        entry["resolution"] = resolution

        out.append(entry)

    return out


def open_items(*, sender: str | None = None, path: Path | None = None) -> list[dict]:
    """Unresolved, actionable items. Optionally only from one side.

    `sender` filters by who RAISED it, which is how each side finds work
    waiting on them: the engineer reads what LOCKBOT raised.
    """

    out = [
        i for i in items(path=path)
        if i["kind"] in ACTIONABLE and not i["resolved"]
    ]

    if sender:
        out = [i for i in out if i["sender"] == str(sender).strip().lower()]

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

    return {
        "what_this_is": (
            "Messages between you and the engineer who edits your code. "
            "You cannot change code; file a 'bug' here and it will be "
            "picked up. Items stay open until resolved."
        ),
        "open_items_you_raised": [
            {"id": i["id"], "at": i["at"], "kind": i["kind"],
             "subject": i["subject"]}
            for i in everything
            if i["sender"] == "lockbot" and i["kind"] in ACTIONABLE
            and not i["resolved"]
        ],
        "waiting_on_you": [
            {"id": i["id"], "at": i["at"], "subject": i["subject"],
             "body": i["body"][:600]}
            for i in everything
            if i["sender"] == "agent" and i["kind"] in ACTIONABLE
            and not i["resolved"]
        ],
        "recently_done_by_the_engineer": [
            {"id": i["id"], "at": i["at"], "subject": i["subject"],
             "body": i["body"][:600]}
            for i in everything
            if i["sender"] == "agent" and i["kind"] == "fix"
        ][-8:],
        "your_items_now_resolved": [
            {"id": i["id"], "subject": i["subject"],
             "resolved_by": i["resolution"].get("by"),
             "note": i["resolution"].get("note", "")[:400]}
            for i in everything
            if i["sender"] == "lockbot" and i["resolved"]
        ][-8:],
    }


def brief_for_agent(*, path: Path | None = None) -> str:
    """What the engineer should read at the start of a session."""

    everything = items(path=path)
    waiting = [
        i for i in everything
        if i["sender"] == "lockbot" and i["kind"] in ACTIONABLE
        and not i["resolved"]
    ]
    questions = [i for i in waiting if i["kind"] == "question"]
    bugs = [i for i in waiting if i["kind"] == "bug"]

    if not everything:
        return (
            "Nothing in the channel yet.\n\n"
            "LOCKBOT files items here with the file_for_engineer tool; "
            "post back with agent_channel.post('agent', ...) and resolve "
            "with agent_channel.resolve(id, 'agent', note)."
        )

    lines = [
        "=" * 68,
        "FROM LOCKBOT",
        "=" * 68,
        f"  {len(bugs)} open bug(s), {len(questions)} open question(s), "
        f"{len(everything)} item(s) total",
        "",
    ]

    if not waiting:
        lines.append("  Nothing open. ")
    else:
        for item in waiting:
            lines.append(f"  [{item['id']}] {item['kind'].upper()}: "
                         f"{item['subject']}")
            lines.append(f"      filed {item['at']}")

            for line in item["body"].splitlines()[:14]:
                lines.append(f"      {line}")

            if item.get("refs"):
                lines.append(f"      refs: {', '.join(item['refs'])}")

            lines.append("")

    recent_fixes = [i for i in everything if i["kind"] == "fix"][-5:]

    if recent_fixes:
        lines.append("-" * 68)
        lines.append("RECENT FIXES ON RECORD")
        lines.append("-" * 68)

        for item in recent_fixes:
            lines.append(f"  [{item['id']}] {item['at'][:16]}  {item['subject']}")

    lines.append("")
    lines.append("Resolve with: "
                 "python agent_channel.py --resolve <id> --note \"...\"")

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
    print("Resolution is what makes this more than a suggestion box")

    check("resolving an unknown id fails",
          resolve("nope", "agent", path=log) is False)
    check("resolving needs a real sender",
          resolve(bug_id, "hacker", path=log) is False)

    check("the bug resolves", resolve(bug_id, "agent", "rounded in the "
                                      "constructor", path=log) is True)
    check("and is no longer open", open_items(path=log) == [])

    resolved = [i for i in items(path=log) if i["id"] == bug_id][0]
    check("the item records who resolved it",
          resolved["resolution"]["by"] == "agent")
    check("and why", "constructor" in resolved["resolution"]["note"])

    # The original item text must survive resolution: an append-only log
    # is the point, so history is not rewritten.
    check("the original body is untouched",
          "56.00000000000001" in resolved["body"])

    print()
    print("Each side sees what is waiting on it")

    q_id = post("agent", "Did the short patch land?", "check signals.csv",
                kind="question", path=log)
    lockbot_view = brief_for_lockbot(path=log)

    check("lockbot sees the engineer's open question",
          any(i["id"] == q_id for i in lockbot_view["waiting_on_you"]))
    check("lockbot sees its own resolved item",
          any(i["id"] == bug_id for i in lockbot_view["your_items_now_resolved"]))
    check("and the resolution note comes with it",
          "constructor" in lockbot_view["your_items_now_resolved"][0]["note"])
    check("lockbot has no open items of its own now",
          lockbot_view["open_items_you_raised"] == [])

    post("agent", "Rounded entry_debit", "constructor now rounds",
         kind="fix", path=log)
    lockbot_view = brief_for_lockbot(path=log)
    check("fixes are advertised so it stops re-reporting them",
          any("Rounded" in i["subject"]
              for i in lockbot_view["recently_done_by_the_engineer"]))

    text = brief_for_agent(path=log)
    check("the engineer brief renders", "FROM LOCKBOT" in text)
    check("and mentions the open question is not theirs to answer",
          "0 open bug" in text, text[:200])

    print()
    print("Nothing here may break a caller")

    broken = Path(tempfile.mkdtemp()) / "broken.jsonl"
    broken.write_text("{ not json\n{}\n[]\n", encoding="utf-8")
    check("a corrupt log yields no items", items(path=broken) == [])
    check("and no open items", open_items(path=broken) == [])

    missing = Path(tempfile.mkdtemp()) / "missing.jsonl"
    check("a missing log is empty", items(path=missing) == [])
    check("and briefs still render", "Nothing in the channel"
          in brief_for_agent(path=missing))

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
    parser.add_argument("--resolve", metavar="ID", help="mark an item done")
    parser.add_argument("--note", default="", help="resolution note")
    parser.add_argument("--all", action="store_true", help="show every item")
    parser.add_argument("--self-test", action="store_true")

    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    if args.post:
        item_id = post("agent", args.post, args.body, kind=args.kind)
        print(f"Filed {item_id}." if item_id else "Could not file that.")
        return 0 if item_id else 1

    if args.resolve:
        ok = resolve(args.resolve, "agent", args.note)
        print(f"Resolved {args.resolve}." if ok
              else f"No open item with id {args.resolve}.")
        return 0 if ok else 1

    if args.all:
        for item in items():
            mark = "done" if item["resolved"] else "OPEN"
            print(f"[{item['id']}] {mark:<4} {item['sender']:<7} "
                  f"{item['kind']:<8} {item['subject']}")
        return 0

    print(brief_for_agent())

    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    sys.exit(main())
