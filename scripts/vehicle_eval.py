"""Score the `vehicle` capability on `trip` events — the adjudicator for corroboration changes (#46).

`trip_eval.py` judges `car_trip`'s boundary pairing; it never looks at `trip` or its
capabilities. This does: it replays raw history through the *actual* Router + Shaper (the same
code path that derives `vehicle` in production, minus Kafka) and scores each emitted `trip`
against an independent own-car proxy, so a corroboration rule change is adjudicable before it
ships.

The proxy is a CarPlay drive session (connect -> disconnect >= 3 min) overlapping the trip's
span — independent because CarPlay sessions are raw signals, not derived boundaries (`got_into`
is CarPlay-anchored since #39, so scoring against derived boundaries would be circular on the
entry side; the *session* is the phone's own record of being docked in the car). Caveats, both
deliberate:

  - ~7% of own-car entries historically lacked CarPlay, so the "no CarPlay" bucket may hold a
    few real own-car trips. It is therefore reported as `uncorroborated`, not "borrowed" —
    anything in it that GAINS vehicle evidence under a candidate needs manual adjudication
    (verbose lists every trip), exactly the borrowed-car phantom check #46 requires.
  - Recall is measured on `confirmed` (both distinct boundary names present), the bar the
    dashboard's car icon needs, with evidence-present counted separately: #46's week-scale data
    shows one-sided evidence is common, and a candidate that converts absent -> one-sided is
    progress the confirmed number alone would hide.

Per-edge offsets (nearest derived boundary vs each span edge, sign = boundary - edge) are
printed so a candidate's effect is visible per mechanism — cold-start starts vs parking-search
ends fail differently (#46).

Usage (same env/DSN as backtest.py; run from inside workers/ so workers/.env is found):
  NEON_DATABASE_URL=... uv run python scripts/vehicle_eval.py --days 25 [-v] [<cand.yml> ...]
"""
from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

from backtest import DictState, apply_candidate, external_inputs, fetch_signals
from dotenv import find_dotenv, load_dotenv
from trip_eval import carplay_drives

from inference.capabilities import set_place_book
from inference.runtime.core import Router, RoutingPlan, Shaper
from inference.runtime.definition import load_definitions

START, END = "got_into_the_car", "got_out_the_car"
TRIP = "trip"
OVERLAP_PAD = 120   # CarPlay session vs trip-span overlap slack: the session brackets the
                    # driving while the span is settled-fix to settled-fix, so the two can
                    # miss each other by up to ~a minute at each edge on a real own-car trip.
OFFSET_HORIZON = 600  # how far from a span edge to still report a boundary as "nearest"


def _fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%m-%d %H:%M")


def replay(defs, signals: list[dict]) -> list[dict]:
    """Shaped `message` bodies for every derived event, in emission order — Router for
    detection AND Shaper for capabilities, because `vehicle` is minted by the Shaper."""
    plan = RoutingPlan.from_definitions(defs)
    router, shaper, state = Router(plan), Shaper(plan), DictState()
    return [shaper.shape(item)["message"]
            for ev in signals for item in router.route(ev, state)]


def _nearest(ts_list: list[int], edge: int) -> int | None:
    """Signed offset (boundary - edge) of the nearest boundary within OFFSET_HORIZON."""
    near = [t - edge for t in ts_list if abs(t - edge) <= OFFSET_HORIZON]
    return min(near, key=abs) if near else None


def _judge(trips, intos, outs, drives) -> list[dict]:
    out = []
    for m in trips:
        s, e = int(m["interval"]["started_at"]), int(m["interval"]["ended_at"])
        vehicle = m.get("vehicle")
        journey = m.get("journey") or {}
        out.append({
            "start": s, "end": e,
            "own": any(a <= e + OVERLAP_PAD and b >= s - OVERLAP_PAD for a, b in drives),
            "evidence": tuple((vehicle or {}).get("evidence") or ()),
            "confirmed": bool((vehicle or {}).get("confirmed")),
            "off_in": _nearest(intos, s),
            "off_out": _nearest(outs, e),
            "mode": journey.get("mode"),
            "route": " -> ".join(
                (journey.get(k) or {}).get("label") or "?" for k in ("origin", "destination")),
        })
    return out


