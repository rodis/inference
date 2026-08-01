# ADR 0010 — A trip is movement between two stays, not a car session

**Status:** accepted, implemented 2026-08-01
**Issue:** [#41](https://github.com/rodis/inference/issues/41)
**Relates to:** [ADR 0007 (stays, not fences)](0007-stays-not-fences.md) — this is its complement.
[ADR 0006 (car-native signals)](0006-car-native-trip-signals.md) and
[#23](https://github.com/rodis/inference/issues/23) are the other direction, and this ADR explains
why that direction has a ceiling this one does not.

## Context

`car_trip` derives a journey by pairing two detected boundaries: `got_into_the_car` and
`got_out_the_car`. Both are scored from **your car's peripherals** — the wireless charger, CarPlay,
the phone-as-key lock event, the BMW door. That framing has absorbed a lot of work: five ADRs, a
displacement guardrail, a session gate, and a documented conclusion (ADR 0009) that the weight maps
are at their ceiling and the remaining failure mode is ambiguity rather than unreliability.

None of that work can address a different failure mode, which had gone unnamed: **a journey that
your car is not involved in produces no signals at all.** It is not mistuned, not ambiguous, not
late. It is invisible, and no weight map, veto or second car-native source reaches it.

On **2026-07-30** the user drove to a vet in someone else's car. What the pipeline had:

| leg | span (Europe/Zurich) | pings | path | max vel | `motion:driving` | max gap |
|---|---|---|---|---|---|---|
| out | 14:52:10 → 15:12:44 (21 min) | 123 | 24.1 km | 119 km/h | 131/142 | 144 s |
| back | 15:32:49 → 15:50:35 (18 min) | 104 | 24.1 km | 112 km/h | 114/129 | 71 s |

Bounding-box extent 13.9 km each way — **46x** `car_trip`'s 300 m displacement guardrail. Mean
accuracy 8-23 m. No `wifi` on any ping of either leg (the user's own `BMW 73638` absent throughout),
which is the signature of the borrowed car. Both ends were already bracketed by real derived events:
`stay` **Home** (87 min) → `stay` **ENNETSeeKLINIK für Kleintiere** (19 min, centroid 3.1 m off the
POI) → `stay` **Home**.

So the day's timeline had a home→vet→home shape with a 20-minute, 24 km hole where the journey should
be. The evidence was not missing, thin, or ambiguous. Nothing consumed it.

Scale of the gap, over the 14 days to 2026-08-01: segmenting `location_ping` on `motion:driving` or
`vel > 15` into runs of ≥ 3 min and ≥ 300 m extent gives **26 movement segments, 6 with no
overlapping `car_trip`** — the two vet legs plus two more out-and-back ~13 km pairs on 25 and 26 July
(also vet trips, as it turned out).

## Decision

Derive a generic **`trip`** from motion in the raw location stream, with no car peripherals in the
path. A new `trip_window` engine, plus a new `journey` capability for the geography. `car_trip` stays
exactly as it is and becomes the car-*evidenced* specialisation rather than the only kind of journey.

`trip` is the **structural complement of `stay`** (ADR 0007), over the same stream and the same
geometry primitives, keyed on the cluster *breaking* rather than holding. ADR 0007 already measured
the material this leaves on the floor: at radius 60 m / dwell 300 s, a 13-minute drive "fragmented
into ~35 singleton clusters that correctly produced nothing." Those singletons are this event. A day
of `location_ping` decomposes into stays and the journeys between them, with no third category —
which is why neither engine needs the other's output, and why this is one definition rather than a
correlation between two.

Three choices carry the design:

**1. Motion comes from the stream's own classifier, with geometry as the last resort.** The ladder is
`motion` (iOS's `CMMotionActivity`, present on 77% of real fixes) → `vel` (87%) → speed implied
against the last accepted fix. The order matters in both directions: reading `vel` first ends a trip
at a red light where a car reports `vel` 0 while `motion` still says `driving`, fragmenting one
journey into two; treating an absent `motion` as a negative loses the ~10 fixes per leg that carry
neither field. A useful consequence is that `min_speed_kmh` never has to model walking — rung 1
already caught it — so it can sit above the 4-7 km/h `vel` noise that real fixes report while the
phone is sitting still at home.

This is also what makes the event generic rather than car-shaped: the mode is *read*, not assumed.
Over 14 days the stream classified 1713 fixes `driving`, 300 `walking`, 24 `cycling`, 388
`stationary`.

**2. Both bounds are settled fixes, not moving ones.** A trip is bounded by the last settled fix
before departure and the first settled fix after arrival. Clipping to the first and last *moving*
fix is the obvious implementation and it is wrong for a specific, measurable reason: on the vet trip
the first moving fix was ~600 m down the road, outside Home's POI radius, so the journey would have
lost its origin label. The whole value of the event is that it reads *Home → ENNETSeeKLINIK*.

The cost is that arrival is only knowable in retrospect: `settle_seconds` (180) distinguishes
arriving from stopping at a light, so non-moving fixes are buffered, spliced back into the trip if
motion resumes, and only promoted to "arrived" once the entity has stayed still that long. It sits
below `stay`'s `min_dwell_seconds` (300) so a trip closes before the stay it leads into opens.

**3. The guardrails are the ones the geometry engines already use, for the reasons already recorded.**
Bounding-box **extent** rather than net displacement (`validated_session_window`: a drive that returns
to its origin is still a drive), `max_accuracy_m` to drop fixes too vague to place, and
`is_implausible_jump` because a fix reporting `acc: 5` while sitting 700 m wrong is a real case and
would manufacture a journey out of standing still. `min_distance_m` 500 is what keeps drift from
becoming a trip: at the vet, 15 minutes and 510 m of *walking path* covered a box ~100 m across.

One guardrail deliberately points the **opposite way** to its counterpart. In
`validated_session_window`, sparse fixes must **abstain and emit**, because there they exist only to
*refute* a session detected from other evidence, and a silently-dropped real trip is worse than a
visible phantom. Here the fixes are the *only* evidence, so sparse sampling has nothing to report and
emitting anyway would be a fabrication rather than a graceful degradation. Same word, `min_fixes`,
inverted polarity — noted in both engines because getting it backwards in either place is a bug.

### `journey`, not `place`

`place` answers "where did this happen?" with one centroid over all the evidence. For a 24 km drive
that answer is a field beside the motorway. A journey's geography is two endpoints and what lies
between them: a different fact, not a variant of the same one, so `trip` declares a new `journey`
capability instead. Its endpoints are full `Place`s sharing the same reference-data lookup, so they
label exactly as a stay does. It reports both `straight_line_m` and `path_m` because a loop separates
them — out to a shop and back has the first near zero and the second at 20 km, and reporting only the
first would call a real journey a non-journey.

## Consequences

Replayed through the real core over 25 July - 1 August (the era with real ping density; before 25 July
the Overland lane produced 2-12 fixes a day and no `motion` at all):

- **20 trips, all `mode=driving`, shortest 15.6 min.** No sub-2-minute phantoms — the class
  [#26](https://github.com/rodis/inference/issues/26) tracks for `car_trip` does not appear here,
  because a run has to clear duration, fix-count *and* extent.
- **6 of the 20 have no `car_trip`**: the vet legs of 25, 26 and 30 July. These are the journeys the
  car-peripheral path structurally cannot reach.
- **1 `car_trip` has no `trip`**: 25 July 09:30, the lane's setup day — 8 fixes in 30 minutes,
  including the 700 m-wrong `acc: 5` fix. Below `min_fixes` there is nothing to conclude, which is
  the intended behaviour, not a regression.
- The 30 July vet trip derives as **Home → ENNETSeeKLINIK für Kleintiere, 25.6 min, 23.96 km path,
  142 sources**.

It also catches a `car_trip` failure independently. On 30 July the 13:11 `car_trip` is 15 seconds
long with time-inverted lineage (`got_out` 13:11:06 preceding `got_into` 13:11:21), and it consumed
the `got_into` belonging to the real 13:11:57→13:23:14 drive home, which therefore got no trip at all
— [#2](https://github.com/rodis/inference/issues/2) causing
[#26](https://github.com/rodis/inference/issues/26) causing
[#36](https://github.com/rodis/inference/issues/36) in one sequence. `trip` derives that drive as
*Konditorei von Rotz Baar → Home, 20.9 min*. Deriving the same fact two ways from independent
evidence is the reliability argument ADR 0006 makes for car-native signals, arrived at from the other
side: a second *independent source*, not more phone peripherals.

**Costs and open edges:**

- Two definitions now describe overlapping facts, so a consumer showing both will draw a car drive
  twice. Which to surface is a **presentation** decision and stays in the dashboard (the data model
  has carried no `role` since Stage 1), but it is real work that this ADR does not do.
- On sparse days a trip's span stretches: the 25 July legs read 50.4 and 57.4 minutes for an 11 km
  drive, against 25.6 on the densely-sampled 30th. The endpoints are right and the extent is right;
  the *duration* inherits the sampling. Worth watching, not worth a threshold yet.
- `trip` sees a **walk**, and none appeared in the replay window. `min_distance_m` 500 over
  bounding-box extent is calibrated against drives; the first real walk will be the test of it.

Existing history is rebuildable: the raws are retained, so `scripts/rederive.py --only trip`
backfills (invariant 19). That is how the numbers above were produced.

## Addendum — "a `car_trip` is a journey with a `got_into` and a `got_out`"

Added in the same change, on the observation that once the span comes from motion, the car boundaries
no longer need to be *directionally correct* — they only need to fall **inside** a journey that is
already known. A lock that fired at entry rather than exit still proves the car was involved, because
it no longer has to *be* the boundary. So car-ness became the **`vehicle` capability** on `trip`
rather than a second event.

Measured over 25 July - 1 August, boundaries contained strictly inside each replayed span:

| | own car | borrowed car |
|---|---|---|
| both boundaries (`confirmed`) | 12 | 0 |
| one boundary | 2 | 0 |
| none (no `vehicle` fragment) | 0 | 6 |

**Perfect separation on presence, with no threshold to tune.** Three findings shaped the
implementation:

**Derived boundaries, not raw signals.** The borrowed-car legs still had 0-5 *raw* car signals inside
their spans (lock, car wifi, door) — the user's own car sits at home and the phone touches it while
parked. Raw containment would have false-positived on 5 of the 6. The derived `got_into`/`got_out`
are clean because they already require co-occurrence.

**Zero tolerance on containment, which is both safer and more accurate.** A pad was the obvious
choice and is wrong twice over. A mark outside the span widens the `interval` capability, which
projects from the lineage extent — the exact corruption `validated_session_window` refuses its own
fixes for — and on the `ended_at` side it breaks `occurred_at == interval.ended_at`. It also measured
*worse*: at ±2 min, a phantom exit 31 s past a borrowed-car arrival leaked in and claimed the vehicle
(14/1/5 instead of 12/2/6 — one wrong). At zero, every own-car journey keeps evidence and no
borrowed-car one gains any. The cost is that two own-car trips are `evidence`-only rather than
`confirmed`, which is a strength signal, not a classification.

**Structural classification, so framework code names no concrete event.** The deriver treats a source
carrying coordinates as movement and one carrying none as corroboration, and reports whatever names it
found. Which signals corroborate is the definition's `corroborating_events` config. A bicycle lock
would work with no change to `capabilities.py`.

Two mechanics were needed in the engine. A **latch**, because the entry boundary fires when you get in
— before the first moving fix, so before the run exists (up to 15 minutes ahead of it on
sparsely-sampled mornings); recording only what arrives during an open run would drop entry evidence
on every trip and make `confirmed` unreachable. And the marks are **consumed on close**, so a later
journey cannot inherit this one's evidence.

Corroboration is strictly evidence: it can never open, extend or close a run, and the guardrails
judge a journey identically whether or not any is configured. A car-flavoured non-journey is still
not a journey.

A detail worth recording because it is the argument in miniature: on real data most trips' evidence
reads **`[got_out, got_into]`** — the exit *preceding* the entry. That is issue #2's phantom
exit-at-entry, sitting in the lineage, and the classification is correct anyway.

**Deliberately still open.** `car_trip` is unchanged and was not retired. The case for keeping it is
redundancy (`trip` needs ≥4 moving fixes, so it is contingent on one producer continuing to run —
pre-Overland, 18 of 18 `car_trip`s had no `trip`; post-Overland, 1 of 15) and latency (`trip` fires
**+6.4 min later** than `car_trip` on average, max +21 min, because arrival is only knowable after
`settle_seconds` plus a confirming fix — which matters for the outbound action lane, #18). What has
changed is that #2/#23/#26/#36 are no longer *correctness* bugs for the authoritative journey: the
span no longer depends on the pairing. Retiring `car_trip` is a separate decision with its own
evidence.
