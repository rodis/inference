import logging
import time

from inference.pipeline.draft import DerivedDraft

logger = logging.getLogger(__name__)


class EnrichmentPipeline:
    """Runs an ordered chain of enrichers over a draft, then finalizes to a dict.

    Best-effort: by the time the pipeline runs, the engine has already decided to
    fire — possibly with irreversible side effects — so a failing enricher must
    degrade the output, not drop the event. A raising enricher is logged and
    skipped; the chain continues with the (partially-enriched) draft, which is
    safe because enrichers are pure (a skip leaves the draft untouched).
    """

    def __init__(self, enrichers: list):
        self.enrichers = enrichers

    def run(self, draft: DerivedDraft) -> dict:
        for enricher in self.enrichers:
            if not _applies(enricher, draft):
                logger.debug(
                    "Enricher %s not applicable to this draft, skipping",
                    type(enricher).__name__,
                )
                continue
            try:
                draft = enricher.enrich(draft)
            except Exception as e:
                logger.error(
                    "Enricher %s failed, skipping: %s",
                    type(enricher).__name__,
                    e,
                    exc_info=True,
                )
        return finalize(draft)


def _applies(enricher, draft: DerivedDraft) -> bool:
    """An enricher applies when it declares no requirement, or at least one
    contributor's message satisfies the required capability (nominal isinstance).
    """
    required = getattr(enricher, "requires", None)
    if required is None:
        return True
    return any(isinstance(c.message, required) for c in draft.contributors)


def finalize(draft: DerivedDraft) -> dict:
    """Merge the engine's core with the enrichers' accreted `fields` into the
    transport dict the Emitter expects.

    `sources`/`evidence` are reconstructed from the contributors to keep the
    `high_level_events` payload a superset of the pre-pipeline output (no
    downstream consumer breaks); capability fields (e.g. `derived_from`,
    `location`) are layered on top.
    """
    # contributors are Envelopes; read the canonical event_name/timestamp from
    # their typed `message` (the data), per the metadata/data invariant.
    message = {
        "event_name": draft.event_name,
        # A derived event must itself be a valid pipeline input so it can contribute
        # to further derivations (ADR 0002 recursive derivation): the window engine
        # keys on `message.timestamp` and MessageBase requires it. We use the integer
        # `occurred_at` as the derived event's timestamp.
        "timestamp": int(draft.occurred_at),
        "confidence_score": draft.confidence_score,
        "occurred_at": draft.occurred_at,
        "sources": [c.message.event_name for c in draft.contributors],
        "evidence": {c.message.event_name: c.message.timestamp for c in draft.contributors},
        **draft.fields,
    }
    return {
        "inference_type": draft.inference_type,
        "processed_at": time.time(),
        "message": message,
    }
