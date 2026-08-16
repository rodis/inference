"""The Prefect entry point (ADR 0012).

**The only file in the tier that imports Prefect**, and the reason that matters is the runner
question is unsettled by design: Prefect Cloud's Managed pool was chosen because its free tier
*executes* code rather than only scheduling it, so nothing lands on the cluster — but if that
changes, the swap is this file. Everything below it (`app`, `core`, the adapters) stays put,
which is also why the tier's tests need no Prefect installed.

Two deployments, because a cycle is opened by an event and advanced by a loop, and conflating
them would make the loop a scheduler:

- `open_cycle_flow`  — fires on the process's own `opens:` cron. Creates one cycle. Nothing else.
- `advance_flow`     — fires often. Advances whatever exists. Creates nothing, ever.

That split is what makes a missed run harmless: `advance` is idempotent by construction (it is
a pure function of recorded milestones, so re-running it re-derives the same frontier), and a
missed `open` is visible as a month with no cycle rather than as a silently skipped invoice.

Deployed from `prefect.yaml` at the repo root.
"""

import logging
from datetime import UTC, datetime

from prefect import flow

from reconciler import app

logger = logging.getLogger("reconciler.flow")


def _wire_logging() -> None:
    """Let the tier's narration reach the Prefect run log.

    It is worth the two lines: which candidate matched, what the classifier read, and why a
    stage stopped are the whole diagnostic story, and without them a run that *decided* nothing
    had happened looks identical to one that never looked.

    Delivery is Prefect's own `PREFECT_LOGGING_EXTRA_LOGGERS=reconciler` (set on the deployment
    in `prefect.yaml`) — an earlier version forwarded records by hand through `get_run_logger`,
    which worked but emitted everything twice, once through the bridge and once through the
    handler Prefect had already attached. All that is actually missing is the level: the
    `reconciler` logger is never configured by this package (a library must not call
    `basicConfig`), so it inherits WARNING and its INFO records die before any handler runs.
    """
    logging.getLogger("reconciler").setLevel(logging.INFO)


@flow(name="open-process-cycle")
def open_cycle_flow(process: str, seq: int | None = None, period: dict | None = None,
                    year: int | None = None, user: str | None = None,
                    use_previous_month: bool = True, dry_run: bool = False) -> str:
    """Open one cycle. Returns its key.

    `seq` is omitted by the schedule and resolved from the cycles already recorded — see
    `app.next_sequence`. A human opening one by hand passes it, because the number is often the
    thing they care about.

    `use_previous_month` fills the worked period from the calendar month that just ended, which
    is what a cron firing on the 1st means. A bonus invoice — no worked days at all — is opened
    manually with it off.
    """
    _wire_logging()
    if period is None and use_previous_month:
        period = app.previous_month(datetime.now(UTC).date())

    cycle = app.open_cycle(process, seq=seq, period=period, year=year, user=user,
                           options=app.RunOptions(dry_run=dry_run))
    return cycle.key


@flow(name="advance-process-cycles")
def advance_flow(process: str, cycle_key: str | None = None, dry_run: bool = False) -> dict:
    """Advance every cycle of a process as far as it will go.

    Returns a per-cycle summary rather than nothing, so the Prefect UI's result view answers
    "what moved?" without opening the logs.

    **Does not fail when a cycle stops.** A stage waiting on a human, or on machinery that does
    not exist yet, is the normal resting state of a long-running process — failing the run
    would turn every ordinary Tuesday into an alert, and then nobody would read the alerts. A
    genuine fault (an unreachable relay, a bad credential) still raises out of `app` and fails
    the run, which is the distinction worth alerting on.
    """
    _wire_logging()
    results = app.advance(process, cycle_key=cycle_key,
                          options=app.RunOptions(dry_run=dry_run))

    summary = {}
    for cycle, outcome in results:
        summary[cycle.key] = {
            "status": outcome.status.value if outcome else "stopped",
            "advanced": list(outcome.advanced) if outcome else [],
            "waiting_on": list(outcome.waiting_on) if outcome else [],
        }
    logger.info("advanced %d cycle(s) of %s", len(summary), process)
    return summary


@flow(name="open-then-advance")
def open_and_advance_flow(process: str, **kwargs) -> dict:
    """Convenience for a manual run: open a cycle and immediately walk it.

    Not scheduled. It exists because opening a cycle and then waiting an hour to see whether
    the approval mail rendered correctly is a poor feedback loop for the *first* cycle of a new
    process — which is exactly when you most want to look.
    """
    key = open_cycle_flow(process, **kwargs)
    return advance_flow(process, cycle_key=key, dry_run=kwargs.get("dry_run", False))


if __name__ == "__main__":
    # Local smoke run against the ephemeral server Prefect starts on its own — proves the
    # deployment's entrypoint resolves and the flow executes, without touching Prefect Cloud.
    import sys

    print(advance_flow(sys.argv[1] if len(sys.argv) > 1 else "dreamhost_invoice",
                       dry_run=True))
