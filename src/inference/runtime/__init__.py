"""Generic, definition-driven runtime.

Loads `EventDefinition`s (from YAML) and builds the same engine + pipeline +
observer + emitter + Kafka consumer that a hand-written worker `main.py` wired
before — one handler per definition, supervised in a single process.

The runtime names no concrete engine or enricher: engines/enrichers register
themselves into the registries here (see `register_engine` / `register_enricher`),
and the runtime resolves them by the string key a definition declares. This keeps
framework code free of concrete-implementation names (engines stay swappable).
"""

from inference.runtime.definition import EventDefinition, EnricherSpec, load_definitions
from inference.runtime.registry import (
    register_engine,
    register_enricher,
    engine_builder,
    enricher_builder,
)

__all__ = [
    "EventDefinition",
    "EnricherSpec",
    "load_definitions",
    "register_engine",
    "register_enricher",
    "engine_builder",
    "enricher_builder",
]
