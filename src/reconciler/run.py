"""Command-line entry point for the reconciler (ADR 0012).

Deliberately built before the Prefect entry point. A reconciler run is a plain function of
recorded events, so it must be runnable by hand — that is what makes the first cycle of a new
process reviewable, and what keeps the runner swappable (`flow.py` will call the same
`reconcile_once`, and nothing else in the tier will know Prefect exists).

    # See exactly what July's approval mail would say — writes nothing, sends nothing
    python -m reconciler.run open --process dreamhost_invoice --seq 7 \\
        --period 2026-07-01:2026-07-31 --dry-run

    # Open the cycle for real, then advance it
    python -m reconciler.run open --process dreamhost_invoice --seq 7 \\
        --period 2026-07-01:2026-07-31
    python -m reconciler.run reconcile --process dreamhost_invoice
"""

import argparse
import logging
import os
import pathlib
import sys
from datetime import UTC, date, datetime

from dotenv import find_dotenv, load_dotenv

from reconciler.actions import Services
from reconciler.adapters.gateway import DryRunMilestones, GatewayMilestones
from reconciler.adapters.mail import (
    ConsoleMailer,
    FileMailer,
    N8nRelayMailer,
    SmtpMailer,
)
from reconciler.adapters.neon import NeonMilestones
from reconciler.core import Cycle, Milestone, reconcile
from reconciler.adapters.craftmypdf import CraftMyPdf
from reconciler.adapters.gmail import N8nGmailQuery
from reconciler.adapters.llm import N8nGeminiRelay
from reconciler.finder import SignalFinder
from reconciler.definition import GENESIS_STAGE, ProcessDefinition, load_definitions
from reconciler.world import NotYetImplemented, RealWorld

logger = logging.getLogger("reconciler.run")

DEFAULT_PROCESSES_DIR = pathlib.Path(__file__).resolve().parents[2] / "processes"


def _definition(name: str, processes_dir: pathlib.Path) -> ProcessDefinition:
    for definition in load_definitions(processes_dir):
        if definition.name == name:
            return definition
    raise SystemExit(f"no enabled process named {name!r} in {processes_dir}")


def _mailer(args):
    """Pick a transport: file > console(dry-run) > n8n relay > SMTP.

    The relay is the normal path — it keeps the SMTP credential in n8n's store rather than in
    this repo. Direct SMTP stays available for local-only testing, and is only reached when no
    relay is configured.
    """
    if args.mail_to_file:
        return FileMailer(pathlib.Path(args.mail_to_file))
    if args.dry_run:
        return ConsoleMailer()
    if os.environ.get("MAIL_RELAY_URL"):
        return N8nRelayMailer(
            url=os.environ["MAIL_RELAY_URL"],
            token=os.environ["MAIL_RELAY_TOKEN"],
            header=os.environ.get("MAIL_RELAY_HEADER", "X-Relay-Token"),
            recipient=os.environ["MAIL_TO"],
            sender=os.environ.get("MAIL_FROM"),
        )
    if os.environ.get("SMTP_HOST"):
        return SmtpMailer(
            host=os.environ["SMTP_HOST"],
            port=int(os.environ.get("SMTP_PORT", 587)),
            username=os.environ["SMTP_USERNAME"],
            password=os.environ["SMTP_PASSWORD"],
            sender=os.environ["SMTP_SENDER"],
            recipient=os.environ["MAIL_TO"],
        )
    return ConsoleMailer()


def _services(args) -> Services:
    # `extras` stays unwired: manual lines are collected after approval, which this
    # increment does not reach, and ADR 0012 open question 8 has not been settled.
    key = os.environ.get("CRAFTMYPDF_API_KEY")
    return Services(mailer=_mailer(args),
                    pdf=CraftMyPdf(api_key=key) if key else None)


def _sink(args, definition: ProcessDefinition):
    if args.dry_run:
        return DryRunMilestones(definition)
    base = os.environ.get("VECTOR_BASE_URL")
    if not base:
        raise SystemExit("VECTOR_BASE_URL is not set (run with --dry-run to preview)")
    return GatewayMilestones(base, definition)


def _neon() -> NeonMilestones:
    dsn = os.environ.get("NEON_DATABASE_URL")
    if not dsn:
        raise SystemExit("NEON_DATABASE_URL is not set")
    return NeonMilestones(dsn)


def cmd_open(args) -> int:
    """Record `cycle_opened` — the genesis fact a cycle exists because of."""
    definition = _definition(args.process, args.processes_dir)

    context: dict = {"invoice_number": args.seq}
    if args.period:
        start, _, end = args.period.partition(":")
        date.fromisoformat(start), date.fromisoformat(end)      # validate, fail loudly
        context["worked_period"] = {"start": start, "end": end}

    year = args.year or datetime.now(UTC).year
    cycle_key = definition.cycle_key.format(year=year, seq=args.seq)
    cycle = Cycle(key=cycle_key, process=definition.name, user_id=args.user,
                  opened_at=int(datetime.now(UTC).timestamp()), context=context)

    sink = _sink(args, definition)
    sink.record(cycle, GENESIS_STAGE, context)

    if args.dry_run:
        # A dry-run open is only useful if it also shows what would happen next, so walk the
        # cycle immediately against an empty milestone set.
        status = _advance(definition, cycle, {}, args, sink)
        print(f"\ndry run — nothing written, nothing sent. Cycle would be {cycle_key}.")
        return status

    print(f"opened cycle {cycle_key}; run `reconcile` to advance it")
    return 0


