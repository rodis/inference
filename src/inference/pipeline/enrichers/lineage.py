from inference.pipeline.draft import DerivedDraft


class LineageEnricher:
    """Records the contributing events as `derived_from` lineage.

    Always applies (`requires = None`) — every derived event has contributors.
    Each ref carries the contributor's `envelope_id` (the stable join key, minted
    by Vector at ingest) plus `event_name` + `timestamp` for human-readable provenance.
    """

    requires = None

    def enrich(self, draft: DerivedDraft) -> DerivedDraft:
        derived_from = [
            {
                "envelope_id": str(c.envelope_id),
                "event_name": c.message.event_name,
                "timestamp": c.message.timestamp,
            }
            for c in draft.contributors
        ]
        return draft.model_copy(update={"fields": {**draft.fields, "derived_from": derived_from}})
