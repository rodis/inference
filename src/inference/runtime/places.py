"""Places-as-data: load known places from Neon for the `place` capability's label lookup.

Sibling of `regions.py`, reading the SAME table for a different purpose — the `regions` table
is the one place registry, and its `kind` column says what each row is *for*:

    kind='zone'  a region you cross      -> expanded into entered_/left_ geofence definitions
    kind='poi'   a place you stop at     -> a label for stay centroids (this module)

One table because both are "a named circle on the map" and both should be editable in one
place (the dashboard, eventually). Two kinds because the consumers must not overlap: a POI
expanded into a geofence would emit `entered_<slug>` events colliding with the names the
OwnTracks lane already produces, and would fire spurious edges for a radius far smaller than
the sampling can resolve (ADR 0007).

Editing a place takes effect on the next runtime start, like regions.
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
