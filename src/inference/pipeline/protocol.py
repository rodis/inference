from typing import Protocol, runtime_checkable

from inference.pipeline.draft import DerivedDraft


@runtime_checkable
class Enricher(Protocol):
    """Shapes one aspect of a derived event.

    Applicability is declared, not self-decided: `requires` names the capability
    a contributor's message must have for this enricher to run (`None` = always).
    The pipeline checks it centrally — an enricher's `enrich` is only called when
    it applies, so it never re-decides whether to run. Enrichers must be pure:
    return a new draft via `draft.model_copy(update=...)`, never mutate the input.
    """

    # Capability mixin a contributor's message must be an instance of, or None.
    requires: type | None

    def enrich(self, draft: DerivedDraft) -> DerivedDraft: ...
