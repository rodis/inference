"""Aware dashboard — React/Vite bundle + a small Neon-backed API.

Serves the built single-page app (``web/dist``) and a handful of JSON endpoints:

  GET  /api/users               — distinct user_ids in the events table (the selector)
  GET  /api/events?user_id=…&days=N — one user's events over a trailing N-day window
  GET  /api/preferences?user_id=… — that user's level config (their row, else the seed)
  PUT  /api/preferences?user_id=… — persist that user's config (the one write path)
  GET  /api/processes           — process definitions as graphs (ADR 0012's process tier)
  GET  /api/processes/{name}/cycles?user_id=… — that process's recent cycles + milestones
  GET  /api/stream?user_id=…    — SSE seam for the (deferred) live view; stubbed for now
  GET  /healthz                 — liveness

Reads come from the Neon ``events`` table (the inference runtime is its sole writer);
the only thing the dashboard writes is its own ``dashboard_prefs`` table. Connection
comes from DATABASE_URL (a Neon Postgres URL, sslmode=require). Stateless pod — all
state lives in Neon.
"""

import asyncio
import base64
import binascii
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

log = logging.getLogger("aware-dashboard")

HERE = Path(__file__).parent
DIST = HERE / "web" / "dist"          # Vite build output (absent in local dev — Vite serves it)
SEED_PATH = HERE / "logical_levels.json"
PROCESSES_PATH = HERE / "processes.json"   # generated; see scripts/emit_process_graph.py

# one row per event, same shape the page expects (id, name, event_class, source_app,
# occurred_epoch, message) — aggregated server-side into a single JSON array, scoped
# to one user (the entity key the whole pipeline partitions on) **and** to a trailing
# window of whole days.
#
# The window is the point: the UI is day-based (it renders one day and offers a short
# day picker), so history older than the window can't be displayed at all — fetching it
# was pure waste that grew with every event ever recorded, and a high-rate source like
# the movement tracker's location pings makes that growth steep. Bounding it here (not
# by filtering in the browser) keeps the payload flat as history accrues.
#
# Whole *days*, not a rolling 24h*N, so the window lines up with the day the UI draws;
# UTC, matching the client's day bucketing (`dayKey`). days=1 is today only. Rows with a
# NULL occurred_at drop out — they have no place on a timeline anyway (they used to land
# on 1970-01-01).
EVENTS_SQL = """
SELECT coalesce(json_agg(json_build_object(
    'id', id, 'name', name, 'event_class', event_class, 'source_app', source_app,
    'occurred_epoch', extract(epoch from occurred_at), 'message', message
  ) ORDER BY occurred_at), '[]'::json)
FROM events
WHERE user_id = %s
  AND occurred_at >= (
        date_trunc('day', now() AT TIME ZONE 'UTC') - make_interval(days => %s::int - 1)
      ) AT TIME ZONE 'UTC'
"""

# Fallback window when the client doesn't ask for one; the SPA passes its own (DAY_WINDOW
# in web/src/api.ts) so the day picker and the fetch can't disagree.
DEFAULT_DAYS = 7

USERS_SQL = "SELECT user_id FROM events GROUP BY user_id ORDER BY user_id"

# The dashboard's level config, as of the single-override model: `level` holds only the
# event types whose lane differs from the one their derivation depth implies, and `hidden`
# lists the types kept off the timeline entirely. A type the user never touched has no
# entry in either — the client derives its lane from depth (see web/src/view.ts).
#
# This replaced a `levels` + `lift` pair that stored a home lane *and* a ceiling for every
# type. Only the ceiling ever affected what rendered, so the home lane was decoration that
# doubled the write. Those two columns are still on the table, unread, as a rollback path.
PREFS_GET_SQL = "SELECT level, hidden FROM dashboard_prefs WHERE user_id = %s"