def cmd_reconcile(args) -> int:
    """Advance every cycle of a process as far as it will go."""
    definition = _definition(args.process, args.processes_dir)
    neon = _neon()
    sink = _sink(args, definition)

    cycles = neon.cycles(definition)
    if args.cycle:
        cycles = [c for c in cycles if c.key == args.cycle]
        if not cycles:
            raise SystemExit(f"no cycle {args.cycle!r} for process {definition.name}")
    if not cycles:
        print(f"no cycles open for {definition.name}")
        return 0

    worst = 0
    for cycle in cycles:
        milestones = neon.milestones(definition, cycle)
        worst = max(worst, _advance(definition, cycle, milestones, args, sink))
    return worst


def _finders(args) -> dict:
    """Finders by signal `source`.

    Both read the same Gmail mailbox through the same n8n question — asked at decision time, so
    every loop stays on this side and an unreachable n8n raises instead of looking like
    "nothing labelled yet". They differ only in what settles the match:

    - `gmail`   — a label somebody applied. The label IS the decision; nothing to interpret.
    - `classify` — the same candidates, plus a reading, for the one distinction a label cannot
      carry (a payment *sent* vs the same payment *completed*).

    An unwired `classify` is left absent rather than degraded to `gmail`: `world.find` then
    raises `NotYetImplemented` naming the source, which is the loud stop. Silently falling back
    would match the first Tipalti mail mentioning the invoice and record the wrong step.
    """
    url = os.environ.get("GMAIL_QUERY_URL")
    if not url:
        return {}
    query = N8nGmailQuery(
        url=url,
        token=os.environ["MAIL_RELAY_TOKEN"],
        header=os.environ.get("MAIL_RELAY_HEADER", "X-Relay-Token"),
    )
    finders = {"gmail": SignalFinder(query)}

    if os.environ.get("LLM_RELAY_URL"):
        finders["classify"] = SignalFinder(query, classifier=N8nGeminiRelay(
            url=os.environ["LLM_RELAY_URL"],
            token=os.environ["MAIL_RELAY_TOKEN"],
            header=os.environ.get("MAIL_RELAY_HEADER", "X-Relay-Token"),
        ))
    else:
        logger.warning("LLM_RELAY_URL is not set; `classify` stages will stop rather than "
                       "advance (see connectors/n8n/llm-relay.workflow.ts)")
    return finders


def _advance(definition, cycle: Cycle, milestones: dict[str, Milestone], args, sink) -> int:
    world = RealWorld(definition, sink=sink, services=_services(args),
                      finders=_finders(args))
    try:
        outcome = reconcile(definition, cycle, milestones, world)
    except NotYetImplemented as e:
        # Everything completed before this point was already recorded — the emit happens
        # before the next stage is attempted, which is the same property that makes a
        # crashed run resumable. So report and stop rather than pretending it failed.
        print(f"\n{cycle.key}: stopped — {e}")
        print("  (expected: this is as far as the tier reaches today)")
        return 0

    print(f"\n{cycle.key}: {outcome.status.value}")
    if outcome.advanced:
        print(f"  advanced: {', '.join(outcome.advanced)}")
    if outcome.waiting_on:
        print(f"  waiting on: {', '.join(outcome.waiting_on)}")
    # A voided cycle is a normal terminal state, not an error; nothing here is a failure.
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="reconciler.run", description=__doc__)
    parser.add_argument("--processes-dir", type=pathlib.Path,
                        default=DEFAULT_PROCESSES_DIR)
    parser.add_argument("--dry-run", action="store_true",
                        help="write no events and send no mail; print what would happen")
    parser.add_argument("--mail-to-file", metavar="PATH",
                        help="write the rendered HTML mail here instead of sending it")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    opener = sub.add_parser("open", help="open a new cycle")
    opener.add_argument("--process", required=True)
    opener.add_argument("--seq", type=int, required=True,
                        help="per-year invoice sequence number")
    opener.add_argument("--year", type=int)
    opener.add_argument("--period", metavar="START:END",
                        help="worked period, e.g. 2026-07-01:2026-07-31; omit for a "
                             "manual-lines-only invoice such as a bonus")
    opener.add_argument("--user", default=os.environ.get("AWARE_USER_ID", "rods"))
    opener.set_defaults(func=cmd_open)

    runner = sub.add_parser("reconcile", help="advance existing cycles")
    runner.add_argument("--process", required=True)
    runner.add_argument("--cycle", help="restrict to one cycle key")
    runner.set_defaults(func=cmd_reconcile)

    args = parser.parse_args(argv)
    # Same convention as the Quix entrypoint: walk upward from the CWD for a .env, so
    # credentials live in `workers/.env` and never in the repo. In a deployed runner
    # find_dotenv returns "" and the environment already carries the values.
    load_dotenv(find_dotenv(usecwd=True))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
