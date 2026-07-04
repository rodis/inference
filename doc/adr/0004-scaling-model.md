# ADR 0004 — Scaling model: keyed partitions over event-type placement

Status: **Draft / exploration — design-only; nothing implemented. Names the target and the next decision.**
Date: 2026-06-22

> The bridge record between the current threaded runtime ([ADR 0003](0003-dynamic-event-runtime.md))
> and the chosen Quix-Streams data-plane direction. It explains *why* the runtime does not scale
> horizontally per event type, why static sharding is the wrong fix, and what the principled answer is.
> No code changes here — the actionable output is a single modeling decision (the entity key).

---

## Context

ADR 0003 collapsed one-pod-per-event into a single generic runtime: it loads every `events/*.yml` and
runs **one handler (one thread, one consumer group) per definition in one process**
([`supervisor.py`](../../src/inference/runtime/supervisor.py)). `replicas: N` stamps out N **identical**
pods — every pod runs **every** handler.

This bought real operational wins (one image, one deploy, add-an-event = a YAML change) at a cost that
only shows up under load:

- **No per-event-type horizontal scaling.** A replica duplicates all M handlers; it cannot give one
  hot event type more parallelism. With 100 event types, every pod runs 100 threads + 100 librdkafka
  consumers + 100 consumer-group memberships, regardless of which types are busy.
- **The first real ceiling is consumer-group count**, not threads. 100 blocking IO-bound threads are
  cheap (parked in `poll()`); 100 group memberships × N replicas against a **2-broker free-tier Aiven**
  cluster is the constraint that bites first.

### Why the obvious fixes are wrong

- **More replicas** — duplicates the bundle; idle threads for partitions a pod didn't get. Adds load,
  not scale (see [ADR 0003 scaling note] / discussion).
- **Static sharding** (split `events/` via `EVENTS_DIR`, run K deployments each loading a slice).
  Rejected as the *primary* model: it's offline bin-packing against a load distribution you can't know
  in advance, it goes stale as traffic shifts, and the assignment is manual. Useful as a coarse blast-
  radius/ops boundary later; **not** a scaling strategy.

### Root cause

All of the pain traces to one decision in [`invariants.md`](../invariants.md): **identity = event_type
= consumer group**. That makes the *event type* the unit of placement — and the event type has
unpredictable, unbounded load. You cannot pack an unknown distribution by hand.

---

## Keystone: why state/partition co-location is the load-bearing idea

Everything below rests on one property, and it is worth stating on its own because the rest is
downstream of it:

> **`key → partition → exactly one consumer owns that partition` ⇒ there is structurally one writer
> for any key's state. Not one writer enforced by a lock — one writer by construction.**

The hardest thing in distributed stateful processing is concurrent mutation of shared state; the usual
toolbox (locks, leases, CAS, Lua, transactions) only *manages* that concurrency. Co-location takes a
different move: it arranges things so **the concurrency never exists**. You are not coordinating access
to shared state — there is no sharing.

Consequences that all fall out of this one property:

- **No locks / no Lua.** The partition assignment *is* the lock, held by the group coordinator. The
  **Single-Writer invariant** ([`invariants.md`](../invariants.md)) is satisfied for free.
- **State goes local.** Because there's one writer, window state can live local to the process
  (RocksDB on the pod) instead of in a shared store reached over the network — no per-event round-trip,
  no shared bottleneck. Redis drops out of the engine; the weighted-window *algorithm* survives, only
  its backend changes.
- **Scaling stays correct automatically.** On rebalance the partition *and ownership of its state* move
  together; the new owner rebuilds from the changelog. No split-brain window where two instances both
  think they own a key.
- **Three problems collapse into one fix.** The single-writer problem, the un-shardable-global-window
  problem, and horizontal scaling all dissolve from this single decision — they share one root (no
  partition key).

This is the foundation of Kafka Streams, Flink keyed state, Samza, and (in different dress) the actor
model — an actor *is* co-located state + a single-threaded mailbox keyed by identity.

