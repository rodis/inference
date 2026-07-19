"""Map BMW CarData descriptor updates → canonical raw signals (ADR 0006).

Asymmetric by design (ADR 0006):
  - isMoving false→true       → car_started_moving   (trip START anchor; self-sufficient in
    got_into_the_car; the runtime's cooldown swallows red-light restarts)
  - ignition (isActive) →off  → car_ignition_off     (STRONG trip END anchor: ignition off =
    actually parked; unlike raw motion-off it doesn't fire at red lights)
  - driver door OPEN edge      → car_driver_door_opened  (END disambiguator / corroborator)
  - isMoving true→false        → car_stopped_moving   (weak end corroborator — red-light unsafe alone)
  - ignition →on               → car_ignition_on      (start corroborator)
  - deep-sleep true edge       → car_deep_sleep        (slow, certain park backstop)

NOTE: the two engine descriptors have confusingly-swapped catalogue labels
(`isActive`="Vehicle ignition state", `isIgnitionOn`="Vehicle engine state"). We treat
`isActive` as the clean park signal (ignition/terminal — stable through auto start-stop)
and leave `isIgnitionOn` (the combustion engine — cycles at lights) out for now; confirm
from a live drive which one is red-light-stable before weighting it upstream.

Edge-triggered: we hold the last seen value per (descriptor) and emit only on the
transition, so a stream of unchanged readings doesn't spam the pipeline.

TWO THINGS TO FINALIZE FROM A REAL PAYLOAD (we've only seen the HA entity names, not the
raw CarData descriptor ids / message envelope):
  1. DESCRIPTOR_* ids below — the exact keys BMW streams (HA calls them
     vehicle_motion_state / door_state_front_driver / vehicle_deep_sleep_mode).
  2. `_iter_updates` — the MQTT message envelope shape (vin + data[...]).
Both are isolated here on purpose; log a raw message once connected and adjust.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

log = logging.getLogger(__name__)

# --- Descriptor ids (CONFIRMED 2026-07-20 against the live container J00I035N193CD +
# telematicData snapshot). Values arrive as string enums ("OPEN"/"CLOSED", "SECURED",
# "true"/"false") + ISO-8601 timestamps — handled by _as_bool / _epoch_seconds. ---
DESCRIPTOR_MOTION = "vehicle.isMoving"                              # "Vehicle Motion state"
DESCRIPTOR_IGNITION = "vehicle.drivetrain.engine.isActive"         # "Vehicle ignition state"
DESCRIPTOR_DRIVER_DOOR = "vehicle.cabin.door.row1.driver.isOpen"   # "Door state (front driver)"
DESCRIPTOR_DEEP_SLEEP = "vehicle.vehicle.deepSleepModeActive"      # "Vehicle deep sleep mode"

# Canonical signal names (must match the weights maps in events/got_into|got_out once fused)
SIG_STARTED = "car_started_moving"
SIG_STOPPED = "car_stopped_moving"
SIG_IGNITION_OFF = "car_ignition_off"    # STRONG end anchor: ignition off = actually parked
SIG_IGNITION_ON = "car_ignition_on"      # start corroborator
SIG_DOOR_OPEN = "car_driver_door_opened"
SIG_DEEP_SLEEP = "car_deep_sleep"


def _as_bool(value) -> bool | None:
    """Normalize BMW's various truthy encodings. None if unrecognized."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "on", "moving", "open", "opened", "active", "yes"):
            return True
        if v in ("false", "0", "off", "notmoving", "not_moving", "closed", "inactive", "no"):
            return False
    return None


def _epoch_seconds(ts) -> int:
    """CarData descriptor timestamp → int epoch seconds (what the engines read)."""
    if isinstance(ts, (int, float)):
        # Heuristic: ms vs s.
        return int(ts / 1000) if ts > 1e12 else int(ts)
    if isinstance(ts, str) and ts:
        try:
            return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return int(time.time())


class Mapper:
    def __init__(self) -> None:
        self._last: dict[str, bool] = {}

    def process(self, raw_msg: dict) -> list[tuple[str, int, dict]]:
        """Return a list of (event_name, timestamp_epoch, extra) to ingest."""
        out: list[tuple[str, int, dict]] = []
        for descriptor, value, ts in self._iter_updates(raw_msg):
            b = _as_bool(value)
            if b is None:
                continue
            prev = self._last.get(descriptor)
            self._last[descriptor] = b
            # Skip when there's no prior (first observation → establish baseline SILENTLY,
            # so a parked car's initial/retained motion=false doesn't mint a phantom
            # car_stopped_moving on every startup/reconnect) or when unchanged (no edge).
            if prev is None or prev == b:
                continue

            if descriptor == DESCRIPTOR_MOTION:
                out.append(((SIG_STARTED if b else SIG_STOPPED), ts, {"source_descriptor": descriptor}))
            elif descriptor == DESCRIPTOR_IGNITION:
                out.append(((SIG_IGNITION_ON if b else SIG_IGNITION_OFF), ts, {"source_descriptor": descriptor}))
            elif descriptor == DESCRIPTOR_DRIVER_DOOR and b:  # OPEN edge only
                out.append((SIG_DOOR_OPEN, ts, {"source_descriptor": descriptor}))
            elif descriptor == DESCRIPTOR_DEEP_SLEEP and b:
                out.append((SIG_DEEP_SLEEP, ts, {"source_descriptor": descriptor}))
        return out

    def _iter_updates(self, raw_msg: dict):
        """Yield (descriptor_id, value, epoch_seconds) from one MQTT message.

        FINALIZE against a real payload. Defensively handles the two likely shapes:
          A) {"vin": "...", "data": [{"name"/"descriptor": id, "value": v, "timestamp": t}, ...]}
          B) {"vin": "...", "data": {id: {"value": v, "timestamp": t}}}  or  {id: v}
        Logs the raw message the first time so we can lock the parser.
        """
        if not getattr(self, "_logged_shape", False):
            log.info("first CarData message (finalize _iter_updates against this): %s", raw_msg)
            self._logged_shape = True

        data = raw_msg.get("data", raw_msg)
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                did = item.get("name") or item.get("descriptor") or item.get("id")
                if did:
                    yield did, item.get("value"), _epoch_seconds(item.get("timestamp"))
        elif isinstance(data, dict):
            for did, v in data.items():
                if isinstance(v, dict):
                    yield did, v.get("value"), _epoch_seconds(v.get("timestamp"))
                else:
                    yield did, v, int(time.time())