PREFS_UPSERT_SQL = """
INSERT INTO dashboard_prefs (user_id, level, hidden, updated_at)
VALUES (%s, %s, %s, now())
ON CONFLICT (user_id) DO UPDATE
  SET level = EXCLUDED.level, hidden = EXCLUDED.hidden, updated_at = now()
"""

# The Money module's aggregate (portal P2, issue #64) — the first module-owned route: it
# ships with dashboards/money/* and the frame never learns its shape. One round trip, all
# aggregation server-side, because the whole point of the module data seam is that a board
# wanting 30 days of payments must not widen the shared events window every board loads.
#
# Two design decisions worth stating:
#
# - **The place join is time containment, not merchant-name parsing.** A payment that fell
#   inside a labelled stay's interval inherits that stay's place label — the same rule the
#   timeline's moments lane draws bands with (hostOf), done in SQL. The *shortest* containing
#   stay wins, mirroring hostOf's innermost-container rule. Merchant strings are terminal
#   noise ("Backerei Konditorei", "Coop-2214 Baar SBB"); the stay label is reference data the
#   user curated, so it groups cleanly. Unmatched payments keep their merchant string.
# - **Windows are trailing whole UTC days**, same convention as EVENTS_SQL, and the previous
#   window (for the delta) is simply the equal-length window before it. days=7 ⇒ "this week"
#   means the last 7 whole days, not a calendar week — honest and picker-free.
SPEND_SQL = """
WITH bounds AS (
  SELECT (date_trunc('day', now() AT TIME ZONE 'UTC')
            - make_interval(days => %(days)s::int - 1)) AT TIME ZONE 'UTC' AS start,
         (date_trunc('day', now() AT TIME ZONE 'UTC') + interval '1 day') AT TIME ZONE 'UTC' AS fin,
         (date_trunc('day', now() AT TIME ZONE 'UTC')
            - make_interval(days => 2 * %(days)s::int - 1)) AT TIME ZONE 'UTC' AS prev_start
),
pay AS (
  SELECT e.id, e.occurred_at,
         (e.message->>'amount')::numeric AS amount,
         coalesce(e.message->>'merchant', 'unknown') AS merchant,
         extract(epoch from e.occurred_at) AS ts
  FROM events e, bounds b
  WHERE e.name = 'credit_card_payment' AND e.user_id = %(user_id)s
    AND e.occurred_at >= b.start AND e.occurred_at < b.fin
),
located AS (
  SELECT p.*, s.label
  FROM pay p
  LEFT JOIN LATERAL (
    SELECT st.message->'place'->>'label' AS label
    FROM events st, bounds b
    WHERE st.name = 'stay' AND st.user_id = %(user_id)s
      -- a stay emits at departure, so one containing this payment lands at or after it;
      -- the +1 day catches a stay still open at the window's edge
      AND st.occurred_at >= p.occurred_at AND st.occurred_at < b.fin + interval '1 day'
      AND st.message->'place'->>'label' IS NOT NULL
      AND (st.message->'interval'->>'started_at')::numeric <= p.ts
      AND (st.message->'interval'->>'ended_at')::numeric >= p.ts
    ORDER BY (st.message->'interval'->>'duration_seconds')::numeric ASC
    LIMIT 1
  ) s ON true
),
day_list AS (
  SELECT to_char(date_trunc('day', now() AT TIME ZONE 'UTC') - make_interval(days => n),
                 'YYYY-MM-DD') AS day
  FROM generate_series(%(days)s::int - 1, 0, -1) AS n
),
by_day AS (
  SELECT d.day,
         coalesce(sum(l.amount), 0)::float AS total,
         count(l.id)::int AS count
  FROM day_list d
  LEFT JOIN located l
    ON to_char(l.occurred_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') = d.day
  GROUP BY d.day
),
merchants AS (
  SELECT coalesce(l.label, l.merchant) AS label,
         bool_or(l.label IS NOT NULL) AS placed,
         count(*)::int AS count,
         sum(l.amount)::float AS total
  FROM located l
  GROUP BY 1
),
prev AS (
  SELECT coalesce(sum((e.message->>'amount')::numeric), 0)::float AS total
  FROM events e, bounds b
  WHERE e.name = 'credit_card_payment' AND e.user_id = %(user_id)s
    AND e.occurred_at >= b.prev_start AND e.occurred_at < b.start
)
SELECT json_build_object(
  'days', %(days)s::int,
  'total', coalesce((SELECT sum(amount) FROM pay), 0)::float,
  'count', (SELECT count(*) FROM pay)::int,
  'matched', (SELECT count(*) FROM located WHERE label IS NOT NULL)::int,
  'prev_total', (SELECT total FROM prev),
  'by_day', (SELECT json_agg(json_build_object(
                'day', day, 'total', total, 'count', count) ORDER BY day) FROM by_day),
  'merchants', coalesce((SELECT json_agg(json_build_object(
                'label', label, 'placed', placed, 'count', count, 'total', total)
                ORDER BY total DESC)
              FROM (SELECT * FROM merchants ORDER BY total DESC LIMIT 8) m), '[]'::json)
)
"""


