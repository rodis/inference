"""Resolving `await` signals against recorded events (ADR 0012).

An `await` asks "has the fact this stage waits for appeared yet?". For a human decision the
answer is deterministic and needs no interpretation: **applying a Gmail label IS the decision**,
so the connector reports it (ADR 0008's Stage 1 shape) and this matches it. No LLM is involved,
and none should be — classification is for genuine semantic work like telling a *submitted*
payment from a merely *processed* one, not for a fact a human has already stated.

Pure except for the `signals(name, since)` port, so every correlation rule below is testable
without a database.
"""

import logging
from typing import Protocol

from reconciler.core import Cycle, Milestone

logger = logging.getLogger("reconciler.finder")

DEFAULT_SLACK_SECONDS = 900


class SignalSource(Protocol):
    def signals(self, name: str, since: int) -> list[tuple[int, dict]]: ...


class EventFinder:
    """Satisfies a stage when a matching raw event exists.

    Signal grammar (`source: event`):

        event:            the raw event name to look for (required)
        correlate_on:     a field that must match the SAME field on a predecessor milestone,
                          which is what ties a labelled mail to *its* cycle rather than to
                          whichever cycle happens to be open
        where:            plain field equality filters, e.g. {from_domain: dreamhost.com}
        slack_seconds:    how far before `since` to look

    **Slack is not sloppiness.** For a mail we sent ourselves, the connector's event time is
    the message's `Date` header — i.e. roughly when the approval request went out, not when
    the label was applied. So the evidence legitimately carries a timestamp at or just before
    the milestone that triggered it, and a strict `>= since` would never match. `correlate_on`
    is what keeps the widened window safe.
    """

    def __init__(self, source: SignalSource):
        self._source = source

    def find(self, signal: dict, cycle: Cycle, since: int,
             milestones: dict[str, Milestone] | None = None) -> dict | None:
        name = signal["event"]
        slack = int(signal.get("slack_seconds", DEFAULT_SLACK_SECONDS))
        where = signal.get("where", {})
        correlate_on = signal.get("correlate_on")

        expected = None
        if correlate_on:
            expected = _expected_value(correlate_on, milestones or {})
            if expected is None:
                logger.warning(
                    "cycle %s: nothing to correlate %r against yet; not matching",
                    cycle.key, correlate_on)
                return None

        for ts, body in self._source.signals(name, max(0, since - slack)):
            if any(body.get(field) != value for field, value in where.items()):
                continue
            if expected is not None and body.get(correlate_on) != expected:
                continue
            logger.info("cycle %s: matched %s at %s", cycle.key, name, ts)
            return {"matched": name, "matched_at": ts, "evidence": body}
        return None


def _expected_value(field: str, milestones: dict[str, Milestone]):
    """The value a signal must carry, taken from the most recent milestone that has it.

    Most recent, not first: a process that asks twice (a re-opened cycle, a resent mail)
    must correlate against the *latest* thing it said, or it would match its own stale
    request forever.
    """
    candidates = [m for m in milestones.values() if field in m.payload]
    if not candidates:
        return None
    return max(candidates, key=lambda m: m.timestamp).payload[field]
