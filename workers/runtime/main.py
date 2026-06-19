"""Generic inference runtime (ADR 0003).

Replaces the per-event `workers/<name>/main.py`. Loads every event definition
from `events/*.yml` and runs one handler per definition in a single process.
Adding/retiring an event is a YAML change, not a new pod.

Pure wiring — no inference logic. The two engine/enricher imports below are here
(not in the framework) to register their builders into the runtime registries
before definitions are resolved; that keeps `src/inference` free of concrete
engine/enricher names (engines stay swappable).
"""

import logging
import os
from pathlib import Path

from inference import config

# Import for side effect: each module registers its builder into the registries.
import inference.engines.weighted_window  # noqa: F401
import inference.pipeline.enrichers  # noqa: F401  (imports geo + lineage)

from inference.runtime.builder import KafkaSettings, build_handler
from inference.runtime.definition import load_definitions
from inference.runtime.supervisor import RuntimeSupervisor


# Definitions live at the repo root `events/` by default; overridable for the
# container image / local runs via EVENTS_DIR.
EVENTS_DIR = Path(os.environ.get("EVENTS_DIR", Path(__file__).resolve().parents[2] / "events"))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger = logging.getLogger("runtime")

    kafka = KafkaSettings(
        bootstrap_servers=config.KAFKA_BOOTSTRAP_SERVERS,
        ssl_ca_path=config.KAFKA_SSL_CA_PATH,
        ssl_cert_path=config.KAFKA_SSL_CERT_PATH,
        ssl_key_path=config.KAFKA_SSL_KEY_PATH,
    )

    definitions = load_definitions(EVENTS_DIR)
    handlers = [
        build_handler(d, kafka=kafka, vector_base_url=config.VECTOR_BASE_URL)
        for d in definitions
    ]
    logger.info("Built %d handler(s); starting runtime.", len(handlers))

    RuntimeSupervisor(handlers).run()
