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
- Engine class and rules are injected at runtime via Kubernetes ConfigMap
- Credentials (Kafka SSL, Redis) are injected via Kubernetes Secrets (currently hardcoded in `config.py` for local testing)
- Redis is used for distributed windowed state — multiple replicas of the same engine share the same ZSET buffer and cooldown lock

## Data Flow

1. `KafkaStreamHandler` polls `source_topics` in a blocking loop
2. Each message is decoded from JSON and passed to `InferenceEngine.process()`
3. If `process()` returns a result dict, it is logged via `Observer` and produced to `inference_topic`
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
| `config.py` | `src/inference/config.py` | ConfigMap emulation for local development |
| `workers/home_arrival/main.py` | `workers/home_arrival/main.py` | Worker entrypoint — wires all components together |

## Configuration (`config.py`)

`config.py` emulates values that would be injected via Kubernetes ConfigMap in production. It is not committed with real credentials in production deployments.

| Key | Description |
|---|---|
| `ENGINE_CLASS` | Fully qualified class name, loaded dynamically via `load_class()` |
| `SOURCE_TOPICS` | List of Kafka topics to consume from |
| `CONSUMER_GROUP` | Kafka consumer group ID |
| `VECTOR_URL` | HTTP endpoint of the Vector instance that receives inference results |
| `REDIS_CONFIG` | Redis connection parameters |
| `RULES` | Engine-specific configuration (weights, threshold, window, cooldown) |
