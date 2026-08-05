"""
conversation_memory.py — what was said, so the next answer knows it.

WHY THIS EXISTS

Every Telegram message was answered from a standing start.
lockbot_telegram.handle_message called lockbot_brain.ask(text) with the
text and nothing else, so "what about the other one?" had no other one,
"do that" had no that, and "why?" had no antecedent. The local console
session (lockbot_brain.chat) keeps a running message list and behaves
completely differently, which made the gap easy to miss: the same
assistant is coherent at the keyboard and amnesiac on the phone.

TWO DIFFERENT THINGS ARE BEING ASKED FOR

Continuity -- follow-up questions work -- and recall -- "what did we
decide about the IBIT spread last week". They need different mechanisms
and conflating them is how this goes wrong.

Continuity is solved by replaying the last few turns into the request.
It has to be BOUNDED: an unbounded history costs tokens on every message
and eventually pushes the state snapshot out of the window.

Recall is solved by searching the transcript on demand. Injecting weeks
of history to answer one question about last Tuesday is the expensive
way to be wrong, and it also invites the model to treat a stale
conversation as current -- which in a trading tool means discussing
positions that closed days ago as though they were open. Every replayed
turn therefore carries its age, and turns older than the continuity
window are searchable rather than automatic.

WHY THE TRANSCRIPT IS REDACTED ON THE WAY IN

This file is a plaintext record of everything ever typed at a system
holding live broker credentials, and Telegram is where credentials get
pasted -- a bot token was pasted into this very channel while it was
being set up. Redaction happens at write time, not at read time, so a
secret never reaches the disk in the first place. It is pattern-based
and therefore imperfect; it is a reduction in blast radius, not a
guarantee, and nothing else in LOCKBOT should rely on it.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_FOLDER = Path(__file__).resolve().parent

# Read from config rather than defined here. A second module deciding
# for itself where a data file lives is how the journal filename drifted
# apart once before and silently zeroed all performance reporting. The
# fallback exists only so this module stays importable if config cannot
# be loaded -- it must never be the path actually used in practice.
try:
    from lockbot_config import CONVERSATION_LOG_FILE as TRANSCRIPT_FILE
except Exception:  # pragma: no cover - config is normally importable
    TRANSCRIPT_FILE = PROJECT_FOLDER / "conversation_log.jsonl"

# How many past exchanges are replayed for continuity.
#
# Twelve is roughly a conversation, not a history. Past this the state
# snapshot -- positions, risk, heartbeats -- starts competing with old
# chat for the context window, and the snapshot is what makes the
# answers true.
CONTINUITY_TURNS = 12

# Nothing older than this is replayed automatically. It stays in the
# transcript and stays searchable.
CONTINUITY_HOURS = 36

# A single turn longer than this is truncated in the replay. One pasted
# log should not evict the rest of the conversation.
MAX_TURN_CHARS = 2000

# Read no more than this many lines from the tail of the transcript.
MAX_SCAN_LINES = 4000


# Patterns that must never reach the disk. Ordered most specific first.
_SECRETS = [
    # Telegram bot token: 8-10 digits, colon, 35 chars.
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"), "[REDACTED telegram token]"),
    # Anthropic keys.
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), "[REDACTED api key]"),
    # Alpaca key ids and anything shaped like a long opaque secret.
    (re.compile(r"\b(?:PK|AK)[A-Z0-9]{16,}\b"), "[REDACTED alpaca key]"),
    # key=value / key: value for obviously sensitive names.
    #
    # The identifier is matched WHOLE, prefix and suffix included, rather
    # than anchored with \b. ALPACA_SECRET_KEY=... defeated the anchored
    # version: \b does not match between an underscore and a letter, so
    # the one env var name most likely to be pasted into a chat was the
    # one name the pattern could not see.
    (re.compile(
        r"(?i)([A-Za-z0-9]*[_-]?"
        r"(?:token|secret|password|passwd|api[_-]?key)"
        r"[A-Za-z0-9_-]*)\s*[:=]\s*\S+"), r"\1=[REDACTED]"),
    # A bare 40+ char base64-ish blob.
    (re.compile(r"\b[A-Za-z0-9/+_-]{40,}={0,2}\b"), "[REDACTED long token]"),
]


def redact(text: str) -> str:
    """Strip anything that looks like a credential.

    Applied at write time. Imperfect by nature -- it cannot recognise a
    secret that looks like ordinary text -- so treat it as reducing
    exposure rather than removing it.
    """

    if not text:
        return ""

    cleaned = text

    for pattern, replacement in _SECRETS:
        cleaned = pattern.sub(replacement, cleaned)

    return cleaned


def record(
    user_id: int,
    question: str,
    reply: str,
    *,
    channel: str = "telegram",
    path: Path | None = None,
) -> None:
    """Append one exchange. Never raises -- a log must not break a reply."""

    target = Path(path or TRANSCRIPT_FILE)

    entry = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "channel": channel,
        "user_id": int(user_id),
        "question": redact(question or ""),
        "reply": redact(reply or ""),
    }

    try:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _load(path: Path | None = None) -> list[dict]:
    """Every readable entry, oldest first. Corrupt lines are skipped."""

    target = Path(path or TRANSCRIPT_FILE)

    if not target.exists():
        return []

    try:
        lines = target.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []

    entries = []

    for line in lines[-MAX_SCAN_LINES:]:
        line = line.strip()

        if not line:
            continue

        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        if isinstance(entry, dict) and entry.get("question") is not None:
            entries.append(entry)

    return entries


def _age_hours(entry: dict, *, now: datetime | None = None) -> float:
    """How long ago this exchange happened. Unparseable stamps are old."""

    moment = now or datetime.now(timezone.utc)

    try:
        when = datetime.fromisoformat(str(entry.get("at")))
    except (TypeError, ValueError):
        return float("inf")

    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    return (moment - when).total_seconds() / 3600.0


def recent(
    user_id: int | None = None,
    *,
    limit: int = CONTINUITY_TURNS,
    within_hours: float = CONTINUITY_HOURS,
    path: Path | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """The last few exchanges, oldest first, for continuity."""

    entries = _load(path)

    if user_id is not None:
        entries = [e for e in entries if e.get("user_id") == int(user_id)]

    fresh = [e for e in entries if _age_hours(e, now=now) <= within_hours]

    return fresh[-limit:] if limit > 0 else []


def _trim(text: str) -> str:
    text = str(text or "")

    if len(text) <= MAX_TURN_CHARS:
        return text

    return text[:MAX_TURN_CHARS] + " […truncated]"


def as_messages(
    turns: list[dict], *, now: datetime | None = None
) -> list[dict]:
    """Past exchanges in the API's message format.

    Each question is prefixed with how long ago it was asked. Without
    that the model reads a three-day-old conversation as the current
    one, and in a trading tool that means answering about positions that
    have since closed.
    """

    messages: list[dict] = []

    for turn in turns:
        age = _age_hours(turn, now=now)

        if age == float("inf"):
            stamp = "[earlier]"
        elif age < 1:
            stamp = "[a few minutes ago]"
        elif age < 24:
            stamp = f"[{int(age)}h ago]"
        else:
            stamp = f"[{int(age / 24)}d ago]"

        question = _trim(turn.get("question"))
        reply = _trim(turn.get("reply"))

        if not question or not reply:
            continue

        messages.append({"role": "user", "content": f"{stamp} {question}"})
        messages.append({"role": "assistant", "content": reply})

    return messages


def search(
    term: str,
    *,
    user_id: int | None = None,
    limit: int = 8,
    path: Path | None = None,
) -> list[dict]:
    """Find past exchanges mentioning a term. Newest first."""

    needle = (term or "").strip().lower()

    if not needle:
        return []

    entries = _load(path)

    if user_id is not None:
        entries = [e for e in entries if e.get("user_id") == int(user_id)]

    hits = [
        e for e in entries
        if needle in str(e.get("question", "")).lower()
        or needle in str(e.get("reply", "")).lower()
    ]

    return list(reversed(hits))[:limit]


def format_hits(hits: list[dict], term: str) -> str:
    """Search results as a readable reply."""

    if not hits:
        return f'Nothing in the transcript mentions "{term}".'

    lines = [f'{len(hits)} exchange(s) mentioning "{term}":', ""]

    for hit in hits:
        when = str(hit.get("at", "?"))[:16].replace("T", " ")
        question = str(hit.get("question", "")).strip()
        reply = str(hit.get("reply", "")).strip()

        lines.append(f"[{when}] you: {question[:180]}")
        lines.append(f"  -> {reply[:300]}")
        lines.append("")

    return "\n".join(lines).strip()


def stats(path: Path | None = None) -> dict[str, Any]:
    """How much has been remembered."""

    entries = _load(path)

    if not entries:
        return {"exchanges": 0}

    return {
        "exchanges": len(entries),
        "first": str(entries[0].get("at", "")),
        "last": str(entries[-1].get("at", "")),
        "users": len({e.get("user_id") for e in entries}),
    }


def _self_test() -> int:
    import tempfile

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            failures.append(name)
            print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))

    print("Credentials must not reach the disk")

    token = "8834646964:AAHrHxgWSb_GzQ33Kn_-jWWUPiLXXwwWDwM"
    check("a telegram token is redacted", token not in redact(f"use {token}"),
          redact(f"use {token}"))
    check("and something is left behind to show it happened",
          "REDACTED" in redact(f"use {token}"))

    check("an anthropic key is redacted",
          "sk-ant-" not in redact("sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAA"))
    check("an alpaca key id is redacted",
          "PKABCDEFGHIJKLMNOPQR" not in redact("key PKABCDEFGHIJKLMNOPQR"))
    # The env var names actually used by this project, since those are
    # what get pasted into a chat while something is being set up.
    for name in ("ALPACA_SECRET_KEY", "ALPACA_API_KEY", "TELEGRAM_BOT_TOKEN",
                 "ANTHROPIC_API_KEY", "password", "api-key"):
        check(f"{name}=value is redacted",
              "hunter2" not in redact(f"{name}=hunter2"),
              redact(f"{name}=hunter2"))

    check("password: value is redacted",
          "swordfish" not in redact("password: swordfish"))
    check("the name is kept so the log still reads",
          "ALPACA_SECRET_KEY" in redact("ALPACA_SECRET_KEY=hunter2"),
          redact("ALPACA_SECRET_KEY=hunter2"))

    ordinary = "Why did lockbot choose IBIT over the PCG put?"
    check("ordinary text survives untouched", redact(ordinary) == ordinary,
          redact(ordinary))
    check("empty input is safe", redact("") == "")

    print()
    print("Recording and reading back")

    folder = Path(tempfile.mkdtemp())
    log = folder / "conversation_log.jsonl"

    record(1, "first question", "first answer", path=log)
    record(1, "second question", "second answer", path=log)
    record(999, "someone else", "their answer", path=log)

    everyone = _load(log)
    check("every exchange is stored", len(everyone) == 3, str(len(everyone)))
    check("oldest first", everyone[0]["question"] == "first question")

    mine = recent(1, path=log)
    check("history is per user", len(mine) == 2, str(len(mine)))
    check("and excludes the other user",
          all(e["user_id"] == 1 for e in mine))
    check("still oldest first", mine[0]["question"] == "first question")

    record(1, f"my token is {token}", "noted", path=log)
    check("the transcript on disk holds no token",
          token not in log.read_text(encoding="utf-8"))

    print()
    print("Replay is bounded")

    for i in range(40):
        record(2, f"q{i}", f"a{i}", path=log)

    replayed = recent(2, path=log)
    check("replay is capped at the continuity window",
          len(replayed) == CONTINUITY_TURNS, str(len(replayed)))
    check("and keeps the most recent turns",
          replayed[-1]["question"] == "q39", replayed[-1]["question"])

    record(3, "x" * 9000, "y" * 9000, path=log)
    messages = as_messages(recent(3, path=log))
    check("an enormous turn is truncated",
          all(len(m["content"]) < MAX_TURN_CHARS + 100 for m in messages),
          str([len(m["content"]) for m in messages]))

    print()
    print("Stale conversations are not replayed as current")

    old = folder / "old.jsonl"
    old.write_text(json.dumps({
        "at": "2020-01-01T00:00:00+00:00", "channel": "telegram",
        "user_id": 1, "question": "ancient", "reply": "history",
    }) + "\n", encoding="utf-8")

    check("an old exchange is not replayed", recent(1, path=old) == [])
    check("but it is still searchable",
          len(search("ancient", path=old)) == 1)

    print()
    print("Message format")

    fresh = folder / "fresh.jsonl"
    record(1, "what about IBIT", "it is a debit spread", path=fresh)
    messages = as_messages(recent(1, path=fresh))

    check("one exchange becomes two messages", len(messages) == 2,
          str(len(messages)))
    check("the user turn comes first", messages[0]["role"] == "user")
    check("then the assistant", messages[1]["role"] == "assistant")
    check("the question is carried", "what about IBIT" in messages[0]["content"])
    check("the answer is carried",
          messages[1]["content"] == "it is a debit spread")
    check("and the age is stamped on it",
          "ago" in messages[0]["content"], messages[0]["content"])

    # An exchange with no reply would replay as a dangling user turn,
    # which the API rejects outright.
    half = folder / "half.jsonl"
    record(1, "unanswered", "", path=half)
    check("an exchange with no reply is dropped from the replay",
          as_messages(recent(1, path=half)) == [])

    print()
    print("Search")

    hits = search("IBIT", path=fresh)
    check("a term is found", len(hits) == 1, str(len(hits)))
    check("case does not matter", len(search("ibit", path=fresh)) == 1)
    check("the reply is searched too",
          len(search("debit spread", path=fresh)) == 1)
    check("a miss returns nothing", search("nonexistent", path=fresh) == [])
    check("an empty term returns nothing", search("", path=fresh) == [])
    check("a miss reads as a miss",
          "Nothing in the transcript" in format_hits([], "zzz"))

    print()
    print("Nothing here may break a reply")

    broken = folder / "broken.jsonl"
    broken.write_text("{ not json\n{}\n", encoding="utf-8")
    check("a corrupt transcript yields no history", recent(1, path=broken) == [])

    missing = folder / "does_not_exist.jsonl"
    check("a missing transcript yields no history",
          recent(1, path=missing) == [])
    check("and no stats", stats(missing) == {"exchanges": 0})

    try:
        record(1, "q", "a", path=folder / "no" / "such" / "dir" / "x.jsonl")
        check("an unwritable path does not raise", True)
    except Exception as error:
        check("an unwritable path does not raise", False, str(error))

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All conversation-memory checks passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    info = stats()

    if not info["exchanges"]:
        print("No conversations recorded yet.")
    else:
        print(f"{info['exchanges']} exchange(s) with {info['users']} user(s)")
        print(f"  first: {info['first']}")
        print(f"  last : {info['last']}")
