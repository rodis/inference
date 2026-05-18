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
  InferenceEngine.process()   ← pluggable inference logic
    │
 dict | None
    │
    ▼ (if not None)
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

- One pod per inference type (e.g., `home_arrival`, `car_departure`)
- Engine class and rules are baked into each worker's `main.py` (see the **Configuration Source** invariant)
- Cluster-shared infra env vars (`KAFKA_BOOTSTRAP_SERVERS`, `VECTOR_BASE_URL`) come from a Kubernetes ConfigMap
- Credentials are injected via Kubernetes Secrets: Kafka mTLS certs are mounted as files and read by `config.py`; Redis credentials (`REDIS_HOST`/`PORT`/`DB`/`USERNAME`/`PASSWORD`) are mounted as env vars and read directly by the engine
- Locally, all env vars come from `workers/.env` (loaded via `dotenv` at `config.py` import)
- Redis is used for distributed windowed state — multiple replicas of the same engine share the same ZSET buffer and cooldown lock

## Data Flow

1. `KafkaStreamHandler` polls `source_topics` in a blocking loop
2. Each message is decoded from JSON and passed to `InferenceEngine.process()`
3. If `process()` returns a result dict, it is logged via `Observer` and forwarded to the sink via `Emitter.emit()` (HTTP POST to Vector → Kafka `high_level_events`)
4. The consumer offset is manually committed after every message (success or skip)

## Components

| Component | Location | Responsibility |
|---|---|---|
| `KafkaStreamHandler` | `transport/kafka_handler.py` | Kafka consumer loop, signal handling, commit strategy |
| `InferenceEngine` | `engines/protocol.py` | Protocol defining the engine contract |
| `WeightedWindowEngine` | `engines/weighted_window.py` | Time-windowed weighted threshold inference |
| `Observer` | `observers/protocol.py` | Protocol defining the observer contract |
| `InferenceObserver` | `observers/logging_observer.py` | Structured logging implementation |
| `Emitter` | `transport/protocol.py` | Protocol defining the emitter contract |
| `VectorHttpEmitter` | `transport/vector_http_emitter.py` | HTTP POST to Vector |
| `config.py` | `src/inference/config.py` | Reads cluster-shared infra env vars (Kafka, Vector) sourced from ConfigMap in prod / `workers/.env` locally |
| `workers/home_arrival/main.py` | `workers/home_arrival/main.py` | Worker entrypoint — wires all components together |

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

Each worker imports its engine class directly and declares its own `RULES`, `KAFKA_SOURCE_TOPICS`, `KAFKA_SINK_TOPIC`, `KAFKA_CONSUMER_GROUP`, `EVENT_DOMAIN`. These are worker-specific with no shared default. The worker's identity (`RULES["name"]` and `APPLICATION`) is derived from the directory name via `WORKER_NAME = Path(__file__).parent.name`, not declared as a literal.

### Engine module — engine-internal infra

The engine resolves its own backend connection from env vars. `WeightedWindowEngine` reads `REDIS_HOST` / `REDIS_PORT` / `REDIS_DB` / `REDIS_USERNAME` / `REDIS_PASSWORD` via `_redis_config_from_env()` inside `weighted_window.py`. Other engines may use different backends; the worker layer never plumbs them through.
