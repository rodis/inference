"""Engine / enricher registries — the seam that keeps the runtime generic.

A concrete engine or enricher registers a *builder* under a string key (the same
key a definition's `engine` / enricher entry uses). The builder owns construction
of its own type (it knows its constructor), so the runtime never names a concrete
class. Mirrors the message registry in `events/messages.py`.

Builders are registered at import time, so the entrypoint must import the modules
that hold the concrete engines/enrichers before loading definitions (importing
`inference.engines.weighted_window` and `inference.pipeline.enrichers` is enough).
"""

from typing import Any, Callable, Protocol


class _Definition(Protocol):
    name: str
    engine_config: dict


# key -> (definition) -> engine instance
ENGINE_BUILDERS: dict[str, Callable[[_Definition], Any]] = {}
# key -> (config dict) -> enricher instance
ENRICHER_BUILDERS: dict[str, Callable[[dict], Any]] = {}


def register_engine(key: str):
    """Register an engine builder under `key` (e.g. "weighted_window")."""

    def _wrap(builder: Callable[[_Definition], Any]) -> Callable[[_Definition], Any]:
        ENGINE_BUILDERS[key] = builder
        return builder

    return _wrap


def register_enricher(key: str):
    """Register an enricher builder under `key` (e.g. "lineage", "geo")."""

    def _wrap(builder: Callable[[dict], Any]) -> Callable[[dict], Any]:
        ENRICHER_BUILDERS[key] = builder
        return builder

    return _wrap


def engine_builder(key: str) -> Callable[[_Definition], Any]:
    try:
        return ENGINE_BUILDERS[key]
    except KeyError:
        raise KeyError(
            f"Unknown engine '{key}'. Registered: {sorted(ENGINE_BUILDERS)}. "
            "Did the entrypoint import the engine module so it could register?"
        ) from None


def enricher_builder(key: str) -> Callable[[dict], Any]:
    try:
        return ENRICHER_BUILDERS[key]
    except KeyError:
        raise KeyError(
            f"Unknown enricher '{key}'. Registered: {sorted(ENRICHER_BUILDERS)}. "
            "Did the entrypoint import the enricher module so it could register?"
        ) from None
