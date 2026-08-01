"""Re-derive events from retained raw signals and (optionally) produce them to Kafka.

Derived events are a **cache**; the raw signals are the source of truth. So whenever a
definition changes — a retuned weight map, a new engine, a place that just got a name — the
history in Neon is stale and can be rebuilt from the raws that are still there. That is what
this does, and it is why keeping raw events matters more than the storage they cost.

Two uses it was built for, both real:
  - a new definition can't see the past (a stream processor derives forward only), so
    `stay` existed for hours with nothing to show for the day it could have described;
  - a `place` label is frozen when an event is minted, so naming a place only labels the
    future — re-deriving relabels what already happened.

SAFETY, because this writes to the production topic:
  - dry-run by DEFAULT; producing requires --produce;
  - `--only NAME` is REQUIRED, so you must name exactly which derived events to emit. Every
    definition fires during a replay, and most of those already exist in Neon from the live
    runtime — producing them all would duplicate history rather than repair it;
  - it prints every event it will produce, and refuses to run without a time window;
  - it REFUSES to produce into a window that already holds those events. Re-derivation mints
    fresh uuids, so a repeat inserts cleanly and silently doubles the history — nothing
    downstream rejects or even notices it. `--replace` deletes them (and their lineage) first.
    Note a LOCK would not have caught this: the likelier mistake is the same window rebuilt
    twice in SEQUENCE, which mutual exclusion permits. The lock (scripts/lock.sh `history`)
    is the second line, closing only the simultaneous case.

It replays through the real `Router`/`Shaper` over the real definitions (same code path as
production, minus Kafka), so what it emits is what the runtime would have emitted.

Usage:
  NEON_DATABASE_URL=... uv run python scripts/rederive.py --since '2026-07-25 00:00' --only stay
  ... same, plus --produce   # actually send to high_level_events
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from dotenv import find_dotenv, load_dotenv

from inference.capabilities import set_place_book
from inference.runtime import config
from inference.runtime.core import Router, RoutingPlan, Shaper
from inference.runtime.definition import load_definitions
from inference.runtime.places import load_places


class DictState:
    """In-memory StateStore — the replay gets fresh state, exactly as a new definition would."""

    def __init__(self):
        self._d: dict = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value) -> None:
        self._d[key] = value


def fetch_signals(dsn: str, user: str, since: str, until: str | None, names: set[str]) -> list[dict]:
    """Raw signals in PRODUCTION order (ingested_at), carrying the full persisted body."""
    sql = """
        SELECT name, EXTRACT(EPOCH FROM occurred_at)::bigint AS ts, id::text, message,
               source_app
        FROM events
        WHERE user_id = %s AND name = ANY(%s) AND occurred_at >= %s::timestamptz
          AND (%s::timestamptz IS NULL OR occurred_at < %s::timestamptz)
        -- Tie-break by EVENT time, not by id. A batched producer (Overland posts up to
        -- 1000 fixes per request) persists a whole batch with near-identical ingested_at,
        -- and a uuid tiebreak then shuffles the batch's internal order at random. The
        -- geometry engines are sequential — `stay_window` skips a fix older than its
        -- cluster's end — so a shuffled batch silently loses about HALF its fixes
        -- (measured: 30 replayed vs 57 live for one stay). Arrival order still governs
        -- ACROSS batches, which is what production ordering means.
        ORDER BY ingested_at, (message->>'timestamp')::bigint, id
    """
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(sql, (user, list(names), since, until, until)).fetchall()
    # `source_app` is carried through from the column, not stubbed. No engine reads it today,
    # but a stubbed value silently filters out every event an engine gates on it — surfacing as
    # "nothing to re-derive" rather than as an error, the worst possible failure for a repair
    # tool. (Cost one debugging session on `ssid_edge`, removed 2026-08-01.)
    return [
        {"name": n, "source_app": app, "source_type": "http_server",
         "message": {**(msg or {}), "id": i, "name": n, "user_id": user, "timestamp": ts}}
        for (n, ts, i, msg, app) in rows
    ]


def existing_counts(dsn: str, user: str, since: str, until: str | None,
                    names: set[str]) -> dict[str, int]:
    """How many of each `names` event already sit in the window. The idempotency pre-flight.

    Producing into a window that already holds these events DUPLICATES history rather than
    repairing it — and the duplicates are semantic, not key collisions, because every run mints
    fresh uuids. Nothing downstream notices: `events.id` is unique, so the rows insert cleanly
    and the timeline quietly gains a second copy of every stay.
    """
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT name, count(*) FROM events "
            "WHERE user_id = %s AND name = ANY(%s) AND occurred_at >= %s::timestamptz "
            "  AND (%s::timestamptz IS NULL OR occurred_at < %s::timestamptz) "
            "GROUP BY name",
            (user, sorted(names), since, until, until),
        ).fetchall()
    return dict(rows)


def delete_existing(dsn: str, user: str, since: str, until: str | None,
                    names: set[str]) -> tuple[int, int]:
    """Delete those events AND their lineage rows, returning (events, lineage_rows).

    Deleting DERIVED events is sound: invariant 19 makes them a cache over retained raws. The
    lineage half is not optional — dropping events while leaving `event_lineage` behind is how
    263 orphan rows accumulated (issue #25), and they are invisible until something joins on
    them.
    """
    with psycopg.connect(dsn) as conn, conn.transaction():
        args = (user, sorted(names), since, until, until)
        target = ("SELECT id FROM events WHERE user_id = %s AND name = ANY(%s) "
                  "AND occurred_at >= %s::timestamptz "
                  "AND (%s::timestamptz IS NULL OR occurred_at < %s::timestamptz)")
        lin = conn.execute(f"DELETE FROM event_lineage WHERE child_id IN ({target})", args).rowcount
        ev = conn.execute(f"DELETE FROM events WHERE id IN ({target})", args).rowcount
    return ev, lin


def main() -> None:
    if _p := find_dotenv(usecwd=True, raise_error_if_not_found=False):
        load_dotenv(_p)                      # NEON_DATABASE_URL + KAFKA_* for producing
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", required=True, help="inclusive start, e.g. '2026-07-25 00:00'")
    ap.add_argument("--until", help="exclusive end (default: now)")
    ap.add_argument("--only", required=True, action="append",
                    help="derived event name to emit (repeatable). Required — see SAFETY.")
    ap.add_argument("--user", default="rods")
    ap.add_argument("--events-dir", default=os.environ.get("EVENTS_DIR", "events"))
    ap.add_argument("--produce", action="store_true",
                    help="actually produce to the sink topic (default: dry run)")
    ap.add_argument("--replace", action="store_true",
                    help="delete the existing events (and their lineage) in the window first. "
                         "Without this, producing into a non-empty window is refused.")
    args = ap.parse_args()

    dsn = os.environ.get("NEON_DATABASE_URL")
    if not dsn:
        raise SystemExit("set NEON_DATABASE_URL")

    defs = load_definitions(Path(args.events_dir))
    plan = RoutingPlan.from_definitions(defs)
    wanted = set(args.only)
    if unknown := wanted - set(plan.sink_for):
        raise SystemExit(f"not produced by any definition: {sorted(unknown)}")

    # Labels come from the SAME reference data the runtime uses, so a re-derived event carries
    # what the runtime would carry today (which is the point when relabelling after naming).
    set_place_book(load_places(dsn))

    external = set(plan.consumers) - set(plan.sink_for)
    signals = fetch_signals(dsn, args.user, args.since, args.until, external)
    router, shaper, state = Router(plan), Shaper(plan), DictState()

    emit: list[dict] = []
    for ev in signals:
        for item in router.route(ev, state):
            if item["message"]["name"] in wanted:
                emit.append(shaper.shape(item))

    def _fmt(ts: int) -> str:
        return datetime.fromtimestamp(ts, UTC).strftime("%m-%d %H:%M")

    print(f"replayed {len(signals)} signals since {args.since} "
          f"({args.user}) -> {len(emit)} event(s) to emit: {sorted(wanted)}\n")
    for e in emit:
        m = e["message"]
        iv, pl = m.get("interval") or {}, m.get("place") or {}
        extra = ""
        if iv:
            extra += f" {iv['duration_seconds'] / 60:.1f}min"
        if pl:
            extra += f" @ {pl.get('label') or f'{pl['lat']:.5f},{pl['lon']:.5f}'}"
        print(f"  {m['name']:12s} {_fmt(m['timestamp'])}Z{extra}  id={m['id'][:8]}")

    if not args.produce:
        print("\nDRY RUN — nothing produced. Re-run with --produce to emit these.")
        return
    if not emit:
        print("\nnothing to produce")
        return

    # --- serialise history rewrites (second line of defence) -------------------------------
    # The pre-flight below closes the SEQUENTIAL hole: the same window rebuilt twice. This
    # closes the SIMULTANEOUS one, where two runs both pre-check, both see the same counts, and
    # both produce. Held only across the produce, and released in `finally` so a crash mid-run
    # cannot wedge every other agent (a 30-min TTL is the backstop if even that is skipped).
    lock_sh = str(Path(__file__).resolve().parent / "lock.sh")
    detail = f"rederive --only {','.join(sorted(wanted))} --since {args.since}"
    if subprocess.run([lock_sh, "history", "acquire", detail]).returncode != 0:
        raise SystemExit(1)                      # lock.sh already explained itself on stderr
    try:
        _produce(dsn, args, wanted, emit, plan)
    finally:
        subprocess.run([lock_sh, "history", "release"], stdout=subprocess.DEVNULL)


def _produce(dsn, args, wanted, emit, plan) -> None:
    # --- idempotency pre-flight ----------------------------------------------------------
    # A lock would stop two SIMULTANEOUS runs. It would not stop the likelier mistake: the same
    # window rebuilt twice in SEQUENCE. Since re-derivation mints fresh uuids there is no key to
    # collide on, so a repeat inserts cleanly and silently doubles the history. Refuse instead,
    # and make --replace the explicit way through.
    #
    # Stable ids would be the textbook fix and are WRONG here: Vector's postgres sink has no
    # ON CONFLICT and one violation fails a whole 500-event batch, so a colliding id would wedge
    # persistence rather than dedupe. The check has to happen before producing.
    present = existing_counts(dsn, args.user, args.since, args.until, wanted)
    if present:
        listing = ", ".join(f"{n}={c}" for n, c in sorted(present.items()))
        if not args.replace:
            raise SystemExit(
                f"\nrefusing to produce: the window already holds {listing}.\n"
                f"Producing now would DUPLICATE those, not repair them — every run mints fresh\n"
                f"uuids, so nothing downstream would reject or notice them.\n\n"
                f"  rebuild it:   add --replace   (deletes those events + their lineage first)\n"
                f"  narrow it:    move --since / --until off the existing range\n"
            )
        ev, lin = delete_existing(dsn, args.user, args.since, args.until, wanted)
        print(f"--replace: deleted {ev} event(s) and {lin} lineage row(s) [{listing}]")

    # Produce through the real sink so the persist lane (Vector -> Neon) handles them exactly
    # as it handles live derived events: same wrapper, same topic, DB-set ingested_at.
    from quixstreams import Application
    # mTLS goes in producer_extra_config (librdkafka keys), not as Application kwargs —
    # same wiring as inference.runtime.quix.build_runtime.
    app = Application(broker_address=config.kafka_bootstrap(), consumer_group="rederive-oneshot",
                      producer_extra_config=config.kafka_ssl())
    with app.get_producer() as producer:
        for e in emit:
            topic = plan.sink_for[e["message"]["name"]]
            producer.produce(topic=topic, key=e["message"]["user_id"].encode(),
                             value=json.dumps(e).encode())
    print(f"\nproduced {len(emit)} event(s) to {sorted({plan.sink_for[e['message']['name']] for e in emit})}")


if __name__ == "__main__":
    main()