# --- the process tier (ADR 0012) -----------------------------------------------
# A process is `processes/*.yml` + a reconciler that advances it; its *state* is the set of
# milestones recorded so far, and nothing else. So this route needs no reconciler and no
# scheduler view — it reads the same `events` table everything else does, and the frontier
# ("which stage is it parked at?") is derived in the browser by intersecting these rows with
# the stage list from `processes.json`. That derivation living on the client is not laziness:
# it is the same pure function the reconciler itself applies, so the page cannot disagree
# with the runner about where a cycle stands.
#
# `source_app = 'process'` is the discriminator rather than a `name LIKE 'dreamhost_%'`
# prefix match — the reconciler stamps it on every milestone, so it stays correct for
# process #2 without this file changing. The per-cycle grouping key is `cycle_key`, which
# every milestone carries (verified across all 11 invoice stages).
#
# No trailing-window bound here, unlike EVENTS_SQL: a monthly process has ~11 rows a cycle,
# and the whole point of the view is the long tail — "how did the last nine invoices go?".
# `limit` bounds it by CYCLE instead, which is the unit a reader actually asks for.
PROCESS_CYCLES_SQL = """
WITH ms AS (
  SELECT e.message->>'cycle_key' AS cycle_key,
         e.name,
         extract(epoch from e.occurred_at) AS at,
         e.message
  FROM events e
  WHERE e.source_app = 'process'
    AND e.user_id = %(user_id)s
    AND e.message->>'process' = %(process)s
    AND e.message->>'cycle_key' IS NOT NULL
),
cycles AS (
  SELECT cycle_key,
         min(at) AS opened_epoch,
         max(at) AS last_epoch,
         count(*)::int AS milestone_count,
         json_agg(json_build_object('name', name, 'epoch', at, 'message', message)
                  ORDER BY at) AS milestones
  FROM ms
  GROUP BY cycle_key
  ORDER BY min(at) DESC
  LIMIT %(limit)s
)
SELECT coalesce(json_agg(c ORDER BY c.opened_epoch DESC), '[]'::json) FROM cycles c
"""


def _processes() -> dict:
    """The process graph, generated from `processes/*.yml` by scripts/emit_process_graph.py.

    Read fresh rather than cached at import: the file is baked into the image, so re-reading
    costs one small stat per request and removes any chance of a stale in-process copy after
    a redeploy. Missing file is not fatal — the tier is optional, and a dashboard that 404s
    one board is better than one that will not start.
    """
    if not PROCESSES_PATH.exists():
        return {"processes": []}
    return json.loads(PROCESSES_PATH.read_text())


def _db_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


def _seed() -> dict:
    """logical_levels.json — the config a user starts with before they've saved anything.

    A first-run default, *not* an overlay: once a row exists it is the whole truth. Merging
    would make "reset this override" impossible, because the seed would immediately put the
    override back.
    """
    return json.loads(SEED_PATH.read_text())


