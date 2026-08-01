"""Session pairing with a displacement guardrail — "a trip with no displacement is not a trip".

`session_window` pairs a start with an end and asks no questions about what happened in
between, so it faithfully mints whatever the detectors hand it — including a *time-inverted*
trip. Live case (2026-07-27, issue #23): `got_out` fired on the real EXIT and `got_into` on
the real ENTRY, so `car_trip [11:58:19 -> 12:13:37]` recorded a 15-minute drive across the
span the phone was demonstrably parked at a vet (14 fixes, spread 42.9m, plus a card payment
to the practice mid-window). Both detectors were direction-ambiguous; the session engine had
no way to notice.

This engine adds the one check that needs no tuning, because it is a physical fact rather
than a threshold over noisy evidence: **while the session was open, did the entity actually
go anywhere?** Measured over every `car_trip` since the Overland lane landed (2026-07-24),
bounding-box extent of the accuracy-gated fixes inside each trip span:

    07-25 09:43   783s    5 fixes   4121m   coverage 0.82   accept
    07-25 11:33   811s   70 fixes   4202m   coverage 0.98   accept
    07-27 08:47   863s   71 fixes   6351m   coverage 0.84   accept
    07-27 11:51   606s   56 fixes   1533m   coverage 0.97   accept
    07-27 12:13   918s    9 fixes     34m   coverage 0.97   REJECT   <- the phantom
    07-27 12:23   612s   49 fixes   3885m   coverage 0.88   accept
    07-24 07:46   899s    1 fix        --   --              abstain (too few fixes)

The phantom sits 45x below the nearest real trip, so `min_displacement_m` 300 has ~5x margin
on one side and ~9x on the other. That gap is why this is worth doing *before* the direction
work (issue #23 P2, blocked on lock history): it kills the phantom class now, from a signal
that is already abundant, and it stays correct however the detectors are later retuned.

**Extent, not net displacement.** The metric is the diagonal of the bounding box of the
fixes, not the distance from first fix to last. A drive that returns to where it started
(out to a shop and back inside one session) has ~zero net displacement but a large box, and
it is a real trip. Net displacement would reject it.

**Abstain and reject are deliberately asymmetric.** Sparse GPS is not evidence of a phantom,
it is absence of evidence, so anything short of a confident refutation emits the trip:
  - fewer than `min_fixes` fixes            -> abstain (the 07-24 trip has exactly one)
  - fixes spanning < `min_coverage_ratio`   -> abstain (they don't cover the trip; Overland
    batches, and a fix 714s late arriving after newer ones is real — see stay_window)
Only a well-covered, well-sampled, demonstrably stationary span is refused. Getting this
backwards would trade a false-positive class for a false-negative one, which is a worse deal:
a phantom trip is visible and correctable, a silently-dropped real trip is neither.

**Both fix filters exist to protect the ACCEPT direction, which is the dangerous one.** A
single confidently-wrong fix inflates the bounding box and waves a phantom straight through —
and that is not hypothetical: the case that motivates `geo.DEFAULT_MAX_SPEED_KMH` is a fix
reporting `acc: 5` while sitting 700m away on the phone's home coordinates. 700m would clear
any sane `min_displacement_m`. So vague fixes are dropped (`max_accuracy_m`) and impossible
jumps are dropped (`is_implausible_jump`), exactly as the two geometry engines already do.

Out-of-order fixes are skipped rather than merged: the plausibility guard is sequential and
cannot judge a point that predates the last accepted one. That biases coverage *downward*,
hence toward abstaining — the safe direction.

**The fixes are NOT added to the decision's sources.** Lineage stays `(start, end)`, because
the `interval` capability projects the trip's span from the lineage extent — folding 60
location pings in would rewrite `started_at`/`ended_at` to the fix range and corrupt the very
span this engine exists to validate. The fixes are evidence *about* the session, not part of
it. Consequently the tracker keeps a running bounding box (10 floats, O(1)) rather than the
fix bodies, unlike `stay_window`, which must retain them because there the fixes *are* the
lineage.

A rejected session is still consumed (the open start is cleared either way), and `got_into` /
`got_out` are emitted independently — only the unsupported *span* is withheld. The raw
signals are retained, so `scripts/rederive.py` can rebuild the trip if the rule is later
found wrong.
"""

import logging

from inference.engines.base import Decision, ScopedState, register_engine
from inference.engines.session_window import SessionWindowEngine
from inference.geo import DEFAULT_MAX_SPEED_KMH, haversine_m, is_implausible_jump

log = logging.getLogger(__name__)


