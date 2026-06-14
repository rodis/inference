from pydantic import BaseModel, ConfigDict, Field

from inference.events import Envelope


class DerivedDraft(BaseModel):
    """Neutral, progressively-shaped carrier for a derived (inference) event.

    The engine fills the core (`event_name`, `confidence_score`, `occurred_at`)
    plus `contributors` — the source events that triggered it, as their full
    `Envelope`s so enrichers have complete context (message body for geo, the
    `envelope_id` for lineage once Vector mints it, source metadata, …).
    Enrichers accrete capability output into `fields` via `model_copy(update=...)`.
    It is NOT the final typed message — it is serialized to a transport dict (or
    validated into a typed message) at `finalize()`.
    """

    model_config = ConfigDict(frozen=True)

    inference_type: str
    event_name: str
    confidence_score: float
    occurred_at: float
    contributors: tuple[Envelope, ...]
    fields: dict = Field(default_factory=dict)