@asynccontextmanager
async def lifespan(app: FastAPI):
    # A small pooled connection set, opened once — cheaper than psycopg.connect() per
    # request now that several endpoints hit the DB. Sync pool is fine: endpoints run
    # in the threadpool.
    #
    # `check` validates (and transparently replaces) each connection on checkout. Neon
    # scales the compute to zero when idle, which drops the server side of every pooled
    # connection; without the check the pool would hand out a dead socket and the first
    # request after any idle period would 500 (SSL connection closed unexpectedly) — the
    # "reload is almost always 500" symptom. The check costs a tiny round-trip per request.
    app.state.pool = ConnectionPool(
        _db_url(), min_size=1, max_size=4, open=True,
        check=ConnectionPool.check_connection,
    )
    try:
        yield
    finally:
        app.state.pool.close()


app = FastAPI(title="aware-dashboard", lifespan=lifespan)


# --- HTTP Basic auth ------------------------------------------------------------
# The dashboard exposes one user's life data on a public URL, so the whole surface
# (SPA, static assets, API, SSE) sits behind a single shared credential. We have one
# user today; Basic auth is the simplest thing that fully closes the hole, and the
# browser handles the login prompt + credential caching natively, so the SPA needs no
# change. Enforced as middleware (not a per-route dependency) precisely so it also
# covers the StaticFiles mount and the SPA fallback, which dependencies don't reach.
#
# Credentials come from env: DASHBOARD_PASSWORD (required to serve; unset = fail
# CLOSED, everything 401s) and DASHBOARD_USER (defaults to "aware"). /healthz is
# exempt so K8s probes, which send no credentials, keep working.
_BASIC_USER = os.environ.get("DASHBOARD_USER", "aware")
_BASIC_PASSWORD = os.environ.get("DASHBOARD_PASSWORD")

if not _BASIC_PASSWORD:
    log.warning(
        "DASHBOARD_PASSWORD is unset — every request except /healthz will 401. "
        "Set it (Doppler in prod, env locally) to serve the dashboard."
    )

_UNAUTHORIZED = Response(
    status_code=401,
    headers={"WWW-Authenticate": 'Basic realm="aware", charset="UTF-8"'},
)


def _authorized(header: str | None) -> bool:
    if not _BASIC_PASSWORD or not header or not header.startswith("Basic "):
        return False
    try:
        user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    # compare_digest on both halves keeps the check constant-time per field.
    return secrets.compare_digest(user, _BASIC_USER) and secrets.compare_digest(
        password, _BASIC_PASSWORD
    )


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if request.url.path == "/healthz":
        return await call_next(request)
    if not _authorized(request.headers.get("authorization")):
        return _UNAUTHORIZED
    return await call_next(request)


@app.get("/api/users")
def users():
    with app.state.pool.connection() as conn, conn.cursor() as cur:
        cur.execute(USERS_SQL)
        return JSONResponse([r[0] for r in cur.fetchall()])


@app.get("/api/events")
def events(user_id: str = Query(...), days: int = Query(DEFAULT_DAYS, ge=1, le=90)):
    """One user's events over the last `days` whole days (UTC), oldest first.

    `days` is what the client's day picker spans; the ceiling is a guard so a
    hand-crafted URL can't ask for the whole table.
    """
    with app.state.pool.connection() as conn, conn.cursor() as cur:
        cur.execute(EVENTS_SQL, (user_id, days))
        return JSONResponse(cur.fetchone()[0])


@app.get("/api/preferences")
def get_preferences(user_id: str = Query(...)):
    """This user's stored level config, or the seed if they have never saved one."""
    with app.state.pool.connection() as conn, conn.cursor() as cur:
        cur.execute(PREFS_GET_SQL, (user_id,))
        row = cur.fetchone()
    if row is None:
        seed = _seed()
        return JSONResponse(
            {"level": seed.get("level", {}), "hidden": seed.get("hidden", [])}
        )
    return JSONResponse({"level": row[0] or {}, "hidden": row[1] or []})


