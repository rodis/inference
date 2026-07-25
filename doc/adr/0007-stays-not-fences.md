# ADR 0007 — Places are stays, not fences

Status: **Accepted — implemented** (`stay_window` engine + [`events/stay.yml`](../../events/stay.yml)).
Date: 2026-07-25

> Builds on [`0002-recursive-derivation.md`](0002-recursive-derivation.md) (derived events are
> valid inputs) and the swappable-engine seam from
> [`0001-message-shaping-pipeline.md`](0001-message-shaping-pipeline.md). It adds one engine
> and one definition; the runtime is unchanged. It does **not** retire `geofence` — see
> *Both, for different jobs*.

## Context

Two things happened on 2026-07-24/25 that together settle how place detection should work.

**1. The phone became a real location source.** Adopting Overland (a movement tracker) next to
OwnTracks (a region sensor) took sampling from ~2 fixes per boundary crossing — about 100 in 13
days — to a fix every ~11s while moving, with accuracy p50 10m. See
[`doc/vector-pipeline.md`](../vector-pipeline.md) for the two lanes and why both exist.

**2. Edge detection turned out to be structurally wrong for small places**, for reasons no amount
of tuning fixes:

- **An edge needs a sample on each side of a boundary.** Inside a shop you stand still, iOS stops
  producing fixes, and the producer's min-distance filter suppresses what remains — so a 40m
  circle can receive **zero** fixes. No enter, therefore no leave, therefore no visit. Observed
  directly: a 13-minute drive produced 4 fixes and the destination exactly 1.
- **Every place must be declared before it can ever be seen.** A region that isn't in the
  `regions` table is invisible, so the model can only ever confirm what you already listed.
- **Two definitions per place**, each with per-entity state, against the Aiven 5-topic ceiling
  and one shared changelog ([ADR 0004](0004-scaling-model.md)).
- **The phone's own geofences were worse still**: iOS decided the same boundary anywhere within
  a 42–274m median scatter (max 701m) while reporting 10m accuracy, fired duplicate exits, and
  minted the *place name* on the phone — so a ~100m ring got a shop's name and could not be
  renamed or re-radiused server-side.

The deeper point: a *place* is not a boundary. It's **where you stopped and for how long**.
Boundary crossing is a proxy for that, and a lossy one.

## Decision

Add a `stay_window` engine that clusters the location stream instead of fencing it:

> Group consecutive fixes that stay within `radius_m` of a running centroid. When a fix breaks
> the cluster, emit one **`stay`** if the cluster lasted at least `min_dwell_seconds`, dated by
> the **last fix inside** and carrying every fix as lineage.

Consequences of that shape, in order of importance:

- **Sparse sampling degrades precision, not existence.** One fix during a 40-minute gap in
  movement still proves you were there. This is the property edge detection cannot have.
- **Places need not be declared.** A stay whose centroid matches nothing is still emitted
  ("42 min at 47.195,8.524"), so unknown places are *discovered* and can be labelled later —
  a discovery loop rather than declare-everything-first.
- **Naming is decoupled from detection.** The event is place-agnostic: one definition serves
  every place, and matching a centroid to a POI is a lookup downstream. Adding a place stops
  being a routing change.
- **Better POI coordinates than you can type.** A centroid over repeat visits beats both a
  hand-entered coordinate and a phone waypoint: the home stay's centroid landed 10m from truth
  from 16 fixes, while the OwnTracks enter-point that seeded the "konditorei" POI was itself
  78m off.
- It emits at **departure**, so a stay is knowable only once left. `occurred_at` is the last
  fix inside (the true end), not the fix that broke the cluster.

**Parameters are measured, not guessed** — `radius_m: 60`, `min_dwell_seconds: 300`,
`max_accuracy_m: 100`, `max_gap_seconds: 3600`. Replaying real history through the actual engine
produced exactly the right three stays for the day, with the 13-minute drive between them
fragmenting into singleton clusters that correctly emitted nothing:

```
07-24 22:41Z → 22:56Z   14.8 min    4 fixes  home (9m from truth)
07-25 09:12Z → 09:20Z    7.5 min    9 fixes  home (12m)
07-25 09:43Z → 11:20Z   96.8 min   28 fixes  the shop (78m from the seeded POI)
```

`max_gap_seconds` earns its place in that table: the overnight sampling outage (10.5h of
silence) split the two home stays instead of fusing them into one implausible overnight stay.

### Data quality is a shared concern, so it lives in `inference.geo`

Both geometry engines now import haversine **and a plausibility guard** from
[`src/inference/geo.py`](../../src/inference/geo.py). The guard rejects a fix implying
impossible travel from the last accepted one, because **reported accuracy is not a safety net**:
a real fix claimed `acc: 5` while sitting on the phone's home coordinates as the car drove 700m
away (2026-07-25 09:32), and out-of-order delivery is routine (one fix arrived 714s late, after
newer ones). An accuracy gate cannot see either failure. Clustering additionally ignores fixes
older than the cluster's end, since a late fix cannot extend a settled boundary.

### Both, for different jobs

`geofence` stays. It is the right tool for a **large, declared** region whose crossing is
reliably sampled *because you are moving through it* — `entered_home`/`left_home` feed
`arrived_home_by_car`/`left_home_by_car`, and a car crossing the home ring always produces fixes
on both sides. `stay_window` is for **dwell**, where the boundary is unknown or unsampled. One
detects transit through a known place; the other detects presence at any place.

## Consequences

- **Positive:** dwell becomes detectable at all; new places need no configuration; POI
  coordinates improve with use; one definition covers every place.
- **The label is missing by design.** `stay` carries a centroid, not a name. Turning stays into
  `visit_<place>` (or a `place_visit` carrying a label) needs a matching step that does not exist
  yet — the obvious next change, and the point at which the `regions` table becomes useful again
  as POI *data* rather than as routing config.
- **State cost is one write per fix** (the open cluster), which is inherent to clustering. The
  same change made `geofence` write `inside` only on change — it was writing a Kafka changelog
  record per fix per region for a value that almost never changed.
- **Emission lags reality by design**: no stay exists until you leave. Anything wanting "you are
  at X *now*" needs a different (open-cluster) read, not this event.
- **Tuning is now testable**: `scripts/backtest.py` was carrying only `{id,name,timestamp}` per
  event, so geometry engines silently derived nothing from a replay. It now carries the full
  persisted body, which is what let these parameters be measured against real fixes.

## Alternatives considered

- **Keep edge-only geofencing and shrink radii.** Fails on the zero-fixes-inside case, which is
  the common one for a shop; accuracy was never the binding constraint (p50 10m against a 40m
  radius), sampling continuity was.
- **Keep the phone's native geofences.** Rejected: 42–274m median decision scatter, duplicate
  exits, and place semantics minted on the phone (see Context).
- **Cluster in the dashboard instead of the pipeline.** Would make stays a presentation artifact
  that nothing else can derive from, breaking the ladder — `stay` should be as available to a
  future habit derivation as `car_trip` is.
- **A `visit_<place>` event per POI (naming inside the engine).** Rejected for now: it
  reintroduces per-place definitions, and forces a place list to exist before anything can be
  seen. Labelling belongs after detection.
