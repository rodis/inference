"""Backtest engine definitions against real signal history — see what a change does before deploying.

Replays raw signals recorded in Neon through the *actual* transport-agnostic core
(`inference.runtime.core.Router`) — the same code path production runs, minus Kafka — so you
can change a weight/threshold/engine in `events/*.yml` and measure the exact behavioral delta
against weeks of real data in seconds, instead of "deploy and watch for a week".

Two things make this trustworthy (the naive replay got both wrong):

  1. ORDER BY ingested_at, not occurred_at. Production processes events in Kafka-arrival order;
     window/cooldown/dedup state evolves in that order. We use `ingested_at` for ordering and
     `occurred_at` (message.timestamp) for the engines' time math — matching production.

  2. CONFIG-DELTA, not history. Comparing to historically-recorded derived events is invalid —
     they were produced by *evolving* configs. Instead we replay TWO definition sets over the
     SAME input and diff their outputs. You adjudicate the handful that changed; no ground-truth
     labels needed to make a tuning decision.

Usage:
  # List what the current events/ definitions derive over the last 14 days for user 'rods':
  NEON_DATABASE_URL=... uv run python scripts/backtest.py --days 14 --user rods

  # Diff current vs a candidate (candidate = YAML of {definition_name: {engine_config overrides}}):
  NEON_DATABASE_URL=... uv run python scripts/backtest.py --days 14 --user rods \
      --candidate scripts/backtest_candidates/door_fusion.yml --focus car_trip
"""
from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import yaml
from dotenv import find_dotenv, load_dotenv

from inference.runtime.core import Router, RoutingPlan
from inference.runtime.definition import load_definitions


class DictState:
    """In-memory StateStore (get/set) — the per-entity state port the core is built against."""

    def __init__(self):
        self._d: dict = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


def fetch_signals(dsn: str, user: str, days: int, input_names: set[str]) -> list[dict]:
    """Raw external signals for `user` over the window, as event dicts in PRODUCTION order.

    Ordered by ingested_at (Kafka-arrival ≈ how the runtime saw them); message.timestamp is
    the event-time (occurred_at) the engines do window/cooldown math on. Only names the loaded
    definitions actually consume externally are fetched.

    The FULL persisted `message` body is carried through, not a reconstructed {id,name,
    timestamp} stub. The windowed engines only need names and times, but the geometry engines
    (`geofence`, `stay_window`) read `lat`/`lon`/`acc` — with a stub they silently derive
    nothing, which reads as "the engine doesn't fire" rather than "the replay starved it".
    Canonical fields are overlaid from the columns so a body missing/disagreeing on them still
    replays with the same identity and event-time production used.
    """
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            """
            SELECT name, EXTRACT(EPOCH FROM occurred_at)::bigint AS ts, id::text, message,
                   source_app
            FROM events
            WHERE user_id = %s
              AND occurred_at > now() - make_interval(days => %s)
              AND name = ANY(%s)
            -- Tie-break by EVENT time, not by id. A batched producer (Overland posts up to
            -- 1000 fixes per request) persists a whole batch with near-identical ingested_at,
            -- and a uuid tiebreak then shuffles the batch's internal order at random. The
            -- geometry engines are sequential — `stay_window` skips a fix older than its
            -- cluster's end — so a shuffled batch silently loses about HALF its fixes
            -- (measured: 30 replayed vs 57 live for one stay). Arrival order still governs
            -- ACROSS batches, which is what production ordering means.
            ORDER BY ingested_at, (message->>'timestamp')::bigint, id
            """,
            (user, days, list(input_names)),
        ).fetchall()
    # `source_app` is carried through from the column, not stubbed. It is envelope metadata the
    # engines mostly ignore, but `ssid_edge` gates on it (two producers emit `location_ping` and
    # only one reports the WiFi field), and a stubbed value would silently filter every ping out
    # of the replay — an engine that "never fires" rather than a visible error.
    return [
        {"name": n, "source_app": app, "source_type": "http_server",
         "message": {**(msg or {}), "id": i, "name": n, "user_id": user, "timestamp": ts}}
        for (n, ts, i, msg, app) in rows
    ]


