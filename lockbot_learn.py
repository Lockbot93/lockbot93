"""
lockbot_learn.py  --  LOCKBOT studying its own results  (v1.0)

WHAT THIS IS
    An unattended pass over everything LOCKBOT has recorded, looking for
    what is TRUE NOW that was not true last time. Anything genuinely new
    goes into brain_memory.md, so the next session starts knowing it.

THE PROBLEM IT SOLVES
    Learning was manual. Somebody had to think to ask, and every session
    started from zero — the inverted volume ranking would have been
    rediscovered next week and reported as news.

WHY IT DIFFS INSTEAD OF SUMMARISING
    A nightly job that re-analyses from scratch produces the same five
    observations every night, and a memory file that repeats itself is
    one nobody reads. So the model is given what it already knows and
    asked a narrower question: what changed, and what is newly supported
    by evidence that was not there before?

    Nothing new is a valid and common answer. Saying so beats inventing
    a finding to justify the run.

HYPOTHESES, NOT JUST NOTES
    A finding is an observation. A hypothesis is a claim with a test
    attached, and it is what actually moves a strategy forward.

    Each pass may open at most one, recorded in learning_log.jsonl with
    the evidence behind it and what would settle it. Later passes revisit
    open ones and mark them supported, contradicted, or still open. That
    is the difference between a system that reports and one that learns:
    it has to be able to be WRONG about something and find out.

    One at a time is deliberate. Several changes in flight at once means
    nothing can be attributed to anything.

WHAT IT WILL NOT DO
    It never edits configuration, never places an order, and never
    changes how LOCKBOT trades. It writes two files: a memory note and a
    hypothesis log. Every decision stays yours.

USAGE
    python lockbot_learn.py              one learning pass
    python lockbot_learn.py --dry-run    print, write nothing
    python lockbot_learn.py --log        show the hypothesis log
    python lockbot_learn.py --self-test  offline checks
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_FOLDER = Path(__file__).resolve().parent
LEARNING_LOG = PROJECT_FOLDER / "learning_log.jsonl"

# Fable 5. Thinking is always on and cannot be disabled — this file never
# set it, so nothing changes here beyond the model string. This is the one
# task where the extra reasoning is worth paying for: telling a real
# pattern from a small-sample accident is the hardest judgement LOCKBOT
# makes, and getting it wrong sends you off optimising noise.
MODEL = os.getenv("LOCKBOT_MODEL", "claude-fable-5")
MAX_TOKENS = 32000
EFFORT = "xhigh"

# Groupings worth re-checking every pass. Each is computed in Python and
# handed over as arithmetic, not asked of the model.
BREAKDOWNS = ("regime", "volume_ratio", "taken", "confidence")


def load_log(path: Path | None = None) -> list[dict]:
    """Read the hypothesis log. A corrupt line is skipped, not fatal."""

    path = path or LEARNING_LOG

    if not path.exists():
        return []

    entries = []

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line:
            continue

        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    return entries


def append_log(entry: dict, path: Path | None = None) -> None:
    """Append one entry. The log is append-only — history is the point."""

    path = path or LEARNING_LOG

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def open_hypotheses(entries: list[dict]) -> list[dict]:
    """
    Hypotheses still awaiting a verdict.

    A later entry with the same id supersedes an earlier one, so
    resolving a hypothesis means appending rather than rewriting.
    """

    latest: dict[str, dict] = {}

    for entry in entries:
        if entry.get("type") != "hypothesis":
            continue

        identifier = entry.get("id")

        if identifier:
            latest[identifier] = entry

    return [e for e in latest.values() if e.get("status") == "open"]


def gather_evidence() -> dict:
    """Collect everything the pass reasons over. Pure reads."""

    from lockbot_brain import collect_state, read_memory
    from lockbot_brain import shadow_breakdown

    state = collect_state()

    # Operational failures, not just trading results. Without these the
    # pass could only ever learn about the market — it had no idea
    # LOCKBOT itself had failed 26 times in three days.
    try:
        from lockbot_incidents import collect as collect_incidents

        incidents = collect_incidents(days=7)
    except Exception:
        incidents = {"incidents": [], "recurring": []}

    return {
        "state": state,
        "memory": read_memory(),
        "breakdowns": {name: shadow_breakdown(name) for name in BREAKDOWNS},
        "open_hypotheses": open_hypotheses(load_log()),
        "incidents": incidents,
    }


LEARN_PROMPT = """You are reviewing LOCKBOT's own results to work out what is
newly true.

