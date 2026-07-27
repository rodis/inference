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
  - lock SECURED/UNLOCKED edge → car_locked / car_unlocked  (car-native and DIRECTIONAL —
    observation only for now, in no weight map; see below)

**Nothing is discarded any more (2026-07-27).** Previously only the five edge descriptors
above became events, so everything else the car streams — odometer, GPS, fuel, windows,
alarm — existed solely in a *rotating* pod log and was unrecoverable within days. Three
additions close that:

  - odometer   → car_odometer     {odometer_km}
  - GPS lat+lon→ car_location     {lat, lon, altitude?}   ← FUSED, see below
  - fuel level → car_fuel_level   {fuel_level}
  - anything else that CHANGES → car_state_change {descriptor, value}

The catch-all is deliberate: it captures the whole stream for one event name instead of
fifteen, so the name space stays small while the history becomes queryable
(`select message->>'descriptor', message->>'value' … where name = 'car_state_change'`).
All of these are observation-only — no engine consumes these names, and the runtime ignores
names no engine declares.

**Two kinds of descriptor, two rules.** An *edge* (door, lock, motion) is baseline-silent:
the first observation only establishes state, because a parked car's retained "door closed"
must not mint a phantom event on every reconnect. A *reading* (odometer, GPS, fuel) is the
opposite — the value IS the fact, there is no phantom to guard against, so the first
observation is emitted and repeats are de-duplicated instead. Getting this backwards would
either lose the first reading or spam an event per reconnect.

**GPS is fused, not three events.** Latitude, longitude and altitude arrive as separate
descriptors in separate messages ~250ms apart, and a latitude without a longitude is
useless. They are paired into one `car_location`: a point is emitted only when lat and lon
carry timestamps within `_GPS_PAIR_WINDOW_S` of each other, which also means exactly one
event per batch rather than one per component (when latitude arrives, the retained
longitude is from the *previous* park and falls outside the window, so only the second
component of a pair completes it).

**Unmapped descriptors are logged, not silently dropped** (2026-07-26). The container
subscribes to more than this maps (lock status, odometer, GPS lat/lon), and because the
loop simply skipped unknown ids we could not say whether they ever arrive — the one code
path that would have shown them (`BMW_DEBUG_LOG_ALL`) has never been on in production.
`_note_unmapped` now logs each unknown descriptor once per process with its value, so a
single drive inventories the whole stream. Cheap and permanent (once per id, not per
message), unlike the whole-envelope debug flag.

It paid for itself on the first drive (2026-07-27): **24** unmapped descriptors — the
container is far larger than the 8 ADR 0006 documented — including odometer, GPS, fuel
level, trunk, all four doors, windows and alarm. It also caught two **wrong descriptor
ids** in the ADR (lock and GPS), which is why the lock mapping shipped 07-26 never fired.
Note what this log can and cannot tell you: once-per-process gives you the stream's
*vocabulary*, not which descriptors change *per trip* — the first message after connect is
a full state dump, so most ids are logged from that, not from driving.

`car_locked`/`car_unlocked` are emitted but deliberately in **no** weight map yet: they
land in `raw_sensors` → Neon for analysis, and the runtime ignores names no engine
consumes. The reason they matter is direction. Every car-native signal we have is
non-directional (the driver door fires at entry *and* exit, which is why it sits at
weight 4 in `got_into_the_car`), and so is the phone's `car_lock_state_change` — a
car-native lock *state* is the first signal that is both phone-independent and knows which
way it went. Weight it only after a replay (`scripts/trip_eval.py`), as ADR 0005/0006 did.

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

# "no prior observation" — distinct from a descriptor whose value legitimately IS None,
# which `dict.get(...)` alone cannot tell apart.
_UNSET = object()

# --- Descriptor ids (CONFIRMED 2026-07-20 against the live container J00I035N193CD +
# telematicData snapshot). Values arrive as string enums ("OPEN"/"CLOSED", "SECURED",
# "true"/"false") + ISO-8601 timestamps — handled by _as_bool / _epoch_seconds. ---
DESCRIPTOR_MOTION = "vehicle.isMoving"                              # "Vehicle Motion state"
DESCRIPTOR_IGNITION = "vehicle.drivetrain.engine.isActive"         # "Vehicle ignition state"
DESCRIPTOR_DRIVER_DOOR = "vehicle.cabin.door.row1.driver.isOpen"   # "Door state (front driver)"
DESCRIPTOR_DEEP_SLEEP = "vehicle.vehicle.deepSleepModeActive"      # "Vehicle deep sleep mode"
# The central lock. NOTE the id: `vehicle.cabin.door.lock.status` (transcribed into ADR 0006 from
# the kvanbiesen source) does NOT exist on this car — the live stream carries
# `vehicle.cabin.door.status = "SECURED"`, confirmed from the 2026-07-27 inventory. Read as a LOCK
# state, not a door-open aggregate: the four `…door.rowN.*.isOpen` descriptors already cover
# open/closed, and this one's vocabulary is SECURED-style. Unrecognized values emit nothing and are
# logged (`_lock_is_secured`), so if that reading is wrong we find out instead of minting nonsense.
DESCRIPTOR_LOCK = "vehicle.cabin.door.status"