@app.put("/api/preferences")
def put_preferences(user_id: str = Query(...), body: dict = Body(...)):
    """Persist a user's level config — the dashboard's only write.

    `level` is sparse by design (overrides only) and `hidden` replaces rather than merges,
    so an empty body is a meaningful state: "everything sits where its depth puts it".
    """
    level = body.get("level")
    hidden = body.get("hidden")
    if not isinstance(level, dict) or not isinstance(hidden, list):
        raise HTTPException(422, "body must be {level: {...}, hidden: [...]}")
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in level.values()):
        raise HTTPException(422, "level values must be integers")
    if not all(isinstance(n, str) for n in hidden):
        raise HTTPException(422, "hidden must be a list of event names")
    with app.state.pool.connection() as conn, conn.cursor() as cur:
        cur.execute(PREFS_UPSERT_SQL, (user_id, Jsonb(level), Jsonb(hidden)))
    return JSONResponse({"ok": True})


@app.get("/api/money/spend")
def money_spend(user_id: str = Query(...), days: int = Query(7, ge=1, le=90)):
    """Spend over the trailing `days` whole UTC days: per-day totals (zero-filled), the
    previous equal window for the delta, and merchants grouped by the place label the
    payment×stay containment join assigns (falling back to the raw merchant string)."""
    with app.state.pool.connection() as conn, conn.cursor() as cur:
        cur.execute(SPEND_SQL, {"user_id": user_id, "days": days})
        return JSONResponse(cur.fetchone()[0])


@app.get("/api/processes")
def processes():
    """The process definitions as a graph — stages, kinds, dependencies, how cycles open.

    Static data (the definition *is* the graph, ADR 0012), so no user scoping and no DB.
    """
    return JSONResponse(_processes())


@app.get("/api/processes/{name}/cycles")
def process_cycles(name: str, user_id: str = Query(...), limit: int = Query(12, ge=1, le=60)):
    """One process's recent cycles, each with its recorded milestones oldest-first.

    404s on a process this dashboard has no definition for, rather than returning an empty
    list — "no such process" and "a process with no cycles yet" are different answers, and
    conflating them would make a stale `processes.json` look like a quiet month.
    """
    known = {p["name"] for p in _processes()["processes"]}
    if name not in known:
        raise HTTPException(404, f"no process definition named {name!r}")
    with app.state.pool.connection() as conn, conn.cursor() as cur:
        cur.execute(PROCESS_CYCLES_SQL,
                    {"user_id": user_id, "process": name, "limit": limit})
        return JSONResponse(cur.fetchone()[0])


@app.get("/api/stream")
async def stream(user_id: str = Query(...)):
    """SSE seam for the deferred live view. Emits a keep-alive heartbeat only; the
    Neon-polling delta loop (by ingested_at) lands in a later phase."""

    async def gen():
        yield "event: hello\ndata: {}\n\n"
        while True:
            await asyncio.sleep(15)
            yield ": keep-alive\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


# --- static SPA bundle (built by Vite into web/dist) ----------------------------
# Mounted last so /api/* and /healthz win. Absent in local dev, where Vite's dev
# server serves the app and proxies /api here.
if (DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


def _spa() -> FileResponse:
    bundle = DIST / "index.html"
    if not bundle.is_file():
        raise HTTPException(503, "UI bundle not built (run `npm run build` in web/)")
    return FileResponse(bundle)


@app.get("/", include_in_schema=False)
def index():
    return _spa()


@app.get("/{path:path}", include_in_schema=False)
def spa_fallback(path: str):
    # Client-side routes (e.g. /d/timeline) must return the SPA shell so deep links and
    # refreshes resolve. /api/* and /assets/* match their own routes/mount first; guard
    # anyway so an unknown API path 404s instead of silently serving HTML.
    if path.startswith(("api/", "assets/")):
        raise HTTPException(404)
    return _spa()
