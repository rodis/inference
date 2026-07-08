"""Aware dashboard — React/Vite bundle + a small Neon-backed API.

Serves the built single-page app (``web/dist``) and a handful of JSON endpoints:

  GET  /api/users               — distinct user_ids in the events table (the selector)
  GET  /api/events?user_id=…    — every event for one user, shaped for the page
  GET  /api/preferences?user_id=… — that user's logical-level/lift config (seed + overrides)
  PUT  /api/preferences?user_id=… — persist that user's config (the one write path)
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

# one row per event, same shape the page expects (id, name, event_class, source_app,
# occurred_epoch, message) — aggregated server-side into a single JSON array, scoped
# to one user (the entity key the whole pipeline partitions on).
EVENTS_SQL = """
SELECT coalesce(json_agg(json_build_object(
    'id', id, 'name', name, 'event_class', event_class, 'source_app', source_app,
    'occurred_epoch', extract(epoch from occurred_at), 'message', message
  ) ORDER BY occurred_at), '[]'::json)
FROM events
WHERE user_id = %s
"""

USERS_SQL = "SELECT user_id FROM events GROUP BY user_id ORDER BY user_id"

PREFS_GET_SQL = "SELECT levels, lift FROM dashboard_prefs WHERE user_id = %s"

PREFS_UPSERT_SQL = """
INSERT INTO dashboard_prefs (user_id, levels, lift, updated_at)
VALUES (%s, %s, %s, now())
ON CONFLICT (user_id) DO UPDATE
  SET levels = EXCLUDED.levels, lift = EXCLUDED.lift, updated_at = now()
"""


def _db_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


def _seed() -> dict:
    """The logical_levels.json defaults — `levels`/`lift` maps a user's prefs overlay."""
    return json.loads(SEED_PATH.read_text())


@asynccontextmanager
async def lifespan(app: FastAPI):
    # A small pooled connection set, opened once — cheaper than psycopg.connect() per
    # request now that several endpoints hit the DB. Sync pool is fine: endpoints run
    # in the threadpool.
    app.state.pool = ConnectionPool(_db_url(), min_size=1, max_size=4, open=True)
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
def events(user_id: str = Query(...)):
    with app.state.pool.connection() as conn, conn.cursor() as cur:
        cur.execute(EVENTS_SQL, (user_id,))
        return JSONResponse(cur.fetchone()[0])


@app.get("/api/preferences")
def get_preferences(user_id: str = Query(...)):
    """Seed defaults overlaid by this user's stored overrides (row may not exist yet)."""
    seed = _seed()
    levels = dict(seed.get("levels", {}))
    lift = dict(seed.get("lift", {}))
    with app.state.pool.connection() as conn, conn.cursor() as cur:
        cur.execute(PREFS_GET_SQL, (user_id,))
        row = cur.fetchone()
    if row:
        levels.update(row[0] or {})
        lift.update(row[1] or {})
    return JSONResponse({"levels": levels, "lift": lift})


@app.put("/api/preferences")
def put_preferences(user_id: str = Query(...), body: dict = Body(...)):
    """Persist a user's full level/lift config — the dashboard's only write."""
    levels = body.get("levels")
    lift = body.get("lift")
    if not isinstance(levels, dict) or not isinstance(lift, dict):
        raise HTTPException(422, "body must be {levels: {...}, lift: {...}}")
    with app.state.pool.connection() as conn, conn.cursor() as cur:
        cur.execute(PREFS_UPSERT_SQL, (user_id, Jsonb(levels), Jsonb(lift)))
    return JSONResponse({"ok": True})


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
