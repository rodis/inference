"""Build a runnable handler from an `EventDefinition`.

Produces exactly what a worker `main.py` used to assemble by hand — engine,
enrichment pipeline, observer, Vector emitter, Kafka consumer, wired into a
`KafkaStreamHandler` — but resolved from the definition + registries. Infra
values (Kafka, Vector) are passed in by the caller so this stays testable; the
engine still reads its own backend (Redis) config from env per the
Engine-Owned Infrastructure rule.
"""

from dataclasses import dataclass

from confluent_kafka import Consumer

from inference.observers.logging_observer import InferenceObserver
from inference.pipeline.runner import EnrichmentPipeline
from inference.runtime.definition import EventDefinition
from inference.runtime.registry import engine_builder, enricher_builder
from inference.transport.kafka_handler import KafkaStreamHandler
from inference.transport.vector_http_emitter import VectorHttpEmitter


@dataclass(frozen=True)
class KafkaSettings:
    bootstrap_servers: str
    ssl_ca_path: str
    ssl_cert_path: str
    ssl_key_path: str


@dataclass(frozen=True)
class Handler:
    """A built handler plus the topics it should subscribe to."""

    name: str
    stream_handler: KafkaStreamHandler
    source_topics: list[str]


def build_engine(definition: EventDefinition):
    return engine_builder(definition.engine)(definition)


def build_pipeline(definition: EventDefinition) -> EnrichmentPipeline:
    enrichers = [enricher_builder(spec.name)(spec.config) for spec in definition.enrichers]
    return EnrichmentPipeline(enrichers=enrichers)


def build_emitter(definition: EventDefinition, vector_base_url: str) -> VectorHttpEmitter:
    # Same URL shape as the old main.py: {base}/{domain}/{application}/{sink}.
    # `application` is the event name (identity), per doc/invariants.md.
    url = f"{vector_base_url}/{definition.event_domain}/{definition.name}/{definition.sink_topic}"
    return VectorHttpEmitter(url=url)


def build_consumer(definition: EventDefinition, kafka: KafkaSettings) -> Consumer:
    return Consumer(
        {
            "bootstrap.servers": kafka.bootstrap_servers,
            "security.protocol": "SSL",
            "ssl.ca.location": kafka.ssl_ca_path,
            "ssl.certificate.location": kafka.ssl_cert_path,
            "ssl.key.location": kafka.ssl_key_path,
            "group.id": f"inference-{definition.slug}-v1",
            "enable.auto.commit": False,
            # Start a brand-new group at the TAIL, not the beginning. Only applies
            # when the group has no committed offset — i.e. a newly added event, or
            # a new topic added to an existing group. Existing handlers keep their
            # committed positions and are unaffected. This avoids replaying all
            # history on every new event (which the wall-clock cooldown vs event-time
            # window collapses into junk fires + "invalid envelope" log spam). The
            # trade-off — a new event doesn't backfill its window from history — is
            # desired; replay-backfill was useless anyway. Mirrors Vector's
            # vector-neon-persister source (auto_offset_reset: latest).
            "auto.offset.reset": "latest",
        }
    )


def build_handler(
    definition: EventDefinition,
    *,
    kafka: KafkaSettings,
    vector_base_url: str,
) -> Handler:
    stream_handler = KafkaStreamHandler(
        kafka_consumer=build_consumer(definition, kafka),
        engine=build_engine(definition),
        observer=InferenceObserver(definition.name),
        emitter=build_emitter(definition, vector_base_url),
        pipeline=build_pipeline(definition),
    )
    return Handler(
        name=definition.name,
        stream_handler=stream_handler,
        source_topics=list(definition.source_topics),
    )
