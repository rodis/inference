"""Regions-as-data: expand Neon `regions` rows into geofence `EventDefinition`s.

This is the adapter-side seam that lets you define places in the database (stable,
per-user, shareable, dashboard-editable) and have the runtime derive region enter/leave
events server-side from the raw `location_ping` stream — no on-phone geofences.

Each region becomes two definitions the router treats exactly like a YAML one:
`entered_<slug>` and `left_<slug>` (engine `geofence`). `build_runtime` appends these to
the YAML-loaded definitions, so `core` never learns regions come from Neon — the psycopg
read lives here (lazily imported), keeping the derivation core import-clean.

Editing a region takes effect on the next runtime start; state is ephemeral (recovered
from the changelog), so a restart is cheap and safe.
"""

import logging
import re

from inference.runtime.definition import EventDefinition

logger = logging.getLogger("inference.regions")

# Slug rule must match the OwnTracks Vector lane (owntracks_to_canonical) so a region
# named "Home" yields entered_home/left_home either way: lowercase, non-alnum runs -> "_",
# trim edge underscores.
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _slug(name: str) -> str:
    return _NON_ALNUM.sub("_", name.lower()).strip("_")


def region_definitions(rows: list[dict]) -> list[EventDefinition]:
    """Expand region rows into geofence `EventDefinition`s (two per region).

    Pure: `rows` are plain dicts (`user_id, name, lat, lon, radius_m`), so this is
    unit-testable with no database. A row with a blank name/slug is skipped.
    """
    defs: list[EventDefinition] = []
    for r in rows:
        slug = _slug(str(r.get("name", "")))
        if not slug:
            logger.warning("Skipping region with empty slug: %r", r)
            continue
        geometry = {
            "lat": r["lat"],
            "lon": r["lon"],
            "radius_m": r["radius_m"],
            "owner": r["user_id"],
        }
        for direction, prefix in (("enter", "entered_"), ("leave", "left_")):
            defs.append(EventDefinition(
                name=f"{prefix}{slug}",
                engine="geofence",
                engine_config={**geometry, "direction": direction},
                source_topic="raw_sensors",
                sink_topic="high_level_events",
            ))
    logger.info("Expanded %d region row(s) into %d geofence definition(s)", len(rows), len(defs))
    return defs


def load_region_definitions(dsn: str | None) -> list[EventDefinition]:
    """Read enabled regions from Neon and expand them. No DSN -> feature off (empty).

    psycopg is imported lazily so the derivation core and its in-memory tests never
    need a database driver present.
    """
    if not dsn:
        logger.info("No NEON_DATABASE_URL set; geofence regions disabled")
        return []
    import psycopg  # lazy: adapter-only dependency

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT user_id, name, lat, lon, radius_m FROM regions WHERE enabled = true"
        )
        cols = [c.name for c in cur.description]
        rows = [dict(zip(cols, values)) for values in cur.fetchall()]
    return region_definitions(rows)
