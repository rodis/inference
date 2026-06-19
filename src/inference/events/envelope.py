from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, SerializeAsAny, model_validator

from inference.events.messages import MessageBase, resolve_message_type


class Envelope(BaseModel):
    """Metadata-wrapped event as published to Kafka by Vector.

    Vector wraps every raw HTTP event in this envelope before it reaches the
    worker. The data lives in `message` (a typed `MessageBase`, resolved from
    `event_name` via the registry); everything else is metadata. Engines read
    from `message` only (see doc/invariants.md).

    `envelope_id` is the stable per-event identity (Vector-minted; `default_factory`
    fallback). `message` is typed `SerializeAsAny[MessageBase]` — `SerializeAsAny`
    is required so a concrete subclass's fields (e.g. `location`, `derived_from`)
    survive `model_dump_json` (the engine round-trips contributors through Redis).
    """

    event_name: str
    source_app: str
    source_type: str
    timestamp: datetime
    envelope_id: UUID = Field(default_factory=uuid4)
    message: SerializeAsAny[MessageBase]

    @model_validator(mode="before")
    @classmethod
    def _coerce_message(cls, data):
        # Resolve a raw `message` dict to its concrete class by event_name.
        # A message already given as a MessageBase instance is left untouched.
        if isinstance(data, dict):
            raw = data.get("message")
            if isinstance(raw, dict):
                target = resolve_message_type(raw.get("event_name", ""))
                data = {**data, "message": target.model_validate(raw)}
        return data
