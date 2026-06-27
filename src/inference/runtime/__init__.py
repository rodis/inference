"""Generic, definition-driven Quix Streams runtime (ADR 0004).

Loads `EventDefinition`s (from `events/*.yml`) and runs them all on one Quix
`Application` — see `inference.runtime.quix`. The definition `name` remains the
source of truth for event identity.
"""

from inference.runtime.definition import EnricherSpec, EventDefinition, load_definitions

__all__ = [
    "EventDefinition",
    "EnricherSpec",
    "load_definitions",
]
