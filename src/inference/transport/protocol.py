from typing import Protocol


class Emitter(Protocol):
    def emit(self, event: dict) -> None: ...