# --- Readings (numeric): the value is the payload, not just the transition. Ids confirmed
# from the live stream inventory 2026-07-27 (travelledDistance = 24819, level = 18). ---
DESCRIPTOR_ODOMETER = "vehicle.vehicle.travelledDistance"
DESCRIPTOR_FUEL = "vehicle.drivetrain.fuelSystem.level"
_NAV = "vehicle.cabin.infotainment.navigation.currentLocation"
DESCRIPTOR_GPS_LAT = f"{_NAV}.latitude"
DESCRIPTOR_GPS_LON = f"{_NAV}.longitude"
DESCRIPTOR_GPS_ALT = f"{_NAV}.altitude"

# Boolean/enum descriptors, edge-triggered and baseline-silent.
_EDGE_DESCRIPTORS = frozenset(
    {
        DESCRIPTOR_MOTION,
        DESCRIPTOR_IGNITION,
        DESCRIPTOR_DRIVER_DOOR,
        DESCRIPTOR_DEEP_SLEEP,
        DESCRIPTOR_LOCK,
    }
)

# Numeric readings, keyed to the payload field each one lands in.
_READING_FIELDS = {
    DESCRIPTOR_ODOMETER: "odometer_km",
    DESCRIPTOR_FUEL: "fuel_level",
}
_GPS_DESCRIPTORS = frozenset({DESCRIPTOR_GPS_LAT, DESCRIPTOR_GPS_LON, DESCRIPTOR_GPS_ALT})

# Latitude and longitude arrive in separate messages; only pair readings this close together,
# so a fresh latitude never pairs with the previous park's longitude.
_GPS_PAIR_WINDOW_S = 10

# Canonical signal names (must match the weights maps in events/got_into|got_out once fused)
SIG_STARTED = "car_started_moving"
SIG_STOPPED = "car_stopped_moving"
SIG_IGNITION_OFF = "car_ignition_off"    # STRONG end anchor: ignition off = actually parked
SIG_IGNITION_ON = "car_ignition_on"      # start corroborator
SIG_DOOR_OPEN = "car_driver_door_opened"
SIG_DEEP_SLEEP = "car_deep_sleep"
SIG_LOCKED = "car_locked"                # directional exit cue (observation only — no weight yet)
SIG_UNLOCKED = "car_unlocked"            # directional entry cue (observation only — no weight yet)
SIG_ODOMETER = "car_odometer"            # km reading; consecutive deltas prove a drive happened
SIG_FUEL = "car_fuel_level"
SIG_LOCATION = "car_location"            # fused lat/lon — the car-native park position
SIG_STATE_CHANGE = "car_state_change"    # catch-all {descriptor, value} for the rest of the stream


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


# Lock status is a string enum, NOT a boolean — `_as_bool` can't read it. Only the values we
# are confident about are mapped; anything else returns None and is logged once (via
# _note_unmapped) so we learn the car's real vocabulary instead of guessing at it. Notably
# SELECTIVE_LOCKED ("locked except the driver door") is left unmapped on purpose: whether it
# means arriving or leaving is exactly the question this observation run answers.
_LOCK_SECURED = {"secured", "locked"}
_LOCK_OPEN = {"unlocked"}


