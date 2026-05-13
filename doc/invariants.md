# Inference Worker — Invariants

Design rules that must hold across the entire codebase. When adding new engines, observers, or transport adapters, verify these still hold.

---

## Payload Structure

**`message` contains data. Everything outside `message` is metadata.**

```python
{
    "event_name": "...",          # metadata — routing/filtering hint
    "source_app": "shortcut",    # metadata
    "source_type": "http_server",# metadata
    "timestamp": "2026-...",     # metadata — ISO string, wall-clock arrival time
    "message": {
        "event_name": "...",     # data — canonical event identifier
        "timestamp": 1777673675, # data — Unix integer, used for windowing
        ...                      # data — event-specific fields
    }
}
```

Engines must read `event_name` and `timestamp` from `payload["message"]`, never from the top-level payload.

---

## Engine Contract

**`process()` returns `dict | None`. The engine never produces to Kafka.**

- `None` → no inference triggered; transport commits and moves on
- `dict` → inference triggered; transport is responsible for producing the result to `inference_topic`

The engine owns its internal state (Redis, windowing logic) and has no reference to the Kafka producer, consumer, or observer.

---

## Transport Contract

**The transport layer is unaware of Redis or any engine internals.**

`KafkaStreamHandler` receives a consumer, an engine, an observer, and an emitter. It does not know how the engine works — only that `process()` returns a result or `None`. It does not know where the emitter sends results — only that `emit()` accepts a dict.

---

## Commit Strategy

**Offsets are committed manually after every message, including errors.**

`enable.auto.commit=False`. The handler commits after:
- Successful processing (with or without an inference trigger)
- JSON decode errors (skip-and-move-on)
- Engine exceptions (skip-and-move-on)

A message is never retried indefinitely. If a message cannot be processed, it is logged and skipped.

---

## Cooldown Lock Atomicity

**The cooldown lock must be set atomically.**

`SET NX EX` (atomic) is used instead of a separate `EXISTS` + `SETEX`. This prevents two concurrent engine instances from both passing the threshold check and both emitting a duplicate inference within the same cooldown window.

---

## Configuration Source

**`config.py` emulates the Kubernetes ConfigMap. No runtime config lives in `main.py`.**

`main.py` only wires components together. All tunable values (topics, rules, credentials, Redis config, consumer group) live in `config.py` so they have a single production-equivalent source of truth.

---

## Dynamic Engine Loading

**The engine class is never imported directly in `main.py`.**

`load_class(config.ENGINE_CLASS)` resolves the engine at runtime from a fully qualified string. This allows swapping engine implementations via ConfigMap without code changes.