@register_engine("validated_session_window")
class ValidatedSessionWindowEngine(SessionWindowEngine):
    name = "validated_session_window"   # static engine-type identity (also stamped by register_engine)

    def __init__(self, config: dict):
        super().__init__(config)
        # Below this bounding-box extent the entity did not go anywhere, so the session is not
        # a trip. 300m: ~5x under the shortest real trip measured (1533m), ~9x over the phantom
        # (34m). Re-measure before moving it; this is a physical bound, not a tuning knob.
        self.min_displacement_m = float(config.get("min_displacement_m", 300))
        # Below this many accepted fixes there is nothing to conclude — abstain.
        self.min_fixes = int(config.get("min_fixes", 3))
        # The accepted fixes must span at least this fraction of the session to refute it.
        self.min_coverage_ratio = float(config.get("min_coverage_ratio", 0.5))
        # Mirrors stay_window: a fix vaguer than this can't place you. Guards the ACCEPT side.
        self.max_accuracy_m = float(config.get("max_accuracy_m", 100))
        self.max_speed_kmh = float(config.get("max_speed_kmh", DEFAULT_MAX_SPEED_KMH))
        # Named, not hardcoded, so this engine stays a strategy rather than a location policy.
        self.location_event = config.get("location_event", "location_ping")

    def input_event_names(self) -> set[str]:
        return super().input_event_names() | {self.location_event}

    def decide(self, event: dict, state: ScopedState) -> Decision | None:
        msg = event.get("message") or {}
        name = msg.get("name")

        if name == self.location_event:
            self._track(msg, state)
            return None

        decision = super().decide(event, state)      # pairing + open-start bookkeeping

        if name == self.start_event:
            state.set("track", None)                 # a new session tracks from scratch
            return decision                          # (always None — a start never fires)

        if name != self.end_event:
            return decision

        # An end normally settles the session, so the tracker is cleared whether or not a
        # decision came back (a stale start returns None and must not leak its track).
        #
        # EXCEPT a time-inverted end (issue #38), which the base engine rejects while leaving
        # the start OPEN. `open` still being set is the signal that nothing was settled — and
        # the track must survive, because it holds the displacement evidence gathered so far
        # for a session that is still running. Clearing it would hand the eventual real end an
        # empty bounding box, which reads as "went nowhere" and would veto a genuine trip.
        if state.get("open") is not None:
            return decision                          # session still open — keep tracking
        track = state.get("track")
        state.set("track", None)
        if decision is None:
            return None
        return self._validated(decision, track)

    def _track(self, msg: dict, state: ScopedState) -> None:
        """Fold one fix into the open session's bounding box. No-op when no session is open —
        fixes outside a trip say nothing about it, and accumulating them would be unbounded.

        A fix whose *event-time* predates the session is dropped even though it arrived during
        it. Routing order is arrival order (`ingested_at`), not event order, so a batched
        producer routinely delivers an old fix mid-session — and one is enough to defeat both
        abstain guards at once. Real case caught in replay (2026-07-19 13:20 trip): a fix from
        11:26:44 landed just after the session opened, which pushed `n` from 2 to 3 past
        `min_fixes` AND stretched `f0` back 2h so coverage read 9.23 instead of ~0.8. The
        bounding box then measured two hours of sitting at home rather than the drive, and a
        real trip was suppressed. Clamping to the session makes coverage a true fraction.
        """
        open_ = state.get("open")
        if open_ is None:
            return
        if int(msg.get("timestamp", 0)) < int(open_.get("ts", 0)):
            return
        lat, lon = msg.get("lat"), msg.get("lon")
        if lat is None or lon is None:
            return
        acc = msg.get("acc")
        if acc is not None and float(acc) > self.max_accuracy_m:
            return                                   # too vague to place — can't widen the box
        lat, lon, ts = float(lat), float(lon), int(msg.get("timestamp", 0))

        t = state.get("track")
        if t is None:
            state.set("track", {"n": 1, "la0": lat, "la1": lat, "lo0": lon, "lo1": lon,
                                "f0": ts, "f1": ts, "lat": lat, "lon": lon, "ts": ts})
            return
        if ts < t["ts"]:
            return                                   # late arrival: the guard below needs order
        if is_implausible_jump(t["lat"], t["lon"], t["ts"], lat, lon, ts, self.max_speed_kmh):
            return                                   # a bad fix must not inflate the box
        t["n"] += 1
        t["la0"], t["la1"] = min(t["la0"], lat), max(t["la1"], lat)
        t["lo0"], t["lo1"] = min(t["lo0"], lon), max(t["lo1"], lon)
        t["f1"] = max(t["f1"], ts)
        t["lat"], t["lon"], t["ts"] = lat, lon, ts
        state.set("track", t)

    def _validated(self, decision: Decision, track: dict | None) -> Decision | None:
        """The session stands unless the location stream confidently refutes it."""
        if not track or track["n"] < self.min_fixes:
            return decision                          # not enough sampling to judge
        start_ts = int((decision.sources[0].get("message") or {}).get("timestamp", 0))
        duration = max(1, int(decision.occurred_at) - start_ts)
        coverage = (track["f1"] - track["f0"]) / duration
        if coverage < self.min_coverage_ratio:
            return decision                          # fixes don't span the session
        extent = haversine_m(track["la0"], track["lo0"], track["la1"], track["lo1"])
        if extent >= self.min_displacement_m:
            return decision
        log.info(
            "SUPPRESSED %s: no displacement — extent %.0fm < %.0fm over %ds "
            "(%d fixes, coverage %.2f)",
            self.name, extent, self.min_displacement_m, duration, track["n"], coverage,
        )
        return None
