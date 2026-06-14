from typing import Protocol

from inference.events import Envelope
from inference.pipeline.draft import DerivedDraft


class InferenceEngine(Protocol):
    def decide(self, payload: Envelope) -> DerivedDraft | None: ...
