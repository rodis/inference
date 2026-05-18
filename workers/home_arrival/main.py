import logging

from confluent_kafka import Consumer

from inference import config
from inference.observers.logging_observer import InferenceObserver
from inference.transport.kafka_handler import KafkaStreamHandler
from inference.transport.vector_http_emitter import VectorHttpEmitter
from inference.utils import load_class


ENGINE_CLASS = "inference.engines.weighted_window.WeightedWindowEngine"
RULES = {
    "name": "home_arrival",
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

EVENT_DOMAIN = "sensors"
APPLICATION = "home_arrival"

KAFKA_CONSUMER_GROUP = "inference-engine-v1"
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

    engine_class = load_class(ENGINE_CLASS)
    engine = engine_class(rules=RULES)

    stream_handler = KafkaStreamHandler(
        kafka_consumer=kafka_consumer,
        engine=engine,
        observer=observer,
        emitter=emitter,
    )
    stream_handler.start(source_topics=KAFKA_SOURCE_TOPICS)