You have four things: the current state, the notes you have already written,
statistical breakdowns of the resolved shadow trades, and any hypotheses still
open from earlier passes.

Do three jobs, in order.

1. REVISIT OPEN HYPOTHESES
   For each one, decide from the evidence whether it is now supported,
   contradicted, or still open, and say why in one sentence. If the sample has
   not grown enough to say anything, it is still open — say that rather than
   forcing a verdict.

2. LEARN FROM FAILURES
   You are given LOCKBOT's own incidents — crashes, component failures,
   self-repairs, rejected orders — grouped by fingerprint with a count. A
   recurring incident is worth more attention than a severe one-off: it is
   a fault that is not being fixed, only survived. Say what is recurring,
   what it suggests is wrong, and whether it looks handled or ignored.

   Distinguish "the environment failed" (a dropped network) from "LOCKBOT
   is wrong" (a health check that fails on a normal outcome). Only the
   second is a defect.

3. FIND WHAT IS NEW
   Compare against your existing notes. Report only what they do not already
   contain: a pattern that has appeared, a number that has moved materially, a
   thing that broke. Do NOT restate things you already know. If nothing is new,
   say so plainly — that is a normal outcome, not a failure.

4. OPEN AT MOST ONE HYPOTHESIS
   Only if the evidence genuinely supports one. It must be falsifiable and name
   what would settle it. One at a time, because several changes in flight means
   nothing can be attributed to anything. If nothing warrants one, open none.

Be rigorous about sample size. Below about 30 resolved trades in a group,
almost nothing is distinguishable from noise, and saying "too few to tell" is
the correct answer far more often than a pattern is real. Never describe a
difference as meaningful without giving its n.

Check that a mechanism is switched ON before predicting what it will do. The
configuration contains several settings that are computed and reported but not
acted on — ENABLE_PAPER_EXITS is false, so the break-even and trailing-stop
thresholds are evaluated for alerting and never close a position; volume_ratio
is logged at weight zero and does not affect ranking. A threshold that is
mis-sized but inert is a LATENT problem worth recording as a finding, not an
active cause worth building a hypothesis on. If you are about to claim
something will happen, name the switch that makes it happen and confirm its
value in the state you were given.

Reply as JSON, nothing else:

{
  "hypothesis_updates": [
    {"id": "...", "status": "supported|contradicted|open", "reasoning": "..."}
  ],
  "failure_notes": [
    "What is recurring, and whether it is a LOCKBOT defect or the environment."
  ],
  "new_findings": [
    "One sentence, specific, self-contained, safe to read cold in six weeks."
  ],
  "new_hypothesis": {
    "claim": "...",
    "evidence": "...",
    "test": "what would settle this",
    "proposed_change": "the single config change that would follow, or null"
  },
  "summary": "One or two sentences for a human skimming this."
}

