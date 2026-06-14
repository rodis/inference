from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class Envelope(BaseModel):
    """Metadata-wrapped event as published to Kafka by Vector.

    Vector wraps every raw HTTP event in this envelope before it reaches the
    worker. The data lives in `message`; everything else is metadata. Engines
    must read from `message` only (see doc/invariants.md).

    `envelope_id` is the stable per-event identity used for lineage and (later)
    persistence. Vector mints it at ingest; the `default_factory` is a fallback
    so an event lacking one still parses (a worker-minted id is stable within
    this process but not across consumers — Vector's is authoritative).
    """

    event_name: str
    source_app: str
    source_type: str
    timestamp: datetime
    envelope_id: UUID = Field(default_factory=uuid4)
    message: dict