**The catch (so we don't over-rotate):** co-location is only as strong as the key, and it cuts both
ways. Same-key aggregation becomes trivial; **cross-key joins become the new hard edge** — events with
*different* natural keys land on different owners and must be **repartitioned** (shuffled) to meet.
Co-location doesn't eliminate distributed coordination; it *relocates* it to the join boundary, where
it's explicit and contained (this is exactly ADR 0002's cross-key recursive-derivation question). And
local state needs a **changelog** for recovery — durability and rebalance-time replay are the price for
dropping the shared store. Design keys so the common path is same-key; the difficulty moves to the rare
cross-key path.

---

## Decision (direction, not yet built)

**Stop placing work by event type. Place it by keyed partition.**

Key every event by its **entity** (the unit a window aggregates over — e.g. which car / user / device).
Then:

- the **partition** is the unit of distribution; the **key** chooses the partition;
- a hot entity/type spreads across more partitions → more parallelism, **automatically**;
- scaling = add **identical, generic** instances; the framework's **rebalance** assigns partitions to
  them live — no manual assignment, ever;
- per-key window **state is co-located** with its partition: the instance holding the partition owns
  the state, so there is no cross-instance contention or shared-lock dance.

This is the natural completion of ADR 0003's "work definition is data, runtime is generic" idea: the
generic runtime + event-as-data is the right foundation; the missing pieces are **(a) the key** and
**(b) a real stream framework as the placement/state layer** — not a new bespoke abstraction.

### The two established paradigms (both replace static sharding with dynamic, key-driven placement)

1. **Stream processing — Kafka Streams / Flink / Quix (chosen).** Generic worker = a runtime instance;
   "task definition" = the event YAML (already data); the "middle layer that assigns work" = the
   consumer-group rebalance protocol + partitioned, changelog-backed state. Quix gives Kafka-Streams
   semantics in Python: keyed state in RocksDB co-located per partition, recovered from a changelog
   topic. **This is the direction already chosen** (Flink rejected as too heavy; CRD+operator deferred).

2. **Virtual-actor model — Orleans grains / Akka cluster sharding / Dapr actors.** One actor per entity,
   identified by key, **activated on demand and placed dynamically** by a directory/coordinator,
   single-threaded per entity, owning its own state. A literal embodiment of "a little worker per car
   that wakes, accumulates evidence, and fires." Recorded as the road **not** taken (no actor runtime in
   the stack), but it's the cleanest conceptual match to per-entity windowed inference and worth knowing.

### Explicitly rejected: a generic task queue / dispatcher

A "middle layer that builds task definitions and hands them to a stateless worker pool"
(Celery/SQS-style) is the **wrong tool** here. Task queues fit **independent, stateless** work (scrape a
URL, resize an image). This workload is **stateful, ordered, keyed aggregation** — multiple events must
converge on one window, in order, with consistent state. A task queue discards ordering and
state-locality, forcing you to rebuild locking, dedup, and windowing on top of it — i.e. reimplement a
stream processor, badly. The reason streaming frameworks exist is precisely that this is hard and not
worth hand-rolling.

---

## The lynchpin: do events carry an entity key?

This is the existing Quix spike's open question and **the gate for everything above**. Without a key,
every event hashes to one partition and the system is pinned at today's **1-partition ceiling**
regardless of framework. With a key, the manual-sharding problem evaporates and horizontal scale comes
for free from rebalance.

### Next steps (in order)

1. **Decide the entity key** — what unit does a window aggregate over? (per car? per user?
   per car-per-trip?) A *modeling* decision, not a code one. Blocks everything else.
2. **Re-key ingest** so events carry it (Vector can stamp/route on the key at ingest).
3. **Port one event to Quix** keyed by it; prove `replicas=2` splits partitions correctly and per-key
   state stays consistent — the existing spike, now with the key decided.

---

## Finding from the spike (2026-06-27): the topic budget is a real ceiling