new_findings may be empty. new_hypothesis may be null. Both are normal."""


def run(dry_run: bool = False) -> int:
    """Run one learning pass."""

    from anthropic import Anthropic
    from dotenv import load_dotenv
    import os

    load_dotenv(PROJECT_FOLDER / ".env")

    api_key = os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY")

    if not api_key:
        print("No Claude API key. Set CLAUDE_API_KEY in .env.")
        return 1

    print("Gathering evidence...")
    evidence = gather_evidence()

    resolved = evidence["state"].get("shadow_summary", {}).get("resolved", 0)
    print(f"  {resolved} resolved shadow trades")
    print(f"  {len(evidence['open_hypotheses'])} open hypothesis/hypotheses")
    print("\nThinking...\n")

    client = Anthropic(api_key=api_key)

    # Streaming, not create(). At this max_tokens the SDK refuses a
    # non-streaming call outright — a request that may run past ten
    # minutes would hit an idle-connection timeout and be lost.
    with client.messages.stream(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        output_config={"effort": EFFORT},
        system=[
            {
                "type": "text",
                "text": (
                    "You are LOCKBOT's analyst. You are rigorous about sample "
                    "size and you would rather report nothing than manufacture "
                    "a pattern. You reply with JSON only."
                ),
            },
            {
                "type": "text",
                "text": (
                    "EXISTING NOTES (do not repeat these):\n"
                    + evidence["memory"]
                    + "\n\nOPEN HYPOTHESES:\n"
                    + json.dumps(evidence["open_hypotheses"], indent=2)
                    + "\n\nINCIDENTS — LOCKBOT's own failures:\n"
                    + json.dumps(evidence["incidents"], indent=2, default=str)
                    + "\n\nSHADOW BREAKDOWNS:\n"
                    + json.dumps(evidence["breakdowns"], indent=2, default=str)
                    + "\n\nCURRENT STATE:\n"
                    + json.dumps(evidence["state"], indent=2, default=str)
                ),
                "cache_control": {"type": "ephemeral"},
            },
        ],
        messages=[{"role": "user", "content": LEARN_PROMPT}],
    ) as stream:
        response = stream.get_final_message()

    if response.stop_reason == "refusal":
        print("Claude declined this request.")
        return 1

    text = "".join(b.text for b in response.content if b.type == "text").strip()

    # The model was asked for bare JSON, but a stray fence is a cheap
    # thing to survive rather than fail on.
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]

    try:
        result = json.loads(text.strip())
    except json.JSONDecodeError as error:
        print(f"Could not parse the reply: {error}\n")
        print(text[:1500])
        return 1

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    print("=" * 62)
    print("LEARNING PASS")
    print("=" * 62)
    print(f"\n{result.get('summary', '(no summary)')}\n")

    updates = result.get("hypothesis_updates") or []

    if updates:
        print("Hypothesis updates:")
        for update in updates:
            print(f"  [{update.get('status', '?').upper()}] {update.get('id')}")
            print(f"      {update.get('reasoning', '')}")
        print()

    failure_notes = result.get("failure_notes") or []

    if failure_notes:
        print("Failures:")
        for note in failure_notes:
            print(f"  - {note}")
        print()

    findings = result.get("new_findings") or []

    if findings:
        print(f"New findings ({len(findings)}):")
        for finding in findings:
            print(f"  - {finding}")
        print()
    else:
        print("No new findings. Nothing has changed enough to record.\n")

    hypothesis = result.get("new_hypothesis")

    if hypothesis:
        print("New hypothesis:")
        print(f"  claim    : {hypothesis.get('claim')}")
        print(f"  evidence : {hypothesis.get('evidence')}")
        print(f"  test     : {hypothesis.get('test')}")
        print(f"  change   : {hypothesis.get('proposed_change')}")
        print()

    if dry_run:
        print("(dry run — nothing written)")
        return 0

    from lockbot_brain import append_memory

    # Failure notes go into memory too. A defect nobody records is one
    # that gets rediscovered instead of fixed.
    for note in failure_notes:
        append_memory(f"FAILURE: {note}")

    for finding in findings:
        append_memory(finding)

    for update in updates:
        append_log(
            {
                "type": "hypothesis",
                "id": update.get("id"),
                "status": update.get("status"),
                "reasoning": update.get("reasoning"),
                "resolved_at": now,
            }
        )

    if hypothesis:
        identifier = f"H{len([e for e in load_log() if e.get('type') == 'hypothesis']) + 1}"

        append_log(
            {
                "type": "hypothesis",
                "id": identifier,
                "status": "open",
                "claim": hypothesis.get("claim"),
                "evidence": hypothesis.get("evidence"),
                "test": hypothesis.get("test"),
                "proposed_change": hypothesis.get("proposed_change"),
                "opened_at": now,
                "shadow_resolved_at_open": resolved,
            }
        )

        print(f"Opened {identifier}.")

    append_log(
        {
            "type": "pass",
            "at": now,
            "shadow_resolved": resolved,
            "findings": len(findings),
            "summary": result.get("summary"),
        }
    )

    print(f"Wrote {len(findings)} note(s) to brain_memory.md.")

    return 0


def show_log() -> int:
    """Print the hypothesis history."""

    entries = load_log()

    if not entries:
        print("No learning passes yet.")
        return 0

    passes = [e for e in entries if e.get("type") == "pass"]
    hypotheses = [e for e in entries if e.get("type") == "hypothesis"]

    print(f"{len(passes)} pass(es), {len(hypotheses)} hypothesis entr(ies)\n")

    latest: dict[str, dict] = {}

    for entry in hypotheses:
        if entry.get("id"):
            latest[entry["id"]] = {**latest.get(entry["id"], {}), **entry}

    for identifier, entry in sorted(latest.items()):
        print(f"[{entry.get('status', '?').upper()}] {identifier}")

        if entry.get("claim"):
            print(f"  claim : {entry['claim']}")
        if entry.get("test"):
            print(f"  test  : {entry['test']}")
        if entry.get("reasoning"):
            print(f"  verdict: {entry['reasoning']}")

        print()

    return 0


def _self_test() -> int:
    """Offline checks. No network, no API key."""

    import tempfile

    failures = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name} {detail}")
            failures.append(name)

    print("Hypothesis log")

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "log.jsonl"

        check("a missing log is empty", load_log(path) == [])

        append_log({"type": "hypothesis", "id": "H1", "status": "open",
                    "claim": "x"}, path)
        check("appends", len(load_log(path)) == 1)
        check("one open", len(open_hypotheses(load_log(path))) == 1)

        append_log({"type": "hypothesis", "id": "H1", "status": "supported",
                    "reasoning": "y"}, path)
        check("a later entry supersedes", len(open_hypotheses(load_log(path))) == 0)
        check("history is kept", len(load_log(path)) == 2)

        append_log({"type": "hypothesis", "id": "H2", "status": "open"}, path)
        check("a second opens", len(open_hypotheses(load_log(path))) == 1)
        check("and it is the right one",
              open_hypotheses(load_log(path))[0]["id"] == "H2")

        append_log({"type": "pass", "at": "now"}, path)
        check("passes are not hypotheses",
              len(open_hypotheses(load_log(path))) == 1)

        with path.open("a", encoding="utf-8") as handle:
            handle.write("{ this is not json\n")

        check("a corrupt line is skipped", len(load_log(path)) == 4)
        check("and does not break open-hypothesis lookup",
              len(open_hypotheses(load_log(path))) == 1)

    print()
    print("Prompt")

    # Collapse whitespace first — the prompt is wrapped, so a phrase can
    # straddle a line break and a naive substring check misses it.
    flat = " ".join(LEARN_PROMPT.lower().split())

    check("asks for JSON only", "json" in flat)
    check("requires sample sizes", "sample size" in flat)
    check("permits an empty result", "may be empty" in flat)
    check("caps hypotheses at one", "at most one" in flat)
    check("tells it not to repeat itself", "do not already contain" in flat)
    check("allows 'nothing new' as an answer", "nothing is new" in flat)
    check("demands falsifiability", "falsifiable" in flat)

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All learning checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="LOCKBOT studying its own results.")
    parser.add_argument("--dry-run", action="store_true", help="print, write nothing")
    parser.add_argument("--log", action="store_true", help="show the hypothesis log")
    parser.add_argument("--self-test", action="store_true", help="offline checks")

    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    if args.log:
        return show_log()

    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
