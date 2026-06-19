from inference.events.messages import GeoLocated, GeoPoint
from inference.pipeline.draft import DerivedDraft
from inference.runtime.registry import register_enricher


class GeoEnricher:
    """Sets the derived event's `location` from its contributors' coordinates.

    Owns the geolocation capability. The pipeline only calls this when the draft
    satisfies `requires` (at least one contributor's message is `GeoLocated`), so
    there's no applicability self-check here — only *selection* of which
    contributors actually carry a point to feed the centroid.

    `strategy` is reserved for choosing how to combine multiple points; only
    `centroid` is implemented today.
    """

    requires = GeoLocated

    def __init__(self, strategy: str = "centroid"):
        self.strategy = strategy

    def enrich(self, draft: DerivedDraft) -> DerivedDraft:
        points = [
            c.message.location
            for c in draft.contributors
            if isinstance(c.message, GeoLocated) and c.message.location is not None
        ]
        if not points:
            return draft

        location = GeoPoint(
            lat=sum(p.lat for p in points) / len(points),
            lon=sum(p.lon for p in points) / len(points),
        )
        return draft.model_copy(
            update={"fields": {**draft.fields, "location": location.model_dump(exclude_none=True)}}
        )


@register_enricher("geo")
def build_geo_enricher(config: dict) -> GeoEnricher:
    return GeoEnricher(strategy=config.get("strategy", "centroid"))
