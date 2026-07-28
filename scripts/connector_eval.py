"""Score a CONNECTOR on latency, duplicates, freshness and contract compliance.

The adjudicator for [ADR 0008](../doc/adr/0008-connector-tier-via-n8n.md). A connector is an
n8n workflow POSTing to `/sensors/<app>` — logic that lives outside git, so the only way to
know it is behaving is to measure what arrives. `backtest.py`/`trip_eval.py` judge *derivation*
by replaying the core; this judges *ingestion*, and needs no replay at all — every fact it
reports is already in Neon.

What it reports, per `source_app`:

  latency       Split three ways, because the halves have different owners and only one is
                ours to fix:
                  trigger lag   `n8n_polled_at - occurred_at` — source event -> connector
                                noticing. For a POLLING trigger this is bounded by the poll
                                interval (n8n's Gmail floor is 60s), so ~0-60s is correct,
                                not a fault. Requires the connector to send `n8n_polled_at`.
                  pipeline lag  `ingested_at - n8n_polled_at` — connector -> Vector -> Kafka
                                -> Neon. OURS. Measured baseline 2026-07-28: ~3.3s (Vector
                                sink linger + the persister's batch.timeout_secs:1 + Neon
                                waking from scale-to-zero). Judge against ~3s, NOT 0s.
                  end-to-end    `ingested_at - occurred_at`. Always available.

  duplicates    Rows sharing an `upstream_id`. Ingest is at-most-once and mints a fresh uuid4
                per POST, so a re-delivered item becomes a second row rather than a conflict
                (deliberate — a deterministic id would fail the WHOLE 500-event Neon batch and
                stall persistence for unrelated sources; see doc/connectors.md hazard 2).
                That makes duplicates cheap but invisible, hence this check.

  freshness     Age of the newest event. THE metric that matters operationally: a dead n8n
                produces silence, and silence is indistinguishable from "nothing happened at
                the source". Nothing pages you, so this is a number you have to look at.

  contract      Violations of doc/connectors.md, each of which passes ingest silently and
                breaks something later:
                  ts_not_number    a STRING timestamp. `shape_sensor` only checks the field
                                   EXISTS, but capabilities.py and core._lineage subscript
                                   `message["timestamp"]` — so this is a latent crash, not a
                                   cosmetic issue.
                  forbidden_field  `inference_type`/`derived_from` on a raw event — would
                                   mis-class the row as event_class 'derived'.
                  unknown_user     a `user_id` outside the known set. Silently fragments
                                   per-entity state, or buckets under `_no_user_id`; the
                                   single easiest field to get wrong in a GUI.

What it CANNOT check is completeness: only the source knows how many items it had. Compare a
count at the source (e.g. a Gmail search for the label) against `n` here for the same window.
Given the reported n8n Gmail-Trigger misses, treat a gap as expected until proven otherwise.

Usage (same env/DSN as backtest.py):
  NEON_DATABASE_URL=... uv run python scripts/connector_eval.py --days 7
  NEON_DATABASE_URL=... uv run python scripts/connector_eval.py --days 7 --source gmail -v
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import psycopg
from dotenv import find_dotenv, load_dotenv

# Sources that are NOT connectors: our own producers (a worker or an iOS Shortcut POSTing
# directly), the runtime's own derived output, and replay artefacts. They owe nothing to the
# connector contract — `n8n_polled_at` and `upstream_id` are meaningless for them — so scoring
# them just manufactures warnings. Hidden unless --all or an explicit --source.
#
# Deliberately an exclusion list, not an allowlist: OUR producers are a small set that grows
# once per worker, while connectors are the growing set that must cost zero code to add. A new
# connector is therefore scored automatically; a new first-party producer needs a line here.
NON_CONNECTORS = ("inference", "owntracks", "overland", "shortcut", "bmw", "backtest", "manual")

PIPELINE_LAG_BUDGET = 10.0   # seconds; ~3x the measured 3.3s baseline before it's worth a look
STALE_HOURS = 24.0           # newest event older than this = the connector may be dead

# One statement, because every metric is an aggregate over the same filtered rows and a second
# pass would let the window shift under us (Neon is live).
_SUMMARY = """
WITH rows AS (
    SELECT source_app,
           name,
           user_id,
           message,
           event_class,
           occurred_at,
           ingested_at,
           EXTRACT(EPOCH FROM ingested_at - occurred_at) AS e2e,
           CASE WHEN jsonb_typeof(message->'n8n_polled_at') = 'number'
                THEN (message->>'n8n_polled_at')::bigint - EXTRACT(EPOCH FROM occurred_at)
           END AS trigger_lag,
           CASE WHEN jsonb_typeof(message->'n8n_polled_at') = 'number'
                THEN EXTRACT(EPOCH FROM ingested_at) - (message->>'n8n_polled_at')::bigint
           END AS pipeline_lag
      FROM events
     WHERE ingested_at > now() - make_interval(days => %(days)s)
       -- ::text is required, not cosmetic: psycopg sends an untyped NULL and Postgres then
       -- can't infer the parameter's type ("could not determine data type of parameter $2").
       AND (%(source)s::text IS NULL OR source_app = %(source)s::text)
)
SELECT source_app,
       count(*)                                          AS n,
       count(DISTINCT name)                              AS names,
       max(ingested_at)                                  AS last_seen,
       EXTRACT(EPOCH FROM now() - max(ingested_at)) / 3600.0 AS age_hours,

       percentile_cont(0.5)  WITHIN GROUP (ORDER BY e2e)  AS e2e_p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY e2e)  AS e2e_p95,
       max(e2e)                                          AS e2e_max,

       count(trigger_lag)                                AS n_stamped,
       percentile_cont(0.5)  WITHIN GROUP (ORDER BY trigger_lag)
           FILTER (WHERE trigger_lag IS NOT NULL)        AS trig_p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY trigger_lag)
           FILTER (WHERE trigger_lag IS NOT NULL)        AS trig_p95,
       max(trigger_lag)                                  AS trig_max,

       percentile_cont(0.5)  WITHIN GROUP (ORDER BY pipeline_lag)
           FILTER (WHERE pipeline_lag IS NOT NULL)       AS pipe_p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY pipeline_lag)
           FILTER (WHERE pipeline_lag IS NOT NULL)       AS pipe_p95,
       max(pipeline_lag)                                 AS pipe_max,

       count(*) FILTER (WHERE jsonb_typeof(message->'timestamp') <> 'number') AS ts_not_number,
       -- Scoped to raw: a DERIVED event carries inference_type by design, so an unscoped check
       -- reports every derived row as a violation (it did — 219/219 on `inference`).
       count(*) FILTER (WHERE event_class = 'raw'
                          AND (message ? 'inference_type' OR message ? 'derived_from'))
                                                         AS forbidden_field,
       count(*) FILTER (WHERE NOT (user_id = ANY(%(users)s))) AS unknown_user,
       count(*) FILTER (WHERE message ? 'upstream_id')    AS n_with_upstream_id
  FROM rows
 GROUP BY source_app
 ORDER BY n DESC
