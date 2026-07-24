"""Score a trip tuning on QUALITY, not event counts — the adjudicator for backtest.py's deltas.

`backtest.py` answers "what changed?"; it can't tell you whether the change was good. This does,
by replaying the same real history (same `Router`, same production ordering) and scoring the
derived `got_into`/`got_out` pairs against two things a weight change must not trade away:

  real_trips      paired spans >= 2 min — trips that plausibly happened.
  junk_trips      paired spans < 2 min. A 0-110s "car trip" is never real, so this is a
                  count of phantom pairs. LOWER is better.
  drives_missed   CarPlay drive sessions (connect -> disconnect >= 3 min) with no got_into near
                  the start or no got_out near the end. This is the *independent* check — CarPlay
                  is not in the phantom path (the charger and the ambiguous lock/door are), so it
                  catches a tuning that buys precision by dropping real trips. LOWER is better.

Pairing mirrors `session_window` exactly (latch the start, the next end closes it, an end with no
open start is dropped, a pairing older than max_duration is dropped) so the numbers are the trips
`car_trip` would actually emit.

Usage (same env/DSN as backtest.py):
  NEON_DATABASE_URL=... uv run python scripts/trip_eval.py --days 25
  NEON_DATABASE_URL=... uv run python scripts/trip_eval.py --days 25 -v \
      scripts/backtest_candidates/into_ambiguous_demotion.yml
"""
from __future__ import annotations

import argparse
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from backtest import DictState, apply_candidate, external_inputs, fetch_signals
from dotenv import find_dotenv, load_dotenv

from inference.runtime.core import Router, RoutingPlan
from inference.runtime.definition import load_definitions

START, END = "got_into_the_car", "got_out_the_car"
MAX_DURATION = 21600   # session_window's default max_duration_seconds
JUNK_UNDER = 120       # a paired span shorter than this did not happen
DRIVE_MIN = 180        # a CarPlay session at least this long counts as a real drive
NEAR = 900             # how close a boundary must be to a drive edge to count as detected


def _fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%m-%d %H:%M:%S")


def replay(defs, signals: list[dict]) -> list[tuple[str, int, list[str]]]:
    """-> (name, event-time, contributor names) per derived event, in emission order.

    `route` emits `{message: envelope, sources: [...]}` — the `Shaper` is what projects sources
    into `derived_from`, so contributor names are read off `sources` here.
    """
    router, state, out = Router(RoutingPlan.from_definitions(defs)), DictState(), []
    for ev in signals:
        for item in router.route(ev, state):
            msg = item["message"]
            out.append((msg["name"], int(msg["timestamp"]),
                        [(s.get("message") or {}).get("name") for s in item.get("sources", [])]))
    return out


def carplay_drives(signals: list[dict]) -> list[tuple[int, int]]:
    """Independent drive proxy: CarPlay connect -> next disconnect, at least DRIVE_MIN apart."""
    out, opened = [], None
    for s in signals:
        name, ts = s["message"]["name"], s["message"]["timestamp"]
        if name == "device_connected_to_carplay" and opened is None:
            opened = ts                                    # earliest of a flap burst
        elif name == "device_disconnected_from_carplay" and opened is not None:
            if ts - opened >= DRIVE_MIN:
                out.append((opened, ts))
            opened = None
    return out


def spans(derived) -> list[tuple[int, int]]:
    """The trips `car_trip` would emit — session_window's pairing, replicated."""
    out, opened = [], None
    boundaries = sorted(((n, t) for n, t, _ in derived if n in (START, END)), key=lambda x: x[1])
    for name, ts in boundaries:
        if name == START:
            opened = ts
        elif opened is not None:
            if ts - opened <= MAX_DURATION:
                out.append((opened, ts))
            opened = None                                  # the end consumes the start either way
    return out


def score(label: str, defs, signals, drives, verbose: bool) -> None:
    derived = replay(defs, signals)
    paired = spans(derived)
    junk = [s for s in paired if s[1] - s[0] < JUNK_UNDER]
    real = [s for s in paired if s[1] - s[0] >= JUNK_UNDER]
    starts = [t for n, t, _ in derived if n == START]
    ends = [t for n, t, _ in derived if n == END]
    missed = [d for d in drives
              if not any(abs(t - d[0]) <= NEAR for t in starts)
              or not any(abs(t - d[1]) <= NEAR for t in ends)]
    print(f"{label:<24} {START}={len(starts):3d} {END}={len(ends):3d} | "
          f"real_trips={len(real):3d} junk_trips={len(junk):3d} | "
          f"drives_missed={len(missed):2d}/{len(drives)}")
    if not verbose:
        return
    for a, b in missed:
        print(f"      MISSED drive {_fmt(a)}Z -> {_fmt(b)}Z ({(b - a) // 60}min)")
    for a, b in junk:
        print(f"      junk trip    {_fmt(a)}Z ({b - a}s)")
    for name in (START, END):
        print(f"      {name} lineage signatures:")
        for sig, n in Counter(tuple(sorted(c)) for e, _, c in derived if e == name).most_common():
            print(f"        {n:3d}  {' + '.join(sig)}")


def main():
    if _p := find_dotenv(usecwd=True, raise_error_if_not_found=False):
        load_dotenv(_p)
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("candidates", nargs="*", help="candidate YAMLs to score alongside current")
    ap.add_argument("--days", type=int, default=25)
    ap.add_argument("--user", default="rods")
    ap.add_argument("--events-dir", default=os.environ.get("EVENTS_DIR", "events"))
    ap.add_argument("-v", "--verbose", action="store_true", help="list every junk trip / miss")
    args = ap.parse_args()

    dsn = os.environ.get("NEON_DATABASE_URL")
    if not dsn:
        raise SystemExit("set NEON_DATABASE_URL")

    events_dir = Path(args.events_dir)
    base = load_definitions(events_dir)
    cands = [(Path(c).stem, apply_candidate(load_definitions(events_dir), Path(c)))
             for c in args.candidates]
    # union of inputs, so a candidate that newly consumes a signal still gets fed it
    inputs = external_inputs(base)
    for _, defs in cands:
        inputs |= external_inputs(defs)
    signals = fetch_signals(dsn, args.user, args.days, inputs)
    drives = carplay_drives(signals)
    print(f"{args.days}d, {len(signals)} signals, {len(drives)} CarPlay drives "
          f"(>={DRIVE_MIN}s) — user={args.user}\n")

    score("current", base, signals, drives, args.verbose)
    for label, defs in cands:
        score(label, defs, signals, drives, args.verbose)


if __name__ == "__main__":
    main()
