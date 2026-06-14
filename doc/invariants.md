# Inference Worker — Invariants

Design rules that must hold across the entire codebase. When adding new engines, observers, or transport adapters, verify these still hold.

---

## Payload Structure

**`message` contains data. Everything outside `message` is metadata.**

The inbound schema is enforced by the pydantic `Envelope` model in `src/inference/events/envelope.py`. Vector wraps every event in this shape before publishing to Kafka:

```python
Envelope(
    event_name="...",          # metadata — routing/filtering hint
    source_app="shortcut",    # metadata
    source_type="http_server",# metadata — Vector source type
    timestamp=datetime(...),  # metadata — wall-clock time of Vector ingestion
    envelope_id=UUID(...),    # metadata — stable per-event id, minted by Vector at ingest
    message={...},            # data — the original event body (untyped dict)
)
```

`message` contains the canonical data fields:
- `event_name: str` — canonical event identifier
- `timestamp: int` — Unix integer, used for windowing

Engines must read `event_name` and `timestamp` from `payload.message`, never from the envelope-level fields. Envelope-level `event_name` and `timestamp` are transport/routing metadata only.

**`envelope_id`:** minted by Vector's `classify_domain` transform (`uuid_v4()`) for every event at ingest — the stable identity used for lineage (`derived_from` joins on it) and, later, persistence. The model carries a `default_factory` fallback so an event without one still parses, but Vector's id is authoritative. `message` is still an untyped `dict`; typed per-event models remain a later step.

`message` is an untyped `dict` for now; typed per-event message models are a deliberate next step, not part of this change.

---

## Engine Contract

**`decide()` accepts an `Envelope` and returns a `DerivedDraft | None`. The engine decides and assembles the core; it does not shape the message.**

- `None` → no inference triggered; transport commits and moves on
- `DerivedDraft` → inference triggered. The draft carries only the **core** (`event_name`, `confidence_score`, `occurred_at`) plus the **contributors** (the source events, with their full bodies). The engine does *not* build `sources`/`evidence`/`location` or any capability-specific shape.

`InferenceEngine` is a swappable Protocol — `WeightedWindowEngine` is one implementation; others (e.g. a Bayesian engine) may follow. The gatekeeper, time window, weights, cooldown lock, and Redis are implementation details of `WeightedWindowEngine`, never part of the engine Protocol or of `DerivedDraft`. The engine has no reference to the Kafka producer, consumer, observer, or emitter.

See `doc/adr/0001-message-shaping-pipeline.md` for the design rationale and the future-state target.

---

## Enrichment Pipeline

**The message is shaped by an ordered chain of enrichers, not by the engine.**

After the engine returns a `DerivedDraft`, the worker runs it through an `EnrichmentPipeline` — an ordered list of `Enricher`s (`enrich(draft) -> draft`) configured per-worker in `main.py` next to `RULES`. Each enricher:

- owns exactly **one** capability and **self-decides applicability** — if its capability doesn't apply (e.g. the contributors aren't geolocated), it returns the draft unchanged;
- is **pure**: returns a new draft via `model_copy(update=...)`, never mutates the input;
- decides applicability from the **contributors** (a derived event gains a capability only if its contributors support it).

The pipeline is **best-effort**: by the time it runs, the engine has already decided to fire (possibly with irreversible side effects), so a raising enricher is logged and skipped — the event is still emitted, partially enriched. `finalize()` then merges the core + accreted capability fields into the transport dict; the Emitter still receives a `dict`, which Vector re-wraps into an `Envelope` for `high_level_events`.

**Contributor data:** because enrichers shape the derived event from its contributors, the engine must supply the contributing source events as full `Envelope`s in the draft (`DerivedDraft.contributors: tuple[Envelope, ...]`), not a flattened subset. *How* it retains them is engine-private (`WeightedWindowEngine` keeps the full envelopes in a Redis HASH pruned alongside its ZSET).

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
- The worker's identity must be derived from the directory name, never declared as a literal. The directory layout is the source of truth; this prevents copy-paste drift across workers. Two forms are exposed at the top of `main.py`:
  - `WORKER_NAME = Path(__file__).parent.name` — snake_case, used at the **data layer**: `RULES["name"]`, `APPLICATION`, Redis keys, emitted `inference_type`, Vector URL path, logger names.
  - `WORKER_SLUG = WORKER_NAME.replace("_", "-")` — kebab-case, used at the **infra layer**: Kafka consumer group ID, and any other K8s / Docker / external-naming boundary that rejects underscores. The image-publish workflow already slugifies the directory name the same way (`${WORKER//_/-}`).
  - The worker's Dockerfile must preserve the directory structure when copying `main.py` into the image (`workers/<name>/main.py`, not flattened to `main.py`). Otherwise `Path(__file__).parent.name` resolves to the WORKDIR (`app`), and every identity downstream — Redis keys, `inference_type`, Vector URL path, Kafka group — silently becomes `app`.
- Engine-internal infra (Redis connection, future Postgres connection, etc.) lives in the engine module — see **Engine-Owned Infrastructure**. It must not appear in `config.py` or `main.py`.

`main.py` is still only wiring — it does not contain logic. The per-worker constants at the top of the file are the worker's ConfigMap-equivalent.