The step-3 spike (two instances over a 2-partition keyed topic) **proved the model**
— disjoint key ownership across instances, single-writer-per-key, zero double-fires.
But it surfaced a constraint the theory glosses over: **a stateful stream framework
mints internal topics per handler.** Each Quix stateful operator creates a
**changelog** topic; each `group_by` adds a **repartition** topic. On the Aiven
**free-0 plan (5 user topics max)** that ceiling is hit almost immediately
(`raw_sensors` + `high_level_events` + one spike's changelog already crowds it).

Consequence: with this data plane, **the number of stateful event types is bounded
by the topic budget, not just compute** — a second axis of the same "how many events
can we run" question this ADR opened. Mitigations: a larger Kafka plan; **key at
ingest (Vector) to avoid `group_by`** and its repartition topic; or share changelog
topics across handlers. This makes the "key at ingest vs `group_by` in-app" choice
(below) not just a performance question but a topic-count one.

## Spike outcome (steps 2–5, all verified live against Aiven 2026-06-27)

`workers/quix_spike/` (code since removed — see git history; the productionised form is
[`inference.runtime.quix`](../../src/inference/runtime/quix.py)) carried the direction from
theory to running code on the real cluster:

- **Step 2** — `car_door_opened` on one Quix `Application` + per-key `State` (no Redis,
  no thread-per-event); fired end to end.
- **Step 3** — two instances, one group, 2-partition keyed topic: disjoint key
  ownership, **zero double-fires** (single-writer-per-key, proven).
- **Step 4** — worker mints the full `Envelope` and `to_topic()`s it directly;
  parses through the production `Envelope` model. **Vector leaves the emit path.**
- **Step 5** — `runtime.py` loads every `events/*.yml` on **one shared `Application`**
  and fires both `car_door_opened` and (recursively) `got_into_the_car` in one process.
  **The 1:1 event↔thread binding is gone.**

The step-5 shape was **dictated by the topic budget above**: one shared keyed
stateful **router** (loads all definitions as data, per-`(definition, entity)` window
in namespaced state) costs 1 repartition + 1 changelog *regardless of definition
count*, whereas one-branch-per-definition costs N of each and overruns the 5-topic
cap. So "how many stateful events can run" is answered on free tier by collapsing to
a shared router — a concrete instance of this ADR's whole thesis (place/execute by
something other than per-event-type identity).

## Deployed to the cluster (2026-06-27) — and two findings it forced

The Quix runtime now **runs in-cluster** as the `inference-runtime` image CMD
(`workers/runtime/quix_main.py` → `inference.runtime.quix`), replacing the threaded
`RuntimeSupervisor`. State is an ephemeral emptyDir (`/tmp/quix-state`), recovered
from the changelog. Verified live: injecting raw events makes the deployed pod fire
`car_door_opened` **and** `got_into_the_car`, both landing on `high_level_events`.
Getting there forced two design changes worth recording:

1. **Read-only root filesystem.** RocksDB needs a writable dir; the hardened
   container root is read-only. Fix: an `emptyDir` mounted at `/tmp/quix-state`
   (ephemeral by design — state recovers from the Kafka changelog).

2. **Quix `concat()` + `auto_offset_reset=latest` does not consume new messages.**
   The runtime originally consumed `raw_sensors` + `high_level_events` (for recursive
   derivation) by `concat`-ing the two source dataframes. In-cluster it stalled —
   assigned partitions, consumed nothing. Bisected in-pod: raw consume/produce,
   `latest`, `group_by`, `stateful` all work *individually*; `concat`+`earliest` reads
   the backlog; but `concat`+`latest` consumes **zero** live messages. **Fix, which is
   also a better design: don't consume `high_level_events` at all.** Consume only
   *external* source topics (`union(source_topics) − sink_topics` = just `raw_sensors`
   → a single topic, no `concat`), and resolve recursion **in-process** — a fired event
   is fed back through the router's consumers map within the same call, using the
   entity's persisted window. Lower latency (no Kafka round-trip), fewer topics, and it
   sidesteps the `concat` bug. Caveat: this assumes the runtime is the *only* producer
   of derived events (true here — the old per-event workers are decommissioned); an
   external producer of a derived event would not be seen.

## Core / adapter split (2026-07-04) — the derivation core is transport-agnostic

The shared router grew organically inside one `quix.py` file, which ended up doing three
unrelated jobs — resolve definitions, decide/route/shape events, and speak Quix/Kafka. Two
of those are the *value* of the system (the derivation DAG + output shape) and one is
*plumbing* (the broker). They were split along a single enforceable rule:

