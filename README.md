Kafka-driven inference worker framework for Kubernetes. Each worker consumes raw sensor events from a Kafka topic, evaluates them against a pluggable inference engine, and forwards results to Vector via HTTP when a threshold is met.

See `doc/architecture.md` for the full pipeline, `doc/classes.md` for the class reference, and `doc/invariants.md` for design rules.
