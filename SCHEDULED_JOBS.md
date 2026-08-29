# Scheduled jobs, and why this file exists

These run in Windows Task Scheduler, NOT in the repository. That means
they are invisible to git, invisible to preflight, and lost entirely if
the machine is rebuilt. Discovered on 2026-08-28 while adding one: the
commit reported "nothing to commit", because the work had happened
outside anything version control can see.

Recorded here so the schedule can be rebuilt from the repo.

| task | runs | what it does |
|---|---|---|
| LockBot Wake | boot / interval | starts the controller |
| LockBot Universe Rebuild | daily | rebuilds universe.csv |
| LockBot ETF Portfolio | daily | the buy-and-hold sleeve |
| LockBot Shadow Resolve | daily 15:15 | resolves equity + options shadow setups |
| **LockBot Candidate Resolve** | **daily 15:25** | **resolves the setups LOCKBOT ranked and DID NOT take** |
| LockBot Learning Pass | nightly | LOCKBOT reads its own logs and files findings |

## The one added 2026-08-28

    name        LockBot Candidate Resolve
    runs        C:\LockBot\Medlockbot\.venv\Scripts\python.exe
                candidate_resolution.py --limit 400
    working dir C:\LockBot\Medlockbot
    trigger     daily 15:25 local
    settings    StartWhenAvailable, 30-minute limit,
                MultipleInstances IgnoreNew
    principal   jtmed, Interactive, Limited

**Why 15:25.** After the 15:00 close so the day's rows exist, and after
Shadow Resolve at 15:15 so the two resolvers never contend for the log.

**Why it matters.** It answers the only question LOCKBOT named as the way
it learns to win: were the ~39 setups a session it discards better than
the one it takes? Until now it ran only by hand. 1,506 candidates resolved
as of its first scheduled fire.

It places no orders, writes only its own derived file, and never rewrites
the source log. Worst case of a bad run is deleting one CSV and re-running.

## What is deliberately NOT scheduled

Any job that continuously ingests online sentiment or attention. The
plumbing is trivial -- six tasks prove it -- but every continuous input
tested is negative: news spikes at -0.154R against a price-matched
control, and published work putting WSB attention at -8.5% holding period
returns. A scheduled job feeding a signal measured worse than random makes
LOCKBOT worse on a timer.

Find the input first. Then build the pipe.

**One correction, 2026-08-29.** An earlier version of this reasoning was
told to the owner alongside the claim that LOCKBOT has no web access. That
claim was false -- lockbot_brain.py sets WEB_SEARCH = True and the brain
carries Anthropic's server-side search tool. The trading path has no
internet; the brain does. The argument above is unaffected, since it is
about whether a continuous sentiment feed is worth ingesting, not about
whether one is technically possible.
