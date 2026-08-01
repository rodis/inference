"""Places-as-data: load known places from Neon for the `place` capability's label lookup.

The `regions` table is the one place registry. Its `kind` column once distinguished two
consumers — `kind='zone'` rows were expanded into `entered_`/`left_` geofence definitions,
`kind='poi'` rows label stay centroids. **The zone half was removed 2026-08-01**: no zone row
was ever created, the `geofence` engine never fired in production, and its only downstream
derivations (`arrived_home_by_car` / `left_home_by_car`) were deleted the same day. ADR 0007
is why — clustering replaced fences for dwell, and a fence cannot see a visit that produces no
fixes.

So this module is now the table's only consumer, and `kind='poi'` is the only value in it. The
filter is kept rather than dropped so a future non-POI use of the registry cannot silently
inherit the place book.

Editing a place takes effect on the next runtime start.
"""

import logging

logger = logging.getLogger("inference.places")


def load_places(dsn: str | None) -> list[dict]:
    """Read enabled POI rows as `{name, lat, lon, radius_m, everyday}`. No DSN -> no labels.

    `everyday` marks a place that is not *news* — the one you live in. A stay there is a real
    fact worth deriving and keeping, but it has no natural boundaries in the data: you are
    home for fourteen hours, iOS stops sampling, and `max_gap_seconds` chops the cluster
    wherever the outage fell, so the "visit" is an artifact of sampling rather than of
    behaviour. Carrying the flag on the event lets a consumer skip those without the runtime
    having an opinion about what to draw.

    psycopg is imported lazily so the derivation core and its in-memory tests never need a
    database driver present — the same rule `regions.py` follows.
    """
    if not dsn:
        logger.info("No NEON_DATABASE_URL set; place labels disabled")
        return []
    import psycopg  # lazy: adapter-only dependency

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT name, lat, lon, radius_m, everyday FROM regions "
            "WHERE enabled = true AND kind = 'poi'"
        )
        cols = [c.name for c in cur.description]
        places = [dict(zip(cols, values)) for values in cur.fetchall()]
    logger.info("Loaded %d known place(s) for labelling: %s",
                len(places), [p["name"] for p in places])
    return places
