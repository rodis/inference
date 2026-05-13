# Inference Worker — Class Reference

## Protocols

### `Emitter` — `transport/protocol.py`

Structural protocol for result emission. Any class implementing `emit()` satisfies it — no inheritance required.

```python
def emit(self, event: dict) -> None
```

The transport layer calls `emit()` when the engine returns a result. The emitter has no knowledge of Kafka or the consumer.

---

### `InferenceEngine` — `engines/protocol.py`

Structural protocol. Any class implementing `process()` satisfies it — no inheritance required.

```python
def process(self, payload: dict) -> dict | None
```

Returns an inference result dict when the engine triggers, `None` when it does not. The transport layer handles emission; the engine has no knowledge of Kafka.

---

### `Observer` — `observers/protocol.py`

Structural protocol for observing engine lifecycle events.

```python
def on_received(self, payload: dict) -> None
def on_inference(self, result: dict) -> None
def on_error(self, error: Exception, context: str | None = None) -> None
```

---

## Concrete Classes

### `WeightedWindowEngine` — `engines/weighted_window.py`

Time-windowed, weighted threshold inference engine backed by Redis.

**Constructor:**
```python
WeightedWindowEngine(rules: dict, redis_config: dict)
```

**`rules` keys:**

| Key | Type | Description |
|---|---|---|
| `name` | `str` | Engine identifier; used as Redis key prefix and inference type |
| `weights` | `dict[str, int]` | Map of event name → weight |
| `threshold` | `int` | Minimum total score to trigger an inference |
| `window_seconds` | `int` | How far back to look for contributing events |
| `cooldown_seconds` | `int` | How long to suppress re-triggering after a successful inference (default: 1800) |

**Algorithm:**
1. Drop the event if `event_name` is not in `weights` (no Redis hit)
2. Add the event to a Redis ZSET scored by timestamp
3. Prune entries older than `window_seconds`
4. Fetch all active entries; deduplicate by event type keeping the earliest occurrence
5. Sum weights of unique contributors
6. If score ≥ threshold and no cooldown lock is active, emit and set the lock atomically via `SET NX EX`

**Result dict** (follows the metadata/data invariant — see `doc/invariants.md`):

```python
{
    "inference_type": "...",   # metadata — identifies the inference event type
    "processed_at": 1234.0,   # metadata — wall-clock time of the trigger
    "message": {
        "confidence_score": 12,           # total weight of contributing events
        "occurred_at": 1777673675.0,      # average timestamp of unique contributors
        "sources": ["event_a", "event_b"],# contributing event type names
        "evidence": {                     # full trace for debugging / Gold Layer
            "event_a": 1777673670.0,
            "event_b": 1777673675.0,
        },
    },
}
```

---

### `KafkaStreamHandler` — `transport/kafka_handler.py`

Drives the Kafka consumer loop and delegates to the engine and emitter.

**Constructor:**
```python
KafkaStreamHandler(
    kafka_consumer: Consumer,
    engine: InferenceEngine,
    observer: Observer,
    emitter: Emitter,
)
```

**`start(source_topics: list[str])`** — blocking; exits cleanly on SIGTERM/SIGINT.

Commit strategy: `enable.auto.commit=False`. Offsets are committed manually after every message — including malformed messages, engine failures, and emit failures (skip-and-move-on).

---

### `InferenceObserver` — `observers/logging_observer.py`

Logging implementation of the `Observer` protocol. Uses Python's standard `logging` module with K8s-friendly structured output.

**Constructor:**
```python
InferenceObserver(name: str)
```

`name` becomes the logger name — use `rules["name"]` so log lines identify which engine worker produced them.

---

### `VectorHttpEmitter` — `transport/vector_http_emitter.py`

Emitter implementation that HTTP POSTs the inference result to a Vector `http_server` source.

**Constructor:**
```python
VectorHttpEmitter(url: str)
```

`url` is the Vector endpoint (e.g. `http://vector:8080`). Uses `urllib.request` with a 5-second timeout. Non-2xx responses raise an exception, which `KafkaStreamHandler` catches and logs as `"Emit failed"` before committing the offset and continuing.
