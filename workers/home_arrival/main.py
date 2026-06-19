import logging
from pathlib import Path

from confluent_kafka import Consumer

from inference import config
from inference.engines.weighted_window import WeightedWindowEngine
from inference.observers.logging_observer import InferenceObserver
from inference.pipeline.enrichers import GeoEnricher, LineageEnricher
from inference.pipeline.runner import EnrichmentPipeline
from inference.transport.kafka_handler import KafkaStreamHandler
from inference.transport.vector_http_emitter import VectorHttpEmitter


WORKER_NAME = Path(__file__).parent.name        # snake_case — data layer (Redis keys, payloads, logs)
WORKER_SLUG = WORKER_NAME.replace("_", "-")     # kebab-case — infra layer (K8s, Docker, Kafka group)

RULES = {
    "name": WORKER_NAME,
    "threshold": 10,
    "window_seconds": 600,
    "cooldown_seconds": 10,
    "weights": {
        "car_lock_state_change": 4,
        "device_disconnected_from_power": 3,
        "device_disconnected_from_carplay": 4,
        #"connect_to_home_wifi": 7,
    },
}

# Ordered enricher chain — shapes the message after the engine decides + assembles
# the core. Per-worker config, like RULES: this list sets availability + order +
# config. Whether each enricher *applies* is decided by the pipeline from the
# enricher's declared `requires` capability against the contributors' messages.
ENRICHERS = [
    LineageEnricher(),                  # requires=None → always (derived_from)
    GeoEnricher(strategy="centroid"),   # requires=GeoLocated → only if a contributor is geolocated
]

EVENT_DOMAIN = "sensors"
APPLICATION = WORKER_NAME

KAFKA_CONSUMER_GROUP = f"inference-{WORKER_SLUG}-v1"
KAFKA_SOURCE_TOPICS = ["raw_sensors"]
KAFKA_SINK_TOPIC = "high_level_events"


if __name__ == "__main__":
    # Logging is configured here, not in library modules, to avoid side effects on import.
    # Both the Observer (named logger per engine) and VectorHttpEmitter (logger per module)
    # inherit this root configuration automatically.
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    kafka_consumer = Consumer(
        {
            "bootstrap.servers": config.KAFKA_BOOTSTRAP_SERVERS,
            "security.protocol": "SSL",
            "ssl.ca.location": config.KAFKA_SSL_CA_PATH,
            "ssl.certificate.location": config.KAFKA_SSL_CERT_PATH,
            "ssl.key.location": config.KAFKA_SSL_KEY_PATH,
            "group.id": KAFKA_CONSUMER_GROUP,
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    )

    observer = InferenceObserver(RULES["name"])
    emitter = VectorHttpEmitter(
        url=f"{config.VECTOR_BASE_URL}/{EVENT_DOMAIN}/{APPLICATION}/{KAFKA_SINK_TOPIC}"
    )

    engine = WeightedWindowEngine(rules=RULES)
    pipeline = EnrichmentPipeline(enrichers=ENRICHERS)

    stream_handler = KafkaStreamHandler(
        kafka_consumer=kafka_consumer,
        engine=engine,
        observer=observer,
        emitter=emitter,
        pipeline=pipeline,
    )
    stream_handler.start(source_topics=KAFKA_SOURCE_TOPICS)
