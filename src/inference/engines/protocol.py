from typing import Protocol

from inference.events import Envelope


class InferenceEngine(Protocol):
    def process(self, payload: Envelope) -> dict | None: ...
