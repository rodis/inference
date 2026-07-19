"""Post canonical events into the pipeline via Vector's HTTP ingest (ADR 0006).

We reuse the EXISTING `standard` sensors lane — no new Vector transform. The subscriber
is a producer like any other: it POSTs `{"payload": {...}}` to `/sensors/bmw`, where the
2nd path segment (`bmw`) routes to Vector's `standard` body adapter (shape_sensor), which
renames `event_name`→`name`, requires `user_id`, and hands off to enrich_sensor (id
minting) → raw_sensors. (OwnTracks needed a bespoke adapter only because its body is out
of our control; ours isn't.)

Contract shape_sensor requires inside `payload`:
  - event_name : the canonical signal name (→ message.name → the engine's input)
  - user_id    : entity key for per-user state (ADR 0004)
  - timestamp  : int epoch SECONDS (the event-time the engines read directly)
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class Ingest:
    def __init__(self, base_url: str, ingest_path: str, user_id: str):
        self._url = base_url.rstrip("/") + ingest_path
        self._user_id = user_id

    def post(self, event_name: str, timestamp: int, extra: dict | None = None) -> None:
        payload = {"event_name": event_name, "user_id": self._user_id, "timestamp": timestamp}
        if extra:
            # Never let extra fields clobber the required contract keys.
            for k, v in extra.items():
                payload.setdefault(k, v)
        try:
            resp = requests.post(self._url, json={"payload": payload}, timeout=15)
            # The http_server source returns 200 on receipt; downstream shaping rejects
            # (if any) show up as Vector error logs, not a 4xx here.
            if resp.status_code >= 300:
                log.error("ingest POST %s -> %s: %s", event_name, resp.status_code, resp.text[:200])
            else:
                log.info("ingested %s @ %s (user=%s)", event_name, timestamp, self._user_id)
        except requests.RequestException as exc:
            # Best-effort, at-most-once: a dropped POST is a lost signal. Fine for now
            # (WIP); if at-least-once matters later, add a retry/queue here.
            log.error("ingest POST failed for %s: %s", event_name, exc)
