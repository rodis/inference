"""Aware dashboard — tiny static-site + read-only Neon API.

Serves `index.html` and two JSON endpoints it fetches at load:
  GET /api/events  — every row of the `events` table, shaped for the page
  GET /api/levels  — the logical-level config (logical_levels.json)

Read-only by design: the dashboard only *reads* Neon (the inference runtime is the
sole writer). Connection comes from DATABASE_URL (a Neon Postgres URL, sslmode=require).
Built to run as a single stateless pod — no local state, all data lives in Neon.
"""

import json
import os
from pathlib import Path

import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

HERE = Path(__file__).parent
app = FastAPI(title="aware-dashboard")

# one row per event, same shape the page expects (id, name, event_class, source_app,
# occurred_epoch, message) — aggregated server-side into a single JSON array.
EVENTS_SQL = """
SELECT coalesce(json_agg(json_build_object(
    'id', id, 'name', name, 'event_class', event_class, 'source_app', source_app,
    'occurred_epoch', extract(epoch from occurred_at), 'message', message
  ) ORDER BY occurred_at), '[]'::json)
FROM events
"""


def _db_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise HTTPException(500, "DATABASE_URL is not set")
    return url


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(HERE / "index.html")


@app.get("/api/events")
def events():
    with psycopg.connect(_db_url()) as conn, conn.cursor() as cur:
        cur.execute(EVENTS_SQL)
        return JSONResponse(cur.fetchone()[0])


@app.get("/api/levels")
def levels():
    return JSONResponse(json.loads((HERE / "logical_levels.json").read_text()))


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}
