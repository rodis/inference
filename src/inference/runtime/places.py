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

Editing a place takes effect within `PLACE_BOOK_TTL_SECONDS` of the next event, via
`PlaceBookRefresher` below — no restart, and no background thread.
"""

import logging
import time

from inference.capabilities import set_place_book

logger = logging.getLogger("inference.places")


def load_places(dsn: str | None) -> list[dict]:
    """Read enabled POI rows as `{name, lat, lon, radius_m, everyday, categories}`.
    No DSN -> no labels.

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
        # `categories` is the row's ORDERED kind list ("what is this place?" — e.g. a Konditorei
        # is ['bakery','cafe']); the first entry is the primary and drives iconography, the rest
        # exist for filtering. Reference data like `everyday`: stamped onto events at mint time.
        cur.execute(
            "SELECT name, lat, lon, radius_m, everyday, categories FROM regions "
            "WHERE enabled = true AND kind = 'poi'"
        )
        cols = [c.name for c in cur.description]
        places = [dict(zip(cols, values)) for values in cur.fetchall()]
    logger.info("Loaded %d known place(s) for labelling: %s",
                len(places), [p["name"] for p in places])
    return places


class PlaceBookRefresher:
    """Reload the POI place book on a TTL, driven by the event stream rather than by a timer.

    Lives here rather than in `quix.py` so it is testable: CI installs the package with
    `--no-deps` and `quixstreams` is absent, so anything importing the adapter cannot be
    covered by the suite. `loader`/`setter` are injectable for the same reason.

    **No background thread, on purpose.** The runtime has no liveness or readiness probe, so a
    thread that died would be invisible: the pod stays `Running`, the book silently freezes, and
    stays keep getting labelled from stale reference data. That is the worst failure shape —
    wrong, quiet, and indistinguishable from working. Running on the processing thread means a
    failure surfaces exactly where every other failure does.

    Two consequences of being stream-driven, both wanted:

    - **No traffic, no reads.** Neon runs with `suspend_timeout=0`, so every query wakes the
      compute; a poller would keep it awake to answer a question nobody asked.
    - **Fresh exactly where it matters.** The book is only consulted when a stay is shaped, and
      event traffic is what precedes a stay.

    A failed reload keeps the previous book and logs — the same degraded-mode ethos as the initial
    load in `build_runtime`. `set_place_book` rebinds a module-level list, which is atomic under
    the GIL, so no locking is needed even though `shape` reads it on the same thread.
    """

    def __init__(self, dsn: str | None, ttl_seconds: int, loader=None,
                 setter=set_place_book, clock=time.monotonic) -> None:
        self._dsn, self._ttl, self._clock = dsn, ttl_seconds, clock
        self._loader, self._setter = loader or load_places, setter
        self._last = clock()               # build_runtime already loaded it once

    def tick(self, value):
        """Identity step: reloads when the book is older than the TTL, then passes `value` on."""
        if self._ttl <= 0 or self._clock() - self._last < self._ttl:
            return value
        # Stamp BEFORE attempting. If Neon is unreachable this must not retry on every event —
        # a location stream would turn one outage into a connection storm.
        self._last = self._clock()
        try:
            book = self._loader(self._dsn)
        except Exception:
            logger.warning("Place book refresh failed; keeping the previous book", exc_info=True)
            return value
        self._setter(book)
        logger.info("Place book refreshed: %d place(s)", len(book))
        return value
