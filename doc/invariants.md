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
- `dict` → inference triggered; transport is responsible for forwarding the result to the sink (via the Emitter)

The engine owns its internal state (Redis, windowing logic) and has no reference to the Kafka producer, consumer, or observer.

---

## Engine-Owned Infrastructure

**Engines own their storage and connection dependencies. The worker layer never plumbs them through.**

If an engine needs Redis, Postgres, or any other backend, it resolves that connection itself (typically via a helper in the engine module that reads env vars). `config.py` exposes only the infrastructure that the wiring layer needs directly — Kafka, SSL certs, Vector. Engine-internal config (e.g. `REDIS_*` env vars) is read by the engine itself and never appears in `config.py` or in `main.py`.

**Why:** Different engines may use different backends. Forcing every backend through `config.py` and `main.py` creates a leaky abstraction where the worker has to know each engine's internal implementation. Swapping engines should be a localized change to the worker's import + instantiation lines — not a ripple through shared config.

**How to apply:** A worker constructing an engine should never pass connection config (`redis_config=...`, `db_url=...`, etc.). It passes only the engine's *logical* config — rules, thresholds, weights. For testability, engine `__init__` may accept an optional override (`redis_config: dict | None = None`); production code passes nothing.

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

**Shared infra config lives in `config.py`. Per-worker config lives in `workers/<name>/main.py`. Engine-internal infra lives in the engine module.**

- `config.py` holds only the cluster-shared infrastructure that the wiring layer touches directly: Kafka bootstrap servers, SSL cert paths, Vector base URL. Sourced from env vars / K8s Secrets and identical across all workers.
- Each worker's `main.py` imports its engine class directly and declares its own per-worker config: `RULES`, source/sink topics, consumer group, event domain. Worker-specific, no meaningful shared default.
- The worker's identity — `RULES["name"]` and `APPLICATION` — must be derived from the directory name via `WORKER_NAME = Path(__file__).parent.name`, never declared as a literal. The directory layout is the source of truth; this prevents copy-paste drift across workers.
- Engine-internal infra (Redis connection, future Postgres connection, etc.) lives in the engine module — see **Engine-Owned Infrastructure**. It must not appear in `config.py` or `main.py`.

`main.py` is still only wiring — it does not contain logic. The per-worker constants at the top of the file are the worker's ConfigMap-equivalent.
