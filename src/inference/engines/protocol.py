from typing import Protocol


class InferenceEngine(Protocol):
    def process(self, payload: dict) -> dict | None: ...
