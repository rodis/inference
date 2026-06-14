from inference.pipeline.draft import DerivedDraft


class LineageEnricher:
    """Records the contributing events as `derived_from` lineage.

    Always applies — every derived event has contributors. Each ref carries
    `event_name` + `timestamp`; `envelope_id` stays `None` until Vector mints
    stable ids (Phase 2), at which point lineage becomes a real join key.
    """

    def enrich(self, draft: DerivedDraft) -> DerivedDraft:
        derived_from = []
        for c in draft.contributors:
            # envelope_id doesn't exist on Envelope until Phase 2 (Vector-minted);
            # getattr keeps this forward-compatible — it lights up automatically then.
            envelope_id = getattr(c, "envelope_id", None)
            derived_from.append(
                {
                    "event_name": c.message.get("event_name"),
                    "timestamp": c.message.get("timestamp"),
                    "envelope_id": str(envelope_id) if envelope_id is not None else None,
                }
            )
        return draft.model_copy(update={"fields": {**draft.fields, "derived_from": derived_from}})
