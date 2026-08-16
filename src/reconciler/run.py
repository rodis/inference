"""Command-line entry point for the reconciler (ADR 0012).

Deliberately built before the Prefect entry point, and still the one to reach for. A reconciler
run is a plain function of recorded events, so it must be runnable by hand — that is what makes
the first cycle of a new process reviewable, and what keeps the runner swappable.

The wiring lives in `app.py`; this is argparse and printing. `flow.py` is the same shape for
Prefect, and neither knows the other exists.

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
from datetime import date

from reconciler.app import (
    DEFAULT_PROCESSES_DIR,
    ConfigurationError,
    RunOptions,
    advance,
    open_cycle,
    walk_fresh,
)

logger = logging.getLogger("reconciler.run")


def _options(args) -> RunOptions:
    return RunOptions(dry_run=args.dry_run, mail_to_file=args.mail_to_file,
                      processes_dir=args.processes_dir)


def _period(raw: str | None) -> dict | None:
    if not raw:
        return None
    start, _, end = raw.partition(":")
    date.fromisoformat(start), date.fromisoformat(end)      # validate, fail loudly
    return {"start": start, "end": end}


def cmd_open(args) -> int:
    options = _options(args)
    cycle = open_cycle(args.process, seq=args.seq, period=_period(args.period),
                       year=args.year, user=args.user, options=options)

    if args.dry_run:
        # A dry-run open is only useful if it also shows what would happen next, so walk the
        # cycle immediately against an empty milestone set — which also keeps the preview
        # database-free, since a cycle opened one line ago has nothing recorded to read.
        _report([(cycle, walk_fresh(args.process, cycle, options=options))])
        print(f"\ndry run — nothing written, nothing sent. Cycle would be {cycle.key}.")
        return 0

    print(f"opened cycle {cycle.key}; run `reconcile` to advance it")
    return 0


def cmd_reconcile(args) -> int:
    results = advance(args.process, cycle_key=args.cycle, options=_options(args))
    if not results:
        print(f"no cycles open for {args.process}")
        return 0
    _report(results)
    return 0


def _report(results) -> None:
    for cycle, outcome in results:
        if outcome is None:
            print(f"\n{cycle.key}: stopped — this is as far as the tier reaches today")
            continue
        print(f"\n{cycle.key}: {outcome.status.value}")
        if outcome.advanced:
            print(f"  advanced: {', '.join(outcome.advanced)}")
        if outcome.waiting_on:
            print(f"  waiting on: {', '.join(outcome.waiting_on)}")


def main(argv=None) -> int:
    # On the SUBcommands, not the top level: `run open --dry-run` is the order everyone
    # actually types, and argparse only accepts a top-level flag *before* the subcommand — so
    # the obvious invocation failed with "unrecognized arguments". Sharing one parent parser
    # puts them after the subcommand instead, where they read naturally.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--processes-dir", type=pathlib.Path, default=DEFAULT_PROCESSES_DIR)
    common.add_argument("--dry-run", action="store_true",
                        help="write no events and send no mail; print what would happen")
    common.add_argument("--mail-to-file", metavar="PATH",
                        help="write the rendered HTML mail here instead of sending it")
    common.add_argument("-v", "--verbose", action="store_true")

    parser = argparse.ArgumentParser(prog="reconciler.run", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    opener = sub.add_parser("open", help="open a new cycle", parents=[common])
    opener.add_argument("--process", required=True)
    opener.add_argument("--seq", type=int,
                        help="per-year sequence number; omit to take the next unused one")
    opener.add_argument("--year", type=int)
    opener.add_argument("--period", metavar="START:END",
                        help="worked period, e.g. 2026-07-01:2026-07-31; omit for a "
                             "manual-lines-only invoice such as a bonus")
    opener.add_argument("--user", default=os.environ.get("AWARE_USER_ID", "rods"))
    opener.set_defaults(func=cmd_open)

    runner = sub.add_parser("reconcile", help="advance existing cycles", parents=[common])
    runner.add_argument("--process", required=True)
    runner.add_argument("--cycle", help="restrict to one cycle key")
    runner.set_defaults(func=cmd_reconcile)

    args = parser.parse_args(argv)

    # Imported here rather than at module scope so this module stays importable with nothing
    # third-party installed — CI runs `pip install -e . --no-deps` plus only pytest, ruff,
    # pydantic and pyyaml, and a top-level `from dotenv import ...` breaks collection there
    # while passing locally. Same reasoning as `adapters/neon.py`'s psycopg import.
    from dotenv import find_dotenv, load_dotenv

    # Same convention as the Quix entrypoint: walk upward from the CWD for a .env, so
    # credentials live in `workers/.env` and never in the repo. In a deployed runner
    # find_dotenv returns "" and the environment already carries the values.
    load_dotenv(find_dotenv(usecwd=True))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        return args.func(args)
    except ConfigurationError as e:
        raise SystemExit(str(e)) from None


if __name__ == "__main__":
    sys.exit(main())
