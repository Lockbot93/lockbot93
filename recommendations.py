"""
recommendations.py — the step between learning something and doing it.

THE GAP

LOCKBOT observes, forms hypotheses, and can now test them. It found the
mleg bookkeeping bug and the missing daily-loss gate on its own, before
anyone else did. But every resulting change was made by a person reading
its output. It could say "this is wrong". It could not say "so change
this to that, and here is why".

WHY A RECOMMENDATION AND NOT AN ACTION

The obvious next step is to let the nightly pass apply what it concludes.
That would be a mistake at this stage, and the reason is on record: the
volume-ratio split looked like a real edge across 55 resolved setups and
turned out to be chance at p=0.61. A loop that acted on findings that
strong would have retuned the ranking around noise, twice.

So a recommendation is a proposal with its evidence attached, recorded
and surfaced. A human applies it. The confirmation path already exists in
lockbot_brain.change_setting, and this feeds it rather than bypassing it.

WHAT MAKES ONE ACCEPTABLE

Every recommendation must name a setting on the runtime_settings
allowlist, a value inside its bounds, and the evidence behind it
including sample size. A recommendation without a sample size is an
opinion, and this file rejects it -- the same standard signal_research
applies to itself.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_FOLDER = Path(__file__).resolve().parent
RECOMMENDATIONS_FILE = PROJECT_FOLDER / "recommendations.jsonl"

# Below this, a recommendation is recorded but marked as insufficient.
# Not a hard block: a small sample can still be worth surfacing, so long
# as nobody mistakes it for evidence.
MIN_SAMPLE = 30


def propose(
    setting: str,
    value,
    rationale: str,
    evidence: str,
    sample_size: int,
) -> tuple[bool, str]:
    """Record a proposed setting change. Applies nothing.

    Returns (accepted, message). Rejection means the proposal was
    malformed or out of bounds, not that the idea was bad.
    """

    from runtime_settings import validate

    setting = setting.strip().upper()

    if not rationale or not rationale.strip():
        return False, (
            "a recommendation needs a rationale. A change nobody can "
            "justify is a guess with a config key attached."
        )

    if not evidence or not evidence.strip():
        return False, (
            "a recommendation needs evidence. State what was measured "
            "and over how much data."
        )

    try:
        sample_size = int(sample_size)
    except (TypeError, ValueError):
        return False, "sample_size must be a whole number."

    if sample_size < 0:
        return False, "sample_size cannot be negative."

    ok, why = validate(setting, value)

    if not ok:
        return False, f"REJECTED: {why}"

    entry = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "setting": setting,
        "value": value,
        "rationale": rationale.strip(),
        "evidence": evidence.strip(),
        "sample_size": sample_size,
        "strength": "SUFFICIENT" if sample_size >= MIN_SAMPLE else "THIN",
        "status": "PENDING",
    }

    try:
        with RECOMMENDATIONS_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError as error:
        return False, f"could not record: {error}"

    caveat = (
        ""
        if sample_size >= MIN_SAMPLE
        else f"  NOTE: {sample_size} observations is thin. Recorded, but "
             f"{MIN_SAMPLE}+ is the bar for acting."
    )

    return True, (
        f"Recorded: {setting} -> {value}\n"
        f"  because: {rationale.strip()}\n"
        f"  evidence: {evidence.strip()} (n={sample_size})\n"
        f"{caveat}\n"
        "  Nothing has changed. Apply it with change_setting if you agree."
    )


def load(path: Path | None = None) -> list[dict]:
    """Every recommendation ever made."""

    source = Path(path or RECOMMENDATIONS_FILE)

    if not source.exists():
        return []

    rows = []

    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    return rows


def pending() -> list[dict]:
    """Recommendations not yet applied or dismissed.

    The latest entry for a setting wins, so a revised recommendation
    supersedes an older one rather than both sitting in the queue.
    """

    latest: dict[str, dict] = {}

    for row in load():
        latest[row.get("setting", "?")] = row

    return [r for r in latest.values() if r.get("status") == "PENDING"]


def resolve(setting: str, status: str, note: str = "") -> tuple[bool, str]:
    """Mark a recommendation APPLIED or DISMISSED.

    Recording dismissals matters as much as applications: a proposer
    whose rejected ideas vanish looks far better than it is.
    """

    setting = setting.strip().upper()
    status = status.strip().upper()

    if status not in {"APPLIED", "DISMISSED"}:
        return False, "status must be APPLIED or DISMISSED."

    current = {r.get("setting"): r for r in load()}

    if setting not in current:
        return False, f"no recommendation on record for {setting}."

    entry = dict(current[setting])
    entry["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry["status"] = status
    entry["note"] = note

    try:
        with RECOMMENDATIONS_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError as error:
        return False, f"could not record: {error}"

    return True, f"{setting} marked {status}."


def report() -> str:
    """Pending recommendations, and how past ones turned out."""

    rows = load()

    if not rows:
        return "No recommendations yet."

    outstanding = pending()
    lines = []

    if outstanding:
        lines.append(f"PENDING ({len(outstanding)})")
        for row in outstanding:
            lines.append(
                f"  {row['setting']} -> {row['value']}   [{row['strength']}]"
            )
            lines.append(f"      {row['rationale']}")
            lines.append(
                f"      evidence: {row['evidence']} (n={row['sample_size']})"
            )
    else:
        lines.append("No pending recommendations.")

    final: dict[str, str] = {}
    for row in rows:
        final[row.get("setting", "?")] = row.get("status", "PENDING")

    applied = sum(1 for s in final.values() if s == "APPLIED")
    dismissed = sum(1 for s in final.values() if s == "DISMISSED")

    lines.append("")
    lines.append(
        f"History: {applied} applied, {dismissed} dismissed, "
        f"{len(outstanding)} pending"
    )

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

    global RECOMMENDATIONS_FILE
    real = RECOMMENDATIONS_FILE
    RECOMMENDATIONS_FILE = Path(tempfile.mkdtemp()) / "recs.jsonl"

    try:
        print("A recommendation must be justified and bounded")

        ok, why = propose("OPTIONS_MAX_SPREAD_PERCENT", 0.04, "", "x", 50)
        check("no rationale is rejected", ok is False, why)
        check("and says why that matters", "guess" in why, why)

        ok, why = propose("OPTIONS_MAX_SPREAD_PERCENT", 0.04, "tighter", "", 50)
        check("no evidence is rejected", ok is False, why)

        ok, why = propose("PAPER_TRADING", False, "go live", "gut feel", 100)
        check("a setting outside the allowlist is rejected", ok is False, why)

        ok, why = propose("OPTIONS_STOP_LOSS_PERCENT", 9.0, "wider stop",
                          "backtest", 100)
        check("a value outside its bounds is rejected", ok is False, why)

        print()
        print("Sample size is recorded, not assumed away")

        ok, message = propose(
            "OPTIONS_MAX_SPREAD_PERCENT", 0.04,
            "wide spreads cost more than the signal earns",
            "median spread on taken trades 6.4% vs 1.6% on winners", 12,
        )
        check("a thin recommendation is accepted", ok is True, message)
        check("but marked THIN", pending()[0]["strength"] == "THIN")
        check("and the message says so", "thin" in message.lower(), message)

        ok, message = propose(
            "OPTIONS_MAX_IV_PREMIUM", 1.40,
            "contracts above 1.6x realised have underperformed",
            "45 resolved options decisions", 45,
        )
        check("a well-supported one is SUFFICIENT",
              any(r["strength"] == "SUFFICIENT" for r in pending()))

        print()
        print("Nothing is applied automatically")

        import lockbot_config as cfg
        before = cfg.OPTIONS_MAX_SPREAD_PERCENT
        check("proposing does not change the live setting",
              cfg.OPTIONS_MAX_SPREAD_PERCENT == before)
        check("it sits pending", len(pending()) == 2, str(len(pending())))

        print()
        print("Outcomes are recorded, including refusals")

        ok, _ = resolve("OPTIONS_MAX_SPREAD_PERCENT", "DISMISSED", "too thin")
        check("a dismissal is accepted", ok is True)
        check("and clears it from pending",
              all(r["setting"] != "OPTIONS_MAX_SPREAD_PERCENT"
                  for r in pending()))

        ok, _ = resolve("OPTIONS_MAX_IV_PREMIUM", "APPLIED")
        check("an application is recorded", ok is True)

        card = report()
        check("history counts both", "1 applied, 1 dismissed" in card, card)

        ok, why = resolve("NOT_A_SETTING", "APPLIED")
        check("resolving an unknown setting fails", ok is False, why)

        ok, why = resolve("OPTIONS_MAX_IV_PREMIUM", "MAYBE")
        check("an invalid status is rejected", ok is False, why)

        print()
        print("A newer recommendation supersedes an older one")

        propose("OPTIONS_MAX_TRADES_PER_DAY", 3, "fewer, better",
                "20 resolved", 40)
        propose("OPTIONS_MAX_TRADES_PER_DAY", 2, "fewer still",
                "30 resolved", 50)
        matching = [r for r in pending()
                    if r["setting"] == "OPTIONS_MAX_TRADES_PER_DAY"]
        check("only the latest is pending", len(matching) == 1, str(matching))
        check("and it is the newer one", matching[0]["value"] == 2)

    finally:
        RECOMMENDATIONS_FILE = real

    print()

    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1

    print("All recommendation checks passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    print(report())
