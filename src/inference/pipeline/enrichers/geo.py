from inference.pipeline.draft import DerivedDraft


class GeoEnricher:
    """Sets the derived event's `location` from its contributors' coordinates.

    Owns the geolocation capability: if the contributors carry coordinates, the
    derived event becomes geolocated; if none do, it returns the draft unchanged
    (a derived event whose contributors aren't geolocated isn't geolocated either).

    Scaffold for now: no producer emits coordinates yet, so this is effectively a
    no-op. It duck-types on a `message["location"]` of `{lat, lon}`; once typed
    messages land (Phase 2) this becomes an `isinstance(msg, GeoLocated)` check.
    `strategy` is reserved for choosing how to combine multiple points; only
    `centroid` is implemented today.
    """

    def __init__(self, strategy: str = "centroid"):
        self.strategy = strategy

    def enrich(self, draft: DerivedDraft) -> DerivedDraft:
        points = [
            loc
            for c in draft.contributors
            if isinstance((loc := c.message.get("location")), dict)
            and "lat" in loc
            and "lon" in loc
        ]
        if not points:
            return draft

        location = {
            "lat": sum(p["lat"] for p in points) / len(points),
            "lon": sum(p["lon"] for p in points) / len(points),
        }
        return draft.model_copy(update={"fields": {**draft.fields, "location": location}})
