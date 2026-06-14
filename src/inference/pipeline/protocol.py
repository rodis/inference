from typing import Protocol, runtime_checkable

from inference.pipeline.draft import DerivedDraft


@runtime_checkable
class Enricher(Protocol):
    """Shapes one aspect of a derived event.

    Each enricher owns a single capability and self-decides applicability: if the
    capability does not apply (e.g. the contributors aren't geolocated), it returns
    the draft unchanged. Enrichers must be pure — return a new draft via
    `draft.model_copy(update=...)`, never mutate the input.
    """

    def enrich(self, draft: DerivedDraft) -> DerivedDraft: ...