"""

# Duplicates need the per-id grouping the summary can't express, so it's a second statement.
_DUPES = """
SELECT source_app,
       message->>'upstream_id' AS upstream_id,
       count(*)                AS copies,
       min(ingested_at)        AS first_at,
       max(ingested_at)        AS last_at
  FROM events
 WHERE ingested_at > now() - make_interval(days => %(days)s)
   AND (%(source)s::text IS NULL OR source_app = %(source)s::text)
   AND message ? 'upstream_id'
 GROUP BY 1, 2
HAVING count(*) > 1
 ORDER BY copies DESC, last_at DESC
 LIMIT 20
"""


def _n(v, unit: str = "s", width: int = 7) -> str:
    """Format a possibly-NULL numeric so a missing measurement reads as absent, not as zero."""
    return f"{'—':>{width}}" if v is None else f"{float(v):>{width}.1f}{unit}"


def report(dsn: str, days: int, source: str | None, users: list[str],
           include_all: bool, verbose: bool) -> int:
    """Print the report; return a shell exit code (0 clean, 1 = something needs attention)."""
    params = {"days": days, "source": source, "users": users}
    with psycopg.connect(dsn) as conn:
        cur = conn.execute(_SUMMARY, params)
        cols = [d.name for d in cur.description]
        summary = cur.fetchall()
        dupes = conn.execute(_DUPES, params).fetchall()

    rows = [dict(zip(cols, r, strict=True)) for r in summary]
    if not include_all and source is None:
        rows = [r for r in rows if r["source_app"] not in NON_CONNECTORS]
    if not rows:
        print(f"No events in the last {days}d"
              + (f" for source_app={source!r}." if source else " from any connector.")
              + ("" if include_all or source else f" (Non-connectors hidden: {', '.join(NON_CONNECTORS)}."
                                                 " Use --all to include them.)"))
        return 0

    problems: list[str] = []

    print(f"\n=== Connectors — last {days}d ===\n")
    print(f"{'source_app':<14}{'n':>7}{'names':>7}   "
          f"{'e2e p50':>9}{'p95':>9}{'max':>9}   {'age':>8}")
    print("-" * 72)
    for r in rows:
        age = float(r["age_hours"])
        flag = "  ⚠ STALE" if age > STALE_HOURS else ""
        print(f"{r['source_app']:<14}{r['n']:>7}{r['names']:>7}   "
              f"{_n(r['e2e_p50'], '', 9)}{_n(r['e2e_p95'], '', 9)}{_n(r['e2e_max'], '', 9)}   "
              f"{age:>7.1f}h{flag}")
        if age > STALE_HOURS:
            problems.append(f"{r['source_app']}: no events for {age:.1f}h — connector may be dead")

    print("\n--- latency split (needs `n8n_polled_at`; trigger lag is the connector's, pipeline is ours) ---")
    print(f"{'source_app':<14}{'stamped':>9}   "
          f"{'trig p50':>10}{'p95':>10}   {'pipe p50':>10}{'p95':>10}{'max':>10}")
    print("-" * 76)
    for r in rows:
        stamped = f"{r['n_stamped']}/{r['n']}"
        print(f"{r['source_app']:<14}{stamped:>9}   "
              f"{_n(r['trig_p50'], '', 10)}{_n(r['trig_p95'], '', 10)}   "
              f"{_n(r['pipe_p50'], '', 10)}{_n(r['pipe_p95'], '', 10)}{_n(r['pipe_max'], '', 10)}")
        if not r["n_stamped"]:
            problems.append(f"{r['source_app']}: no `n8n_polled_at` — trigger lag is unattributable")
        elif r["pipe_p50"] is not None and float(r["pipe_p50"]) < 0:
            # Pipeline lag cannot be negative — the row is persisted AFTER it was fetched. So a
            # negative value means `n8n_polled_at` itself is wrong, and the overwhelmingly likely
            # cause is UNITS: n8n's Date.now() returns MILLISECONDS, and a ms value read as
            # seconds lands ~56000 years in the future. (Clock skew is the boring alternative.)
            # Worth its own message because the number looks like a latency result, not a bug.
            problems.append(
                f"{r['source_app']}: pipeline lag p50 is NEGATIVE ({float(r['pipe_p50']):.1f}s) — "
                "`n8n_polled_at` is in the future. Check it is epoch SECONDS, not Date.now() ms")
        elif r["pipe_p95"] is not None and float(r["pipe_p95"]) > PIPELINE_LAG_BUDGET:
            problems.append(f"{r['source_app']}: pipeline lag p95 "
                            f"{float(r['pipe_p95']):.1f}s > {PIPELINE_LAG_BUDGET}s budget (ours to fix)")

    print("\n--- contract compliance (each of these passes ingest silently) ---")
    print(f"{'source_app':<14}{'ts!=number':>12}{'forbidden':>11}"
          f"{'unknown_user':>14}{'upstream_id':>13}")
    print("-" * 66)
    for r in rows:
        has_id = f"{r['n_with_upstream_id']}/{r['n']}"
        print(f"{r['source_app']:<14}{r['ts_not_number']:>12}{r['forbidden_field']:>11}"
              f"{r['unknown_user']:>14}{has_id:>13}")
        for key, label in (("ts_not_number", "non-numeric message.timestamp (latent crash in shaping)"),
                           ("forbidden_field", "inference_type/derived_from on a raw event"),
                           ("unknown_user", f"user_id outside {users}")):
            if r[key]:
                problems.append(f"{r['source_app']}: {r[key]} events with {label}")
        if not r["n_with_upstream_id"]:
            problems.append(f"{r['source_app']}: no `upstream_id` — duplicates undetectable")

    print("\n--- duplicates (same upstream_id, >1 row) ---")
    if not dupes:
        print("  none 🎉")
    else:
        for app, uid, copies, first_at, last_at in dupes:
            print(f"  {app:<12} {uid:<40} ×{copies}  "
                  f"{first_at:%m-%d %H:%M:%S} → {last_at:%m-%d %H:%M:%S}")
        total = sum(c - 1 for _, _, c, _, _ in dupes)
        problems.append(f"{total} redundant row(s) across {len(dupes)} duplicated upstream_id(s)")

    print()
    if problems:
        print("⚠ needs attention:")
        for p in problems:
            print(f"  · {p}")
        print()
    elif verbose:
        print("✅ clean: fresh, in-contract, no duplicates.\n")
    return 1 if problems else 0


def main() -> int:
    load_dotenv(find_dotenv(usecwd=True) or Path(__file__).resolve().parents[1] / "workers/.env")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7, help="window to score (default 7)")
    ap.add_argument("--source", help="one source_app (implies --all for that app)")
    ap.add_argument("--user", action="append", default=None,
                    help="known user_id; repeatable (default: rods)")
    ap.add_argument("--all", action="store_true",
                    help=f"include non-connectors ({', '.join(NON_CONNECTORS)})")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    dsn = os.environ.get("NEON_DATABASE_URL")
    if not dsn:
        raise SystemExit("NEON_DATABASE_URL is not set (workers/.env or the environment)")
    return report(dsn, args.days, args.source, args.user or ["rods"], args.all, args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