def _lock_is_secured(value) -> bool | None:
    """SECURED/UNLOCKED → True/False. None for any other (or unrecognized) state."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v in _LOCK_SECURED:
        return True
    if v in _LOCK_OPEN:
        return False
    return None


def _as_number(value) -> float | int | None:
    """Numeric readings (odometer, fuel, lat/lon) → a number. None if unreadable.

    Kept separate from `_as_bool` on purpose: booleans coerce "false"→False, which would
    silently turn a legitimate 0 km odometer into a boolean and lose the reading.
    """
    if isinstance(value, bool):  # bool is an int subclass — reject before the numeric check
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
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
        self._last: dict[str, object] = {}
        self._seen: set[str] = set()
        # (value, ts) per GPS component, pending fusion into one car_location.
        self._gps: dict[str, tuple[float, int]] = {}
        self._last_point: tuple[float, float] | None = None

    def _note_descriptor(self, descriptor: str, value) -> None:
        """Log each descriptor's first appearance — ONCE per descriptor per process.

        This is the stream inventory. It predates the catch-all below (when most of the
        stream was dropped, the log was the *only* evidence a descriptor existed) and is
        still worth keeping for two reasons: it covers the mapped-but-never-changing case
        that emits nothing — which is exactly how we tell "this X1 never sends isMoving"
        from "we never looked" — and one grep gives the whole vocabulary without a query.
        """
        if descriptor in self._seen:
            return
        self._seen.add(descriptor)
        log.info("descriptor in stream: %s = %r", descriptor, value)

    def process(self, raw_msg: dict) -> list[tuple[str, int, dict]]:
        """Return a list of (event_name, timestamp_epoch, extra) to ingest."""
        out: list[tuple[str, int, dict]] = []
        for descriptor, value, ts in self._iter_updates(raw_msg):
            # Inventory FIRST, before any edge logic — doing it after the baseline check
            # would hide any descriptor that only ever shows up once.
            self._note_descriptor(descriptor, value)

            if descriptor in _EDGE_DESCRIPTORS:
                out.extend(self._edge(descriptor, value, ts))
            elif descriptor in _READING_FIELDS:
                out.extend(self._reading(descriptor, value, ts))
            elif descriptor in _GPS_DESCRIPTORS:
                out.extend(self._gps_point(descriptor, value, ts))
            else:
                out.extend(self._state_change(descriptor, value, ts))
        return out

    # --- the three rules -----------------------------------------------------------------

    def _edge(self, descriptor: str, value, ts: int) -> list[tuple[str, int, dict]]:
        """Boolean transition → a directional signal. Baseline-silent."""
        # Lock status is a string enum; everything else here is boolean-ish.
        b = _lock_is_secured(value) if descriptor == DESCRIPTOR_LOCK else _as_bool(value)
        if b is None:
            # Unreadable value: leave the baseline alone rather than poisoning it, so the
            # next legitimate edge is still detected.
            log.info("unreadable value for %s: %r", descriptor, value)
            return []
        prev = self._last.get(descriptor)
        self._last[descriptor] = b
        # Skip when there's no prior (first observation → establish baseline SILENTLY, so a
        # parked car's initial/retained motion=false doesn't mint a phantom
        # car_stopped_moving on every startup/reconnect) or when unchanged (no edge).
        if prev is None or prev == b:
            return []

        extra = {"source_descriptor": descriptor}
        if descriptor == DESCRIPTOR_MOTION:
            return [((SIG_STARTED if b else SIG_STOPPED), ts, extra)]
        if descriptor == DESCRIPTOR_IGNITION:
            return [((SIG_IGNITION_ON if b else SIG_IGNITION_OFF), ts, extra)]
        if descriptor == DESCRIPTOR_DRIVER_DOOR and b:  # OPEN edge only
            return [(SIG_DOOR_OPEN, ts, extra)]
        if descriptor == DESCRIPTOR_DEEP_SLEEP and b:
            return [(SIG_DEEP_SLEEP, ts, extra)]
        if descriptor == DESCRIPTOR_LOCK:
            return [((SIG_LOCKED if b else SIG_UNLOCKED), ts, extra)]
        return []

    def _reading(self, descriptor: str, value, ts: int) -> list[tuple[str, int, dict]]:
        """Numeric reading → an event carrying the value. NOT baseline-silent.

        The value is the fact, so there is no phantom edge to guard against and the first
        observation is worth having; de-duplication (below) is what stops a re-sent state
        dump from emitting on every reconnect.
        """
        num = _as_number(value)
        if num is None:
            log.info("unreadable value for %s: %r", descriptor, value)
            return []
        if self._last.get(descriptor) == num:
            return []
        self._last[descriptor] = num
        name = SIG_ODOMETER if descriptor == DESCRIPTOR_ODOMETER else SIG_FUEL
        return [(name, ts, {_READING_FIELDS[descriptor]: num, "source_descriptor": descriptor})]

    def _gps_point(self, descriptor: str, value, ts: int) -> list[tuple[str, int, dict]]:
        """Fuse lat/lon (+altitude) into ONE car_location — see the module docstring."""
        num = _as_number(value)
        if num is None:
            log.info("unreadable value for %s: %r", descriptor, value)
            return []
        self._gps[descriptor] = (num, ts)

        lat = self._gps.get(DESCRIPTOR_GPS_LAT)
        lon = self._gps.get(DESCRIPTOR_GPS_LON)
        if lat is None or lon is None:
            return []  # half a coordinate locates nothing
        if abs(lat[1] - lon[1]) > _GPS_PAIR_WINDOW_S:
            return []  # different batches — wait for this one's other half
        point = (lat[0], lon[0])
        if point == self._last_point:
            return []  # a re-sent state dump, not a move
        self._last_point = point

        extra = {"lat": lat[0], "lon": lon[0], "source_descriptor": _NAV}
        alt = self._gps.get(DESCRIPTOR_GPS_ALT)
        if alt is not None and abs(alt[1] - max(lat[1], lon[1])) <= _GPS_PAIR_WINDOW_S:
            extra["altitude"] = alt[0]
        return [(SIG_LOCATION, max(lat[1], lon[1]), extra)]

    def _state_change(self, descriptor: str, value, ts: int) -> list[tuple[str, int, dict]]:
        """Everything else that CHANGES → one generic event carrying descriptor + value.

        Baseline-silent like an edge: these are discrete states (windows, alarm, trunk),
        so without it the full state dump on every reconnect would emit ~20 events.
        """
        if not isinstance(value, (str, int, float, bool)) and value is not None:
            log.info("unserializable value for %s: %r", descriptor, value)
            return []
        prev = self._last.get(descriptor, _UNSET)
        self._last[descriptor] = value
        if prev is _UNSET or prev == value:
            return []
        return [(SIG_STATE_CHANGE, ts, {"descriptor": descriptor, "value": value})]

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
