# Inference Worker — Architecture

## Overview

Each inference worker is a standalone Python process deployed as a Kubernetes pod. It reads raw events from a Kafka topic, evaluates them against an inference engine, and publishes high-level inference events to a second Kafka topic when a threshold is met.

```
event_producer
    │  HTTP POST (raw event body)
    ▼
 Vector (http_server source)  ← wraps event in metadata envelope, forwards to Kafka
    │
    ▼
Kafka (raw_sensors)
    │
    ▼
 KafkaStreamHandler           ← transport layer; owns consumer loop + commit strategy
    │
    ▼
  InferenceEngine.decide()    ← pluggable: decides + assembles the core (+ contributors)
    │
 DerivedDraft | None
    │
    ▼ (if not None)
  EnrichmentPipeline          ← ordered enrichers shape the message (lineage, geo, …)
    │  finalize() → dict
    ▼
  Emitter.emit()              ← pluggable output target
    │  HTTP POST (inference result)
    ▼
 Vector (http_server source)  ← routes result to Kafka
    │
    ▼
Kafka (high_level_events)
```

## Payload Shape

Vector wraps each incoming HTTP event in an envelope before publishing to Kafka. This defines the payload structure every engine receives:

```python
{
    "event_name": "...",           # routing hint added by Vector
    "source_app": "shortcut",      # metadata: which app produced the event
    "source_type": "http_server",  # metadata: Vector source type
    "timestamp": "2026-05-01T...", # metadata: ISO wall-clock time of Vector ingestion
    "message": {                   # data: the original event body from the producer
        "event_name": "...",       #   canonical event identifier
        "timestamp": 1777673675,   #   Unix integer from the producer
        ...                        #   event-specific fields
    }
}
```

Engines must read from `message` only. See `doc/invariants.md`.

## Deployment Model

- One pod per inference type (e.g., `car_door_opened`, `car_departure`)
- Engine class and rules are baked into each worker's `main.py` (see the **Configuration Source** invariant)
- Cluster-shared infra env vars (`KAFKA_BOOTSTRAP_SERVERS`, `VECTOR_BASE_URL`) come from a Kubernetes ConfigMap
- Credentials are injected via Kubernetes Secrets: Kafka mTLS certs are mounted as files and read by `config.py`; Redis credentials (`REDIS_HOST`/`PORT`/`DB`/`USERNAME`/`PASSWORD`) are mounted as env vars and read directly by the engine
- Locally, all env vars come from `workers/.env` (loaded via `dotenv` at `config.py` import)
- Redis is used for distributed windowed state — multiple replicas of the same engine share the same ZSET buffer and cooldown lock

## Data Flow

1. `KafkaStreamHandler` polls `source_topics` in a blocking loop
2. Each message is parsed into an `Envelope` and passed to `InferenceEngine.decide()`
3. If `decide()` returns a `DerivedDraft`, the worker runs it through the `EnrichmentPipeline` (which shapes the message and `finalize()`s it to a dict), logs it via `Observer`, and forwards it to the sink via `Emitter.emit()` (HTTP POST to Vector → Kafka `high_level_events`)
4. The consumer offset is manually committed after every message (success or skip)

See `doc/adr/0001-message-shaping-pipeline.md` for the decide → enrich → emit design.

## Components

| Component | Location | Responsibility |
|---|---|---|
| `KafkaStreamHandler` | `transport/kafka_handler.py` | Kafka consumer loop, signal handling, commit strategy; runs decide → pipeline → emit |
| `InferenceEngine` | `engines/protocol.py` | Protocol defining the engine contract (`decide() -> DerivedDraft \| None`) |
| `WeightedWindowEngine` | `engines/weighted_window.py` | Time-windowed weighted threshold inference (decider; engine-private Redis state) |
| `DerivedDraft` | `pipeline/draft.py` | Neutral carrier the engine produces (core + `contributors: tuple[Envelope]`) |
| `Enricher` | `pipeline/protocol.py` | Protocol for a single-capability message shaper |
| `EnrichmentPipeline` / `finalize` | `pipeline/runner.py` | Folds enrichers over the draft, finalizes to the transport dict |
| `LineageEnricher` / `GeoEnricher` | `pipeline/enrichers/` | `derived_from` (always) / `location` (if contributors geolocated) |
| `Observer` | `observers/protocol.py` | Protocol defining the observer contract |
| `InferenceObserver` | `observers/logging_observer.py` | Structured logging implementation |
| `Emitter` | `transport/protocol.py` | Protocol defining the emitter contract |
| `VectorHttpEmitter` | `transport/vector_http_emitter.py` | HTTP POST to Vector |
| `config.py` | `src/inference/config.py` | Reads cluster-shared infra env vars (Kafka, Vector) sourced from ConfigMap in prod / `workers/.env` locally |
| `workers/car_door_opened/main.py` | `workers/car_door_opened/main.py` | Worker entrypoint — wires all components together |

## Configuration

Three layers, each owning a different scope (see `doc/invariants.md` → **Configuration Source** and **Engine-Owned Infrastructure**):

### `src/inference/config.py` — shared infra used by the wiring layer

Cluster-wide values, sourced from env vars / K8s Secrets, identical across every worker:

| Key | Description |
|---|---|
| `KAFKA_BOOTSTRAP_SERVERS` | Kafka broker list |
| `KAFKA_SSL_CA_PATH` / `KAFKA_SSL_CERT_PATH` / `KAFKA_SSL_KEY_PATH` | Kafka mTLS cert paths (default to K8s Secret mount locations) |
| `VECTOR_BASE_URL` | Base URL of the Vector instance that receives inference results |

### `workers/<name>/main.py` — per-worker config

Each worker imports its engine class directly and declares its own `RULES`, `KAFKA_SOURCE_TOPICS`, `KAFKA_SINK_TOPIC`, `EVENT_DOMAIN`. These are worker-specific with no shared default. The worker's identity is derived from the directory name in two forms: `WORKER_NAME = Path(__file__).parent.name` (snake_case, used for `RULES["name"]`, `APPLICATION`, and other data-layer references) and `WORKER_SLUG = WORKER_NAME.replace("_", "-")` (kebab-case, used in `KAFKA_CONSUMER_GROUP` and other infra-layer identifiers). See `doc/invariants.md` for the full rule.

### Engine module — engine-internal infra

The engine resolves its own backend connection from env vars. `WeightedWindowEngine` reads `REDIS_HOST` / `REDIS_PORT` / `REDIS_DB` / `REDIS_USERNAME` / `REDIS_PASSWORD` via `_redis_config_from_env()` inside `weighted_window.py`. Other engines may use different backends; the worker layer never plumbs them through.