- [`inference.runtime.core`](../../src/inference/runtime/core.py) — the **transport-agnostic
  inference core**: entity keying (`Router.key_for`), routing + in-process recursion (the `Router`),
  output shaping (`to_event`), and the pure definition→topology planning (`RoutingPlan`, one
  value object built from the definitions). **INVARIANT: this module must not import `quixstreams`** (or any
  transport/state backend).
- [`inference.runtime.quix`](../../src/inference/runtime/quix.py) — the **Quix/Kafka adapter**
  and composition root: builds the `Application`, wires the one keyed pipeline, and injects the
  source events + per-entity `State` into the core.

**Why this is a scaling-model concern, not just tidiness.** The engines were already a clean,
swappable layer (protocol + registry + implementations) because they are the axis of *variation*.
The substrate they run on — keying, routing, state — is the axis of *invariance*, so it never got
a boundary. But invariant ≠ structureless: making the core Quix-free names the **ports** this ADR
implicitly relies on. Engines depend only on a plain event dict + a `get`/`set` state port
(`StateStore`) + returning a `Decision`; the core depends only on that same state port. So the
Kafka/Quix choice is confined to one adapter file, and the whole derivation graph is portable —
a different transport or state backend is a new adapter, engines and core unchanged. This is the
hexagonal (ports & adapters) reading of the same "place/execute by something other than the
transport" thesis the rest of this ADR argues at the partition level.

Deliberately **not** done: no `RuntimeProtocol` with a single implementation (there is one
transport and one intended state backend — a protocol would be speculative indirection). The rule
is *cohesion + a Quix-free import boundary*, not polymorphism. The one second implementation that
already earns the seam is an **in-memory adapter for tests** — drive `core.Router(plan).route` from a list
with a dict-backed `StateStore`, no broker (demonstrated at the split; the repo still has no test suite,
but this is what unblocks one). Formalize additional ports only when a second real adapter arrives.

### Shape (as implemented)

```
                    DRIVING ADAPTERS   (own the transport; drive the core)

    +------------------------------+          +------------------------------+
    |  Quix / Kafka adapter        |          |  in-memory test harness      |
    |  quix.py                     |          |  no broker                   |
    |  raw_sensors -> ... -> sinks |          |  feeds events as a list      |
    +------------------------------+          +------------------------------+
                   |                                         |
                   '--------------------+--------------------'
                                        |  inbound port
                                        |  Router.route(event, state) -> list[dict]
                                        v
    +------------------------------------------------------------------------------+
    |  CORE - inference.runtime.core                    (never imports quixstreams)|
    |                                                                              |
    |    Router.key_for         entity key (partition / state unit)                |
    |    Router.route           in-process derivation DAG (resolves recursion)     |
    |    to_event               shapes the high_level_events record                |
    |    RoutingPlan            consumers index + sink_for + source_topic          |
    |      .from_definitions    built once from the YAML definitions               |
    +---------------+------------------------------------------+-------------------+
                    |                                          |
        state port                                strategy port
        StateStore: .get()/.set()                 Engine protocol
        (wrapped per-def in                       decide(event, state)
         ScopedState)                             input_event_names()
                    |                                          |
                    v                                          v
    +------------------------------+          +----------------------------------------+
    |  Quix State          [prod]  |          |  inference.engines  (registry)         |
    |    RocksDB + changelog       |          |    weighted_window                     |
    |  dict{}              [test]  |          |    decaying_window                     |
    |    in-memory StateStore      |          |    naive_bayes_window                  |
    +------------------------------+          |    session_window                      |
      DRIVEN ADAPTERS (state)                 +----------------------------------------+
                                                STRATEGY ADAPTERS (engines)
```

Three ports, each with swappable adapters; the core (hexagon interior) depends only on the ports:

- **Inbound port** — `Router.route(event, state) -> list[dict]`. Made concrete as the `Router`
  object (built from a `RoutingPlan`): its signature matches the stream framework's stateful callback,
  so the Quix/Kafka adapter (`quix.py` `_wire_topology`) mounts `router.route` directly — no lambda —
  turning the `raw_sensors` stream into calls and sending the returned derived events to
  `high_level_events`; the test adapter feeds a list and collects the return.
