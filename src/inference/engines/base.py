"""Pluggable inference engines (strategies).

An `Engine` decides whether a derived event fires from an incoming event plus this
entity's state. Engines are resolved from a definition's `engine` string via the
registry, so strategies are swappable: a new one is a new `Engine` class +
`@register_engine(...)` + `engine: <name>` in a definition. The runtime
(`inference.runtime.quix`) is strategy-agnostic — it resolves engines, routes
events to them, and shapes/emits the result envelope.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Decision:
    """An engine's verdict that a derived event should fire.

    The runtime turns this into the emitted message (and stamps the entity key), so
    engines only *decide* — they don't shape envelopes/lineage. `contributors` are
    the source events that triggered it (shaped into `derived_from` lineage by the
    runtime); `score` is the engine's confidence metric.
    """

    occurred_at: float
    score: float
    contributors: tuple[dict, ...]   # each: {"event_name", "timestamp", "envelope_id"}


class ScopedState:
    """A per-definition view over the shared per-entity Quix `State`, prefixing keys
    with `<name>:` so many engines share one keyed store without colliding. Keeps
    engines ignorant of the sharing — they use plain keys like `"window"`.
    """

    def __init__(self, state, prefix: str):
        self._state = state
        self._prefix = prefix

    def get(self, key: str, default=None):
        return self._state.get(self._prefix + key, default)

    def set(self, key: str, value) -> None:
        self._state.set(self._prefix + key, value)


@runtime_checkable
class Engine(Protocol):
    name: str                                    # definition name — identity + state scope

    def input_event_names(self) -> set[str]:
        """Event names this engine consumes — drives the runtime's routing index."""
        ...

    def decide(self, event: dict, state: ScopedState) -> Decision | None:
        """Given an incoming event (envelope dict) and this definition's per-entity
        scoped state, return a `Decision` if the derived event fires, else `None`."""
        ...


# --- registry -----------------------------------------------------------------

_REGISTRY: dict[str, type] = {}


def register_engine(engine_type: str):
    """Class decorator registering an `Engine` under `engine_type`. The class is
    constructed as `cls(name=<definition name>, config=<engine_config>)`.
    """

    def _wrap(cls: type) -> type:
        _REGISTRY[engine_type] = cls
        return cls

    return _wrap


def build_engine(definition) -> Engine:
    """Resolve and construct the `Engine` for a definition (by its `engine` string)."""
    try:
        cls = _REGISTRY[definition.engine]
    except KeyError:
        raise RuntimeError(
            f"Unknown engine '{definition.engine}' for event '{definition.name}'. "
            f"Registered engines: {sorted(_REGISTRY)}"
        ) from None
    return cls(name=definition.name, config=definition.engine_config)
