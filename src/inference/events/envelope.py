from datetime import datetime

from pydantic import BaseModel


class Envelope(BaseModel):
    """Metadata-wrapped event as published to Kafka by Vector.

    Vector wraps every raw HTTP event in this envelope before it reaches the
    worker. The data lives in `message`; everything else is metadata. Engines
    must read from `message` only (see doc/invariants.md).
    """

    event_name: str
    source_app: str
    source_type: str
    timestamp: datetime
    message: dict