def score(label: str, defs, signals, drives, verbose: bool) -> None:
    derived = replay(defs, signals)
    intos = sorted(int(m["timestamp"]) for m in derived if m["name"] == START)
    outs = sorted(int(m["timestamp"]) for m in derived if m["name"] == END)
    judged = _judge([m for m in derived if m["name"] == TRIP], intos, outs, drives)

    own = [t for t in judged if t["own"]]
    rest = [t for t in judged if not t["own"]]
    confirmed = [t for t in own if t["confirmed"]]
    one_sided = [t for t in own if t["evidence"] and not t["confirmed"]]
    leaked = [t for t in rest if t["evidence"]]
    off_in = [t["off_in"] for t in own if t["off_in"] is not None]
    off_out = [t["off_out"] for t in own if t["off_out"] is not None]

    print(f"{label:<24} trips={len(judged):3d} (own-car {len(own)}, uncorroborated {len(rest)}) | "
          f"own: confirmed={len(confirmed)} one-sided={len(one_sided)} absent="
          f"{len(own) - len(confirmed) - len(one_sided)} | "
          f"uncorroborated gaining evidence={len(leaked)} "
          f"(confirmed={sum(1 for t in leaked if t['confirmed'])})")
    if off_in or off_out:
        print(f"      own-car edge offsets: got_into vs start median "
              f"{median(off_in):+.0f}s (n={len(off_in)}), got_out vs end median "
              f"{median(off_out):+.0f}s (n={len(off_out)})")
    if not verbose:
        return
    for t in sorted(judged, key=lambda t: t["start"]):
        vehicle = ("CONFIRMED" if t["confirmed"]
                   else "+".join(t["evidence"]) if t["evidence"] else "-")
        offs = ", ".join(f"{k} {v:+d}s" if v is not None else f"{k} none"
                         for k, v in (("in", t["off_in"]), ("out", t["off_out"])))
        print(f"      {_fmt(t['start'])}Z {(t['end'] - t['start']) // 60:3d}min "
              f"{'own ' if t['own'] else '    '} {t['mode'] or '?':8s} "
              f"{t['route']:<30} [{offs}]  vehicle: {vehicle}")


def main():
    if _p := find_dotenv(usecwd=True, raise_error_if_not_found=False):
        load_dotenv(_p)
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("candidates", nargs="*", help="candidate YAMLs to score alongside current")
    ap.add_argument("--days", type=int, default=25)
    ap.add_argument("--user", default="rods")
    ap.add_argument("--events-dir", default=os.environ.get("EVENTS_DIR", "events"))
    ap.add_argument("-v", "--verbose", action="store_true", help="list every trip")
    args = ap.parse_args()

    dsn = os.environ.get("NEON_DATABASE_URL")
    if not dsn:
        raise SystemExit("set NEON_DATABASE_URL")

    events_dir = Path(args.events_dir)
    base = load_definitions(events_dir)
    cands = [(Path(c).stem, apply_candidate(load_definitions(events_dir), Path(c)))
             for c in args.candidates]

    # Journey labels in the verbose listing come from the same place book production uses;
    # best-effort — an empty book only blanks the labels, never the verdicts.
    from inference.runtime.places import load_places
    set_place_book(load_places(dsn))

    inputs = external_inputs(base)
    for _, defs in cands:
        inputs |= external_inputs(defs)
    signals = fetch_signals(dsn, args.user, args.days, inputs)
    drives = carplay_drives(signals)
    print(f"{args.days}d, {len(signals)} signals, {len(drives)} CarPlay drives — "
          f"user={args.user}\n")

    score("current", base, signals, drives, args.verbose)
    for label, defs in cands:
        score(label, defs, signals, drives, args.verbose)


if __name__ == "__main__":
    main()
