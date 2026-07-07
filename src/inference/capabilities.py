"""Capability derivers — the enricher seam (ADR 0001, re-established).

A capability is a structured fact an event carries (see `inference.event.Capability`).
Each is derived from the event's **full source events** by a small registered function,
so capabilities scale by *addition*: write a deriver, register it, list the capability in
a definition's `capabilities:` — no change to the shaper or the router. This mirrors the
engine registry (detection) on the shaping side.

A deriver takes the source event records and returns a **fragment** of `InferredEvent`
fields to merge onto the emitted event (e.g. `{"interval": Interval(...)}`). Deriving over
full source bodies (not the trimmed `derived_from` lineage) is deliberate: a future `geo`
or `amount` capability needs message fields that the lineage projection doesn't carry.

Import-clean (pure Python + the domain model); importing this module registers the
built-ins, the same side-effect pattern as `inference.engines`.
"""

from collections.abc import Callable

from inference.event import Capability, Interval

# capability → deriver(sources) -> fragment of InferredEvent fields
_DERIVERS: dict[Capability, Callable[[list[dict]], dict]] = {}


def register_capability(capability: Capability):
    """Decorator registering a deriver for `capability`."""

    def _wrap(fn: Callable[[list[dict]], dict]) -> Callable[[list[dict]], dict]:
        _DERIVERS[capability] = fn
        return fn

    return _wrap


def derive_capability(capability: Capability, sources: list[dict]) -> dict:
    """Run the registered deriver, returning the InferredEvent-field fragment it produces."""
    try:
        deriver = _DERIVERS[capability]
    except KeyError:
        raise RuntimeError(
            f"No deriver registered for capability '{capability}'. "
            f"Registered: {sorted(c.value for c in _DERIVERS)}"
        ) from None
    return deriver(sources)


def _source_timestamps(sources: list[dict]) -> list[int]:
    return [(s.get("message") or {})["timestamp"] for s in sources]


@register_capability(Capability.INTERVAL)
def _interval(sources: list[dict]) -> dict:
    """The interval spans the lineage's extent — earliest source to latest. Pure function
    of the evidence; no engine-specific knowledge, so any event declaring INTERVAL gets it
    the same way. Callers guarantee non-empty sources (a declared capability with none is a
    misconfiguration)."""
    timestamps = _source_timestamps(sources)
    return {"interval": Interval(started_at=min(timestamps), ended_at=max(timestamps))}
