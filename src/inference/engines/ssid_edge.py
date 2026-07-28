"""SSID-edge engine — a directional presence signal from the network the phone is joined to.

The problem this solves: every phone-side car signal we had was **direction-ambiguous**.
`car_lock_state_change` fires on lock and unlock alike, `car_driver_door_opened` on entry and
exit alike, so neither can say *which side* of a boundary it is on — the root of the phantom
exit-at-entry class (issue #2) and of the weight-tuning fight in ADR 0005. The car's own lock
descriptor turned out no better (issue #3: it fires on drive-away *and* walk-away).

The car's WiFi hotspot is different: the phone **associates** with it on getting in and
**leaves** it on getting out, so the transition itself carries the direction. One definition per
(SSID, direction) — `connect` fires on the not-joined -> joined edge, `disconnect` on the
reverse — mirroring `geofence`'s per-(region, direction) split, and feeding the windowed engines
through the runtime's in-process recursion exactly the same way.

**Association, not availability.** Probing whether the SSID is *in range* would not work, and
this is the same structural point as ADR 0007: an edge detector needs a sample on each side of
the boundary, and a hotspot is in range while you approach it *and* while you walk away. Only
the association state has a genuine edge. (It is also the only one iOS exposes — Shortcuts can
read the joined network, not scan for nearby ones.)

**Why the direction is decided here and not in the producer.** A retry loop in the Shortcut that
concluded "this was an entry, not an exit" would be thresholding before Kafka, which invariant 19
forbids: a producer that misjudges destroys the evidence, while this engine misjudging is fixable
by re-running `scripts/rederive.py` over the retained pings. So the producer reports the joined
network as a plain field and the inference happens here.

**`sources` is a correctness gate, not an optimisation.** The field is *absent* when the phone is
off WiFi (Overland omits the key entirely — 941 of 1485 pings, and never an explicit null), so
"absent" has to be read as "not joined". But a producer that never reports the field at all is
then indistinguishable from one reporting "off WiFi" — and `location_ping` has two producers:
OwnTracks emitted 110 pings and reported WiFi on *none* of them. Ungated, a single interleaved
OwnTracks ping mid-drive would read as a disconnect and mint a phantom exit. So a definition
declares which producers are authoritative for its field, and pings from anywhere else do not
touch state.
"""

from inference.engines.base import Decision, ScopedState, register_engine


@register_engine("ssid_edge")
class SsidEdgeEngine:
    name = "ssid_edge"   # static engine-type identity (also stamped by register_engine)

    def __init__(self, config: dict):
        self.ssid = str(config["ssid"])
        self.direction = config["direction"]                    # "connect" | "disconnect"
        if self.direction not in ("connect", "disconnect"):
            raise ValueError(
                f"ssid_edge direction must be connect|disconnect, got {self.direction!r}"
            )
        # The message field carrying the joined network's name. Configurable so the engine is
        # not welded to one producer's vocabulary (Overland calls it `wifi`).
        self.field = str(config.get("field", "wifi"))
        # Producers authoritative for `field` — see the module docstring. Empty/absent means
        # "trust every producer", which is only safe when every producer of this event name
        # reports the field.
        self.sources = frozenset(config.get("sources") or ())
        self.event_name = str(config.get("event_name", "location_ping"))

    def input_event_names(self) -> set[str]:
        return {self.event_name}

    def decide(self, event: dict, state: ScopedState) -> Decision | None:
        # The producer gate reads the ENVELOPE, not the message — `source_app` is the wrapper
        # field Vector/the runtime stamp, and it is what distinguishes two producers of the
        # same event name.
        if self.sources and event.get("source_app") not in self.sources:
            return None

        msg = event.get("message") or {}
        now = int(msg.get("timestamp", 0))

        current = msg.get(self.field)
        if isinstance(current, str):
            current = current.strip() or None                   # "" is off-WiFi, not an SSID
        elif current is not None:
            current = str(current)

        seen = bool(state.get("seen", False))
        prev = state.get("last")
        prev_ts = int(state.get("ts", 0))

        # Out-of-order guard. Overland posts batches (up to 1000 fixes per request) whose
        # internal order is not guaranteed, so a ping OLDER than the last state-changing one
        # can arrive after it. Without this, pings (t=100 off) (t=200 joined) (t=150 off)
        # processed in that order would read the last one as a disconnect at t=150 and mint a
        # phantom exit. Judged against the last CHANGE, so steady-state pings still cost no
        # state write (`ts` is only stored alongside a real transition — see below).
        if seen and now < prev_ts:
            return None

        if not seen:
            # Baseline-silent first observation: it establishes state only. A phone already
            # sitting in the car when the runtime starts (or after a state reset) must not
            # mint an entry it never crossed — the same rule the BMW mapper applies to its
            # edge descriptors.
            state.set("last", current)
            state.set("ts", now)
            state.set("seen", True)
            return None

        if current == prev:
            return None                                         # no edge; no state write

        state.set("last", current)
        state.set("ts", now)

        was_on = prev == self.ssid
        now_on = current == self.ssid
        fires = (now_on and not was_on) if self.direction == "connect" else (was_on and not now_on)
        if not fires:
            return None                                         # a transition, but not ours
        return Decision(occurred_at=now, score=1.0, sources=(event,))