- **State port** — `StateStore` (`get`/`set`). Adapters: Quix `State` (RocksDB + changelog) in
  production, a `dict` in tests. `Router.route` wraps whatever it's handed in a `ScopedState`
  (per-definition key prefix), so the raw store only ever sees `get`/`set`.
- **Strategy port** — the `Engine` protocol (`decide`, `input_event_names`); its adapters are the four
  registered engines. This is ADR 0001's swappable-engine seam, orthogonal to the two above.

## Open questions

- **Key granularity** — per entity, or per entity-per-session/trip? Finer keys = more parallelism but
  more state churn and shorter-lived windows.
- **Cross-key derivations** — recursive derivation (ADR 0002) may correlate events with *different*
  natural keys (a car event + a phone event). What is the join key then, and does one side need
  re-keying? This is where keyed streams get genuinely hard.
- **Does the threaded runtime stay** — *resolved (2026-06-27, commit c0c6e95):* fully replaced by Quix
  and **removed** from the repo (no rollback shim; git history only).
- **Static sharding as an ops boundary** — even with keyed scaling, is `EVENTS_DIR` slicing still worth
  keeping for blast-radius isolation (a poison event can't stall unrelated handlers)?
- **State migration on rebalance** — changelog replay latency / standby replicas; acceptable for this
  project's "no load, prove-it-structurally" bar, but the real-world cost to understand.

### Future work: the two next goals (in order)

1. **Goal 1 — `user_id` entity key (multi-user, correctness).** The single change that makes the design
   scale to many users (throughput aside): every producer stamps a consistent `user_id` into the payload,
   and `Router.key_for` reads it (the fallback chain already drafts `vehicle_id or user_id or source_app`). It's
   primarily a **producer/ingest contract**, not runtime code. Everything else falls out: cooldowns/windows
   become per-user automatically, topic footprint unchanged, no cross-key issue (a user's events all share
   that user's key). Does **not** require goal 2.

2. **Goal 2 — multi-feed ingest (flexibility).** Let distinct external feeds (different schema/retention/
   producer/ACLs) coexist. Two routes, only one of which touches the `concat`+`latest` stall:
   - **Option A — fan-in at the edge.** Producers write to their own topics; **Vector merges them into the
     single `raw_sensors`** the runtime already consumes. Keeps the runtime single-source (the proven path);
     mostly Vector config. *Preferred default* — delivers decoupled feeds without the bug.
   - **Option B — runtime consumes multiple source topics.** Only if a feed truly can't be normalized at the
     edge. Requires solving the `concat`+`latest` stall. **Diagnose first:** minimal repro + check whether
     it's a known Quix bug / a newer Quix version offers native multi-topic sources (`app.dataframe(topics=…)`,
     making `concat` unnecessary). If real and unfixed, the workaround is `earliest` + **tail-seeded committed
     offsets** on first deploy (we proved `concat`+`earliest` consumes; seeding offsets at the tail avoids the
     one-time history replay, then it resumes from committed offsets like `latest`).
   Until pursued, the runtime enforces a **single external source topic** (the only tested path).

---

## Consequences

- The "generic runtime + event-as-data" foundation (ADR 0003) is **kept** — this builds on it, doesn't
  revert it.
- Per-event-type horizontal scaling stops being a packing problem and becomes a partition-count + key
  decision the framework executes automatically.
- Commits the project to the entity-key modeling decision as the **immediate** next step — it gates the
  Quix port, recursive-derivation join semantics (ADR 0002), and any horizontal-scale claim.
- This is a **learning-project** record: the goal is a *structurally* scalable design (provable without
  load), consistent with "scale by design, not under pressure." No load is expected; the value is the
  skill and the clean target.

---

When this moves past draft, update [`architecture.md`](../architecture.md) and
[`invariants.md`](../invariants.md) (especially the identity rule, which this directly reframes) per the
"update docs alongside behavior" rule in [`CLAUDE.md`](../../CLAUDE.md).
