# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Required reading

The [`doc/`](doc/) folder is the source of truth for architecture and design rules. Read these before non-trivial changes:

- [`doc/core.md`](doc/core.md) — **the reference for everything in [`src/`](src/).** How the runtime works, with diagrams: the topology, the import rule that shapes the code, a hop-by-hop trace of one event, the detection/shaping split, in-process recursion, keying + state, the enrichment/capability seam, and a per-module + per-engine field reference (every `engine_config` key, every state key, every failure mode). Start here for a non-trivial `src/` change; the ADRs below are the *why* behind it.
- [`doc/invariants.md`](doc/invariants.md) — **the normative rule list** (22 rules, each with why + how to apply). The review checklist for a `src/` change. Rewritten 2026-07-27 for the Quix runtime.
- [`doc/adr/0004-scaling-model.md`](doc/adr/0004-scaling-model.md) — **the current architecture.** Why the runtime is a single Quix Streams `Application`, the state/partition co-location keystone, and the deploy findings.
- [`doc/adr/0002-recursive-derivation.md`](doc/adr/0002-recursive-derivation.md) — entailment vs correlation; recursive derivation (now resolved in-process by the Quix router).
- [`doc/adr/0005-session-gated-derivation.md`](doc/adr/0005-session-gated-derivation.md) — the `session_gated_window` engine: a start entails its end, so an open session relaxes the end-detector (the required `trigger` fires on the gate alone), with the trigger kept necessary as the guardrail.
- [`doc/adr/0006-car-native-trip-signals.md`](doc/adr/0006-car-native-trip-signals.md) — **proposed, not implemented.** Fuse car-native BMW CarData signals into the `got_into`/`got_out` weight maps via an HA-independent MQTT subscriber (a producer into `raw_sensors`, no new topic). Reliability comes from a *second independent source*, not more phone signals; start (`isMoving`→true) and end (a park-confirm, never raw `isMoving`-false) are asymmetric.
- [`doc/adr/0007-stays-not-fences.md`](doc/adr/0007-stays-not-fences.md) — **places are stays, not fences.** Why edge-triggered geofencing structurally cannot see a shop visit (a boundary needs a sample on each side; standing still produces none) and the `stay_window` clustering engine that replaces it for dwell. (The `geofence` engine it left in place for large declared regions was itself removed 2026-08-01 — it never fired in production.) Also: place *labelling* is deliberately not in the engine.
- [`doc/vector-pipeline.md`](doc/vector-pipeline.md) — **current truth for Vector.** The ingest + persist + metrics lanes with a graph, and the two-level ingest URL grammar (`/<domain>/<app>`): domain routes to a topic (first level), app routes to a body adapter within that domain (second level). Supersedes ADR 0001's Vector-transform description.
- [`doc/adr/0009-weights-are-at-their-ceiling.md`](doc/adr/0009-weights-are-at-their-ceiling.md) — **stop tuning weights; build vetoes.** Why a proposal to derive weights *dynamically* (base weight scaled by how the event behaved — later arrival, more flap) was rejected: the dominant failure mode is **ambiguity, not unreliability**. Both phantoms of 2026-07-28/29 fired with every contributor prompt, in-lag and flap-free, so a behavioural discount would have changed nothing — a lock means "locked *or* unlocked" regardless of how punctually it arrived. Also: lateness is a property of the transport, not the fact; flap rate isn't observable until after the decision fires (and makes scoring non-monotonic); six parameters per signal cannot be fitted on 22 trips; and `decaying_window` — a dynamic-weight engine — has been registered with **zero consumers** the whole time. Records the five weight candidates that all failed or were neutral, against the displacement guardrail that killed a 4h phantom with a 23m×20m bounding box because it is a *physical fact*, not weighted evidence. Read before proposing any new weight scheme.
- [`doc/connectors.md`](doc/connectors.md) + [`doc/adr/0008-connector-tier-via-n8n.md`](doc/adr/0008-connector-tier-via-n8n.md) — **how a third-party source gets added, and the rule that bounds it.** Sources like Gmail arrive as **n8n workflows** POSTing to the existing `/sensors/<app>` gateway — no worker, no Vector transform, no topic. The boundary: a connector may *authenticate, fetch and rename fields*; it may **not** threshold, correlate, window or decide that something happened. The reason it may not is that transformation splits at the **Kafka boundary** — a parser running *before* Kafka destroys whatever it misparses, while one running *after* leaves the raw body in Neon and is fixable by re-running `rederive.py` (invariant 19). So semantic extraction is a capability deriver, never a pre-Vector shaper. Also: n8n's Gmail trigger **polls** (1-minute floor) — it is not push, so it buys no latency for mail, only the absence of a poller/cursor/OAuth loop; its trigger advantage is real for webhook-native sources. Measured with [`scripts/connector_eval.py`](scripts/connector_eval.py).
- `doc/architecture.md`, `doc/classes.md` — **deleted 2026-07-27** (they described the removed pre-Quix threaded runtime and had carried STALE banners for a month). Replaced by `doc/core.md`; read them in git history for the archaeology.
- [`doc/adr/0001-message-shaping-pipeline.md`](doc/adr/0001-message-shaping-pipeline.md), [`0003-dynamic-event-runtime.md`](doc/adr/0003-dynamic-event-runtime.md) — **superseded by 0004.** Historical decision records (the typed-message/enricher pipeline and the threaded one-process-many-handlers runtime). The code they describe has been removed; the ideas live in git history.