def external_inputs(defs) -> set[str]:
    """Names the definitions consume from OUTSIDE (consumed minus produced) — what to feed."""
    plan = RoutingPlan.from_definitions(defs)
    produced = set(plan.sink_for)
    return set(plan.consumers) - produced


def apply_candidate(defs, candidate_path: Path):
    """Return a copy of `defs` with each named definition's engine_config keys overridden."""
    patch = yaml.safe_load(candidate_path.read_text()) or {}
    out = []
    for d in defs:
        if d.name in patch:
            d = d.model_copy(update={"engine_config": {**d.engine_config, **patch[d.name]}})
        out.append(d)
    return out


def replay(defs, signals: list[dict]) -> list[tuple[str, int]]:
    """Feed signals through the real Router; return derived (name, timestamp) in emission order."""
    router = Router(RoutingPlan.from_definitions(defs))
    state = DictState()
    out = []
    for ev in signals:
        for item in router.route(ev, state):
            out.append((item["message"]["name"], int(item["message"]["timestamp"])))
    return out


def _fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%m-%d %H:%M")


def summarize(label: str, derived: list[tuple[str, int]], focus: str | None):
    from collections import Counter
    counts = Counter(n for n, _ in derived)
    print(f"\n[{label}] derived events: {sum(counts.values())}")
    for name, n in counts.most_common():
        mark = "  <-- focus" if name == focus else ""
        print(f"    {n:4d}  {name}{mark}")


def diff(a: list[tuple[str, int]], b: list[tuple[str, int]], focus: str | None, tol: int = 120):
    """Match a↔b by (name, timestamp within tol); report only what changed."""
    names = {focus} if focus else {n for n, _ in a} | {n for n, _ in b}
    print(f"\n=== DELTA (candidate vs current){' — focus: ' + focus if focus else ''} ===")
    any_change = False
    for name in sorted(names):
        aa = sorted(t for n, t in a if n == name)
        bb = sorted(t for n, t in b if n == name)
        used = [False] * len(bb)
        removed = []
        for t in aa:
            m = next((j for j, u in enumerate(used) if not u and abs(bb[j] - t) <= tol), None)
            if m is None:
                removed.append(t)
            else:
                used[m] = True
        added = [bb[j] for j, u in enumerate(used) if not u]
        if removed or added:
            any_change = True
            print(f"  {name}: current={len(aa)} candidate={len(bb)}  "
                  f"(+{len(added)} new, -{len(removed)} lost)")
            for t in added:
                print(f"      + candidate ADDS  @ {_fmt(t)}Z")
            for t in removed:
                print(f"      - candidate LOSES @ {_fmt(t)}Z")
    if not any_change:
        print("  (no change)")


def main():
    if _p := find_dotenv(usecwd=True, raise_error_if_not_found=False):
        load_dotenv(_p)  # NEON_DATABASE_URL locally (workers/.env); env-provided in CI/K8s
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--user", default="rods")
    ap.add_argument("--events-dir", default=os.environ.get("EVENTS_DIR", "events"))
    ap.add_argument("--candidate", help="YAML of {definition_name: {engine_config overrides}} to diff against current")
    ap.add_argument("--focus", help="only diff/highlight this derived event name (e.g. car_trip)")
    args = ap.parse_args()

    dsn = os.environ.get("NEON_DATABASE_URL")
    if not dsn:
        raise SystemExit("set NEON_DATABASE_URL")

    base = load_definitions(Path(args.events_dir))
    cand_defs = apply_candidate(base, Path(args.candidate)) if args.candidate else None

    # Feed the UNION of base + candidate external inputs — else a signal the candidate newly
    # consumes (e.g. car_driver_door_opened) would never be fetched and the delta would be blank.
    inputs = external_inputs(base) | (external_inputs(cand_defs) if cand_defs else set())
    signals = fetch_signals(dsn, args.user, args.days, inputs)
    print(f"replaying {len(signals)} signals ({args.days}d, user={args.user}) "
          f"through {len(base)} definitions; external inputs: {sorted(inputs)}")

    current = replay(base, signals)
    summarize("current", current, args.focus)

    if cand_defs:
        cand = replay(cand_defs, signals)
        summarize("candidate", cand, args.focus)
        diff(current, cand, args.focus)


if __name__ == "__main__":
    main()
