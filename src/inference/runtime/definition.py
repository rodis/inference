"""`EventDefinition` — an inference event expressed as data, not code.

The YAML-on-disk schema (`events/<name>.yml`) that the Quix runtime
([`inference.runtime.quix`](quix.py)) loads. The `name` field is the source of
truth for identity: it is the emitted event `name`, the key its per-entity state is
scoped under, and the key the router uses to route a fired event to its sink. (The
emitted `inference_type` is the engine *type*, not this name.)
"""

import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from inference.event import Capability

logger = logging.getLogger(__name__)


class EventDefinition(BaseModel):
    """A single inference event, loaded from `events/<name>.yml`."""

    model_config = ConfigDict(extra="forbid")

    name: str                       # identity — snake_case; emitted event name + state scope (inference_type is the engine type)
    enabled: bool = True            # skip-load toggle for quick experiments
    engine: str                     # engine type: weighted_window | decaying_window | naive_bayes_window
    engine_config: dict = {}        # engine-specific (threshold, window_seconds, cooldown_seconds, weights)
    source_topic: str               # external topic the raw contributors arrive on (one per ADR 0004)
    sink_topic: str                 # where the derived event is produced

    # Structured facts to derive from the event's evidence (e.g. interval). Data-model
    # only — presentation (span/point/hidden) is a consumer concern, not declared here.
    capabilities: list[Capability] = []


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