When you modify behavior one of these documents describes, update the document in the same change.

## Big-picture model

An inference event is **data** — a YAML file in [`events/`](events/). A single generic **Quix Streams runtime** loads every definition and runs them all in one `Application`, one process, one consumer group:

- entrypoint [`workers/runtime/quix_main.py`](workers/runtime/quix_main.py) → [`inference.runtime.quix`](src/inference/runtime/quix.py) (`build_runtime()` / `run()`);
- definitions loaded by [`inference.runtime.definition.load_definitions`](src/inference/runtime/definition.py) (the `EventDefinition` schema).

The runtime is two small modules split on one rule — **the derivation core never imports `quixstreams`** (so it's portable across transports/state backends and drivable in-memory for tests; a second adapter reuses it unchanged):

- [`core.py`](src/inference/runtime/core.py) — the **transport-agnostic inference core**: entity keying, routing + in-process recursion, output shaping, and the pure definition→topology planning. Everything here is plain functions over dicts + a `get`/`set` state port (`StateStore`); no Kafka/Quix.
- [`quix.py`](src/inference/runtime/quix.py) — the **Quix/Kafka adapter** and composition root: builds the `Application`, wires the one keyed pipeline, injects the source events + per-entity `State` into the core, and runs it.

Read them as:

- **`core.Router.key_for(event)`** — the **entity key** a window aggregates over (`message.user_id`; sentinel `_no_user_id` + warning if missing). Partition + state-ownership unit; a static method on `Router` (the keying policy is part of the port), which the adapter feeds to `group_by(router.key_for)`.
- **Engines ([`inference.engines`](src/inference/engines/))** — the *strategy*, resolved from the definition's `engine` string via a registry. Six built-ins today. Two share a prune-window-then-score shape: `weighted_window` (weighted sum of distinct contributors vs a threshold, event-time cooldown) and `decaying_window` (same, but each contributor's weight fades with age — `half_life_seconds`). (A seventh, `naive_bayes_window`, was removed 2026-07-27: `car_door_closed` was its only consumer, gone since ADR 0005, and its one distinguishing feature — emitting a *calibrated posterior* rather than an arbitrary sum — lost its point when `confidence_score` left the data model, since a score is now detection-local and never emitted. It lives in git history.) `session_window` is a different strategy: it pairs a *start* + *end* event into one session (`device_connected_to_power` + `device_disconnected_from_power` → `phone_is_charging`), holding the open start in state until the end closes it. `validated_session_window` (issue #23 P1) subclasses it and adds a **displacement guardrail**, and is what `car_trip` uses: while the session is open it folds the raw `location_ping` stream into a running bounding box and *suppresses* a session the entity demonstrably didn't move over. It exists because both of `car_trip`'s detectors are direction-ambiguous and can fire on the wrong side of a boundary, handing the session engine a **time-inverted** trip — on 2026-07-27 that minted a 15-minute "drive" across a span the phone was parked at a vet. Displacement needs no tuning (it's a physical fact, not a threshold over noisy evidence), and abstain/reject are deliberately asymmetric: below `min_fixes`, or when the fixes cover less than `min_coverage_ratio` of the session, it emits anyway, because sparse GPS is absence of evidence and a silently-dropped real trip is worse than a visible phantom. It is the only engine consuming both derived events and a raw stream. `session_gated_window` (ADR 0005) is a weighted window plus a **latched gate**: `got_out_the_car` scores the raw exit signals (any 2-of-3 fires it), and while an open-trip gate (`got_into_the_car`, consumed on fire) is set it adds a bonus weight so a single *reliable* signal (a CarPlay-disconnect) closes a trip we already know is open — weights are tuned (ADR 0005) so the direction-ambiguous lock and the noisy charger-disconnect can't single-fire it (guarding against a lock at entry or a mid-drive unplug). `stay_window` (ADR 0007) detects **dwell** in the same `location_ping` stream, by clustering consecutive fixes within `radius_m` of a running centroid and emitting one `stay` (centroid + duration, `interval` capability) at *departure* when the cluster breaks and lasted `min_dwell_seconds`. It is the answer to `geofence`'s structural limit: an edge detector needs a sample on each side of a boundary, but standing inside a shop produces no fixes at all (iOS stops sampling, the producer's min-distance filter suppresses the rest), so a small region can receive zero points and never fire. Clustering degrades a stay's *precision* with sparse data instead of erasing it, needs no region declared in advance, and costs one definition instead of two per place — so places become a *labelling* problem over centroids rather than a routing problem. Both geometry engines share `inference.geo` (haversine + a plausibility guard that rejects fixes implying impossible travel — a real fix reported `acc: 5` while being 700m wrong, so the accuracy gate alone is not enough). Each exposes `input_event_names()` (drives routing) + `decide(event, state) -> Decision | None`. Per-entity state (Quix `State` = RocksDB + changelog in production, any `StateStore` otherwise) is scoped per definition via `ScopedState` (keys `<def>:window`/`<def>:last_fired`), so definitions share one store without colliding. **No Redis.**
- **`core.Router.route(event, state)` then `core.Shaper.shape(item)`** — two stages, deliberately split ([`core.md` §4](doc/core.md#4-detection-vs-shaping)). `route` is **detection**: it walks the consumers map, calls `engine.decide(...)`, resolves recursion in-process, and mints only the identity envelope (`id`, `name`, `inference_type`, `user_id`, `timestamp`), carrying the engine's full source bodies alongside as a `sources` sidecar. `shape` is **output shaping**: it projects `derived_from` from those sources, derives the definition's declared **capabilities** from them (`interval`, `place` — see `inference.capabilities`), and mints the `InferredEvent` + wrapper. The top-level wrapper is **identical to the one Vector mints for raw events** (`name`, `source_app`, `source_type`, `message`) so every Kafka topic carries the same shape; `source_type` records the entry mechanism (`"kafka"` for derived, `"http_server"` for raw) and is metadata only — the persister drops it, it never reaches Neon. **Time:** the only event-time is `message.timestamp`; "when the system handled it" is the DB-set `ingested_at` column — the old wrapper produce-time `timestamp` and `message.processed_at` (both ~= `ingested_at`) were dropped. The old `decide → finalize → Vector-re-wrap` hop is gone — we produce straight to Kafka — so engines only decide. **Gotcha:** the event re-enqueued for recursion is the *clean* envelope (no sidecar), so a downstream engine or deriver never sees an upstream derived event's lineage or capabilities.
- **`quix.build_runtime()`** — the composition root: builds a `core.RoutingPlan.from_definitions(...)` — one value holding the `name → engines` consumers index (from `input_event_names()`), the produced-name → sink map (`sink_for`, with `sink_topics` a derived view), the single external `source_topic`, and `capabilities_for` — then wires the topology: consume the external source → `group_by(router.key_for)` → stateful `Router.route` (`expand=True`) → `Shaper.shape` → `to_topic(sink)`. It also injects the Neon-loaded reference data (region definitions, the place book) so the core never reads a database itself.

Two things that are *not* obvious from any single file:

1. **One shared keyed router (`core.Router`), all definitions as data** (not one consumer/branch per event). `Router.route` loops the engines that consume each incoming event. This is forced by the Aiven free-tier **5-topic cap**: per-definition branches would mint N changelog + N repartition topics; the shared router costs **1 repartition + 1 changelog regardless of definition count**. See ADR 0004.

2. **Recursion is resolved in-process, not through Kafka.** The runtime consumes the **one external** source topic (the definitions' `source_topic` minus sinks — i.e. `raw_sensors`, *not* `high_level_events`; exactly one is required today, see ADR 0004). When the router fires a derived event, it feeds that event back through the consumers map within the same call (a queue), using the entity's persisted window — so e.g. `got_into_the_car` immediately opens `got_out_the_car`'s gate, and `got_out_the_car` immediately drives `car_trip`. Derived events are still produced to `high_level_events` (for persistence + external consumers); they are just not re-consumed. The `name` gatekeeper keeps the graph a DAG. (Caveat: assumes the runtime is the only producer of derived events — true today.)

**Identity.** The definition `name` (snake_case) is the emitted event `name` and the sink-routing key (and the key its per-entity state is scoped under). The emitted `inference_type` is the **engine type** that produced it (e.g. `weighted_window`), not the event name. The whole runtime shares **one** Kafka consumer group (`QUIX_CONSUMER_GROUP`, default `inference-quix-runtime-v2`) — *not* a group per event (that was the threaded model).

**Engine / strategy.** Each definition's `engine` string selects an `Engine` from the registry (`inference.engines`), constructed with its `engine_config` — which the **engine parses itself**; the runtime never knows the config schema. Six engines are registered today: `weighted_window` (`weights`, `threshold`, `window_seconds`, `cooldown_seconds`), `decaying_window` (adds `half_life_seconds`), `session_window` (`start_event`, `end_event`, `max_duration_seconds`), `validated_session_window` (adds `min_displacement_m`, `min_fixes`, `min_coverage_ratio`, `max_accuracy_m`, optional `max_speed_kmh`/`location_event`), `session_gated_window` (`gate_event`, `gate_weight`, `max_open_seconds`, `window_seconds`, `threshold`, `weights`, `cooldown_seconds`), `stay_window` (`radius_m`, `min_dwell_seconds`, `max_accuracy_m`, `max_gap_seconds`, optional `max_speed_kmh`; place-agnostic, so it needs no region rows). Lineage (`derived_from`) is produced by `core.Shaper` as a projection of the decision's full source bodies, and the declared capabilities are derived from those same bodies (the enricher seam — see below). Per-engine config keys, state keys, firing rules and defaults are tabulated in [`core.md` §11](doc/core.md#11-engine-reference) and [§13](doc/core.md#13-state-key-layout).

**Vector's role shrank.** Vector is the **ingest gateway** (producers POST → `raw_sensors`) and the **Neon persister** (`kafka` source over `raw_sensors` + `high_level_events` → Postgres). It is **no longer in the emit path** — the runtime produces the event record straight to Kafka via `to_topic()`. The full transform topology (and the two-level `/<domain>/<app>` ingest URL grammar) is documented in [`doc/vector-pipeline.md`](doc/vector-pipeline.md).

## Adding a new event

1. `events/<name>.yml` — copy an existing definition; set `name`, `engine: weighted_window`, `engine_config` (`weights`, `threshold`, `window_seconds`, `cooldown_seconds`), `source_topic`, `sink_topic`. (See [`runtime/definition.py`](src/inference/runtime/definition.py).)
2. That's it — the runtime loads it on next start. No new directory, consumer, image structure, or ArgoCD app.

A new **strategy** is a new `Engine` class in [`src/inference/engines/`](src/inference/engines/) + `@register_engine("<name>")` + `engine: <name>` in a definition — no runtime change. (This is ADR 0001's swappable-engine seam, re-established in the Quix runtime.)

## Local development

Env/secrets live in `workers/.env` (gitignored). The entrypoint loads it via `find_dotenv(usecwd=True)`, which walks upward from the CWD — **run from inside the `workers/` tree**. In K8s the same vars come from the `ConfigMap` (Kafka bootstrap) and `Secret` (Kafka mTLS files mounted at `/etc/kafka/ssl`); `find_dotenv` returns `""` and is skipped.

## Deploy-state branch

`deploy/` holds: [`deploy/inference/kustomize/`](deploy/inference/kustomize/) (the runtime), [`deploy/vector/kustomize/`](deploy/vector/kustomize/) (Vector — ingest gateway + Neon persister), [`deploy/dashboard/kustomize/`](deploy/dashboard/kustomize/) (the read-only Aware dashboard — Stakater chart, reads Neon, no ingress yet), and [`deploy/argocd/`](deploy/argocd/) (the three `Application` manifests). All deploy into the **`inference`** namespace. The `inference-runtime` and `inference-dashboard` apps track `deploy-state`; `inference-vector` tracks `main` directly.

`main` is the source branch. Two workflows keep `deploy-state` (which Argo CD watches) in sync — never commit to `deploy-state`, it is force-pushed:

- **Code changes** (`paths-ignore: deploy/**`) trigger [`publish-images.yml`](.github/workflows/publish-images.yml): build each **component** image (auto-discovered `workers/<name>/Dockerfile` → `inference-<slug>`, plus the explicitly-declared `dashboard/Dockerfile` → `inference-dashboard`), bump that component's `values.yml` (`deploy/inference/kustomize/base/<slug>/values.yml` for workers, `deploy/dashboard/kustomize/base/values.yml` for the dashboard) to `sha-<short>`, commit, force-push `deploy-state`.
- **Deploy-only changes** (`paths: deploy/**`) trigger [`mirror-deploy-state.yml`](.github/workflows/mirror-deploy-state.yml): mirror `main`→`deploy-state` **carrying the existing `deploy-state` image tag forward**.

Pushing **both** code and `deploy/**` in one commit races on the `deploy-state` force-push — split them into separate pushes (code first). This is **enforced**, not conventional: `.githooks/pre-push` refuses such a push. Run `scripts/install-hooks.sh` once per clone.

It also **serialises the deploy cycle**: the hook takes a lock (`scripts/deploy-lock.sh`) that the push-monitor releases on its verdict, so one push travels the whole chain — CI → deploy-state → Argo → pods — before the next starts. Otherwise Argo may only ever observe the later of two close bumps, and the earlier commit's image is built, tagged, and never runs anywhere. A 30-minute TTL breaks the lock if the monitor dies.

The same hook refuses a push from a **linked worktree**. With several agents on worktrees there is still exactly one deploy target, so concurrent pushes are a coin flip over whose change ships — merge into the primary worktree and push once from there. Both rules are bypassable with `git push --no-verify` when you mean it.

## Runtime state in K8s

Quix `State` is local RocksDB at `/tmp/quix-state` (set in the Dockerfile). The container root filesystem is read-only, so an **`emptyDir`** is mounted there (see `deploy/inference/kustomize/base/runtime/values.yml`). State is **ephemeral by design** — recovered from the Kafka changelog on restart/reschedule, consistent with the no-in-cluster-persistence rule.

## What is intentionally not here yet

- **No liveness/readiness probes.** (Tests + CI now exist — see below — but the runtime has no health probes yet.)
- **Enricher chain** — the capability seam (`src/inference/capabilities.py`) is the enricher chain re-established (ADR 0001). Two capabilities exist: `interval` (span, from the lineage's extent) and `place` (ADR 0007 — centroid + spread from the contributing fixes, plus the label of the known place containing it). Known places are **data**: `regions` rows with `kind='poi'`, loaded by `inference.runtime.places` and injected into the seam by `build_runtime`, so the core still never reads Neon. `kind='zone'` rows and the geofence expansion that consumed them were removed 2026-08-01, so the registry now has exactly one consumer. A label is frozen at derive time (re-derive to relabel history). A POI row also carries **`everyday`** (the place you *live* in): the deriver stamps it onto the event and the dashboard keeps those stays off the timeline, because home dwell has no natural boundaries — `max_gap_seconds` chops it wherever iOS stopped sampling, so the "visit" is a sampling artifact (ADR 0007). The stay is still derived and persisted; the flag says what *kind* of place it is, and whether to draw it stays a consumer decision.
- **Single source partition** (`raw_sensors` = 1 partition) — correct and keyed, but no horizontal parallelism until partitions are added (by design — see ADR 0004).

## Commands

```bash
# Local run (from inside workers/ tree so workers/.env is found).
# Loads every events/*.yml; override the dir with EVENTS_DIR.
cd workers/runtime && python quix_main.py

# Build the runtime image locally
docker build -f workers/runtime/Dockerfile -t inference-runtime .

# Lint + tests (both run in CI — .github/workflows/ci.yml)
uv run ruff check .
uv run pytest                # tests/ exercise the import-clean core in-memory (no Kafka/Quix)

# Before changing a weight map / threshold: replay real signal history from Neon through the
# actual core (no Kafka). backtest.py says WHAT changed; trip_eval.py says whether it got BETTER
# (junk_trips = sub-2-minute phantom trips, drives_missed = real drives lost).
NEON_DATABASE_URL=... uv run python scripts/backtest.py --days 25 --candidate <cand.yml> --focus car_trip
NEON_DATABASE_URL=... uv run python scripts/trip_eval.py --days 25 [-v] [<cand.yml> ...]

# Score a CONNECTOR (an n8n-fronted third-party source; ADR 0008). Judges INGESTION, not
# derivation, so it needs no replay — latency split into trigger lag (the connector's) vs
# pipeline lag (ours, ~3.3s baseline), plus duplicates, freshness and contract compliance.
# Non-zero exit = something needs attention. Completeness is the one thing it can't self-check.
NEON_DATABASE_URL=... uv run python scripts/connector_eval.py --days 7 [--source gmail] [--all]

# Rebuild derived events from retained raws after a definition change (a new engine can't
# see the past; a `place` label is frozen at mint time). Dry-run by default; --only is
# REQUIRED because every definition fires in a replay and most already exist in Neon.
# Run from inside workers/ so find_dotenv picks up the Kafka creds.
(cd workers && NEON_DATABASE_URL=... uv run python ../scripts/rederive.py \
    --since '2026-07-25 00:00' --only stay --events-dir $PWD/../events [--produce])

# Validate Vector config/VRL locally BEFORE deploying (vector 0.57.0 = the pinned chart version)
vector vrl -i sample.json -p program.vrl                 # run one transform's `source:` program
(cd deploy/vector/kustomize/base/configs && vector validate --no-environment --config-dir .)

# Render manifests locally BEFORE pushing deploy/** — this is the same build Argo runs, and an
# unrenderable kustomization leaves the app stuck in ComparisonError (revision empty, hard
# refresh won't clear it; only a new commit will). `--enable-helm` is required: every base
# inflates a Helm chart via HelmChartInflationGenerator.
kustomize build deploy/inference/kustomize/base --enable-helm            # runtime + bmw-cardata
kustomize build deploy/vector/kustomize/overlays/production --enable-helm
kustomize build deploy/dashboard/kustomize/base --enable-helm
# Inspect a patched field rather than trusting the patch — e.g. the Stakater chart renders
# `strategy: {type: RollingUpdate}` with NO `rollingUpdate` child, so a JSON6902 `add` into
# /spec/strategy/rollingUpdate/... fails the BUILD, and replacing the whole object is required:
kustomize build deploy/inference/kustomize/base --enable-helm | grep -A 4 'strategy:'

# Regenerate the shared contract after changing inference.event (CI checks it's current)
uv run python scripts/emit_event_schema.py            # -> contracts/inferred_event.schema.json
(cd dashboard/web && npm run gen:types)               # -> src/generated/events.ts

# Install into a venv for editing
uv sync --extra dev          # dev extras = pytest + ruff; or: pip install -e .
```
