Turns raw phone and car sensor signals into human-legible life events ("you drove somewhere", "you
spent 40 minutes at the bakery"). An inference event is **data** — a YAML file in `events/` — and one
generic Quix Streams runtime loads every definition and runs them all in a single process, one
consumer group, one keyed pipeline. Signals arrive over Kafka; derived events go back to Kafka and on
to Postgres.

- [`doc/core.md`](doc/core.md) — how the runtime works: topology, engines, enrichment/capabilities,
  state, the full module reference. Start here.
- [`doc/invariants.md`](doc/invariants.md) — the design rules a change to `src/` must not break.
- [`doc/adr/`](doc/adr/) — why it is built this way.
- [`doc/vector-pipeline.md`](doc/vector-pipeline.md) — the ingest gateway and Neon persister.
- [`CLAUDE.md`](CLAUDE.md) — orientation, commands, deploy model.
