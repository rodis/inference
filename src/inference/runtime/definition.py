"""`EventDefinition` — an inference event expressed as data, not code.

This is the YAML-on-disk replacement for a worker's hand-written `main.py`
constants (`RULES`, `KAFKA_*`, `EVENT_DOMAIN`, `ENRICHERS`). The `name` field is
the source of truth for identity (replacing the directory-name rule): the emitted
`event_name`, Redis keys (`inference:<name>:*`), and the kebab `slug` used for the
Kafka consumer group all derive from it.
"""

import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

logger = logging.getLogger(__name__)


class EnricherSpec(BaseModel):
    """One enricher in the chain: a registry key + its constructor config.

    YAML accepts either a bare string (`- lineage`) or a single-key mapping with
    config (`- geo: {strategy: centroid}`); both normalize to this shape.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    config: dict = {}


class EventDefinition(BaseModel):
    """A single inference event, loaded from `events/<name>.yml`."""

    model_config = ConfigDict(extra="forbid")

    name: str                       # identity — snake_case; source of truth (was the directory name)
    enabled: bool = True            # skip-load toggle for quick experiments
    engine: str                     # registry key (e.g. "weighted_window")
    engine_config: dict = {}        # engine-specific (threshold, window_seconds, weights, ...)
    source_topics: list[str]
    sink_topic: str
    event_domain: str
    enrichers: list[EnricherSpec] = []

    @property
    def slug(self) -> str:
        """kebab-case form — infra layer (Kafka consumer group)."""
        return self.name.replace("_", "-")

    @field_validator("enrichers", mode="before")
    @classmethod
    def _normalize_enrichers(cls, value):
        # Accept ["lineage", {"geo": {...}}] and normalize each item to EnricherSpec shape.
        if not isinstance(value, list):
            return value
        out = []
        for item in value:
            if isinstance(item, str):
                out.append({"name": item, "config": {}})
            elif isinstance(item, dict) and len(item) == 1:
                (key, cfg), = item.items()
                out.append({"name": key, "config": cfg or {}})
            else:
                out.append(item)  # let EnricherSpec raise a clear error
        return out


def load_definitions(events_dir: Path) -> list[EventDefinition]:
    """Load every `*.yml` under `events_dir` into a validated `EventDefinition`.

    Best-effort and isolated: a malformed or disabled definition is logged and
    skipped, never fatal to the others (one bad experiment can't take the fleet
    down). Returns the enabled, valid definitions in filename order.
    """
    definitions: list[EventDefinition] = []
    for path in sorted(events_dir.glob("*.yml")):
        try:
            raw = yaml.safe_load(path.read_text()) or {}
            definition = EventDefinition.model_validate(raw)
        except (ValidationError, yaml.YAMLError) as e:
            logger.error("Skipping invalid event definition %s: %s", path.name, e)
            continue
        if not definition.enabled:
            logger.info("Skipping disabled event definition %s", definition.name)
            continue
        definitions.append(definition)
    logger.info("Loaded %d event definition(s): %s",
                len(definitions), [d.name for d in definitions])
    return definitions
