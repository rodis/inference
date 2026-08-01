import { Car, Route, LogIn, LogOut, DoorOpen, DoorClosed, KeyRound, Smartphone, Plug, BatteryCharging, CreditCard, House, MapPin, Circle } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { AwareEvent } from "./types";

export const VERBS: Record<string, string> = {
  car_trip: "Car trip", got_into_the_car: "Got into the car", got_out_the_car: "Got out of the car",
  // A `trip` titles itself by its MODE (see labelOf / MODE_NOUN); this is the fallback for a
  // journey where no fix claimed one.
  trip: "Trip",
  car_door_opened: "Car door opened", car_door_closed: "Car door closed", phone_is_charging: "Phone charging",
  // Retired 2026-08-01 (issue #6) — labels kept because 34 historical events are still in
  // Neon and would otherwise render unlabelled on the timeline.
  arrived_home_by_car: "Arrived home by car", left_home_by_car: "Left home by car",
  // Fallback only: a `stay` that matched a known place is labelled with the place itself
  // (see labelOf), because "Konditorei von Rotz Baar" says more than "Stay" ever will.
  stay: "Stay",
};
export const RAW_LABEL: Record<string, string> = {
  device_connected_to_power: "Power connected", device_disconnected_from_power: "Power disconnected",
  device_connected_to_carplay: "CarPlay connected", device_disconnected_from_carplay: "CarPlay disconnected",
  car_lock_state_change: "Car lock changed",
  credit_card_payment: "Card payment",
  location_ping: "Location ping",
  car_driver_door_opened: "Driver door opened",
};
export const CAT: Record<string, { c: string; Icon: LucideIcon }> = {
  car_trip: { c: "#3d6cf7", Icon: Car }, got_into_the_car: { c: "#18b26b", Icon: LogIn }, got_out_the_car: { c: "#12a89b", Icon: LogOut },
  car_door_opened: { c: "#7a5bff", Icon: DoorOpen }, car_door_closed: { c: "#9b7bff", Icon: DoorClosed }, car_lock_state_change: { c: "#e0567f", Icon: KeyRound },
  device_connected_to_carplay: { c: "#6b5bff", Icon: Smartphone }, device_disconnected_from_carplay: { c: "#8a7cff", Icon: Smartphone },
  device_connected_to_power: { c: "#f5a524", Icon: Plug }, device_disconnected_from_power: { c: "#e0892a", Icon: Plug },
  phone_is_charging: { c: "#27ae60", Icon: BatteryCharging },
  credit_card_payment: { c: "#14b8a6", Icon: CreditCard },
  car_driver_door_opened: { c: "#7a5bff", Icon: DoorOpen },
  arrived_home_by_car: { c: "#c2557f", Icon: House }, left_home_by_car: { c: "#d1719b", Icon: House },
  // Warm brown, and deliberately the only warm colour: a stay is where the day actually
  // happened, so it should stand out on a board of blues and teals. Lifted from the
  // coffee-shop capsule in the parallel-lanes design sketch, which is what a stay turned out
  // to be in practice — most of them are a café.
  stay: { c: "#b4732f", Icon: MapPin },
  // Same blue as `car_trip`, on purpose: a journey is a journey, and on any day since the
  // Overland lane landed the `trip` is the one drawn (see SUPERSEDED_BY), so a drive must not
  // change colour depending on which event happened to express it. The icon carries the
  // difference — `Route` for a journey known from motion, `Car` for one known from the car's
  // own signals — which is the distinction that actually matters when both appear on one day.
  trip: { c: "#3d6cf7", Icon: Route },
};

/** Readable ink for something drawn ON a category fill (a capsule icon, a lineage tile).
 *
 *  Every category colour is currently dark enough for white, so this returns `#fff` throughout
 *  today — it exists because that is a property of the palette, not a rule. `stay` was briefly
 *  a light yellow and its white MapPin was close to invisible; asking here instead of
 *  hard-coding `#fff` at each site means the next light colour can't quietly erase its own icon.
 *  sRGB relative luminance, thresholded where white falls below ~3:1 (the non-text contrast
 *  floor). */
export const inkOn = (hex: string): string => {
  const chan = (i: number) => {
    const c = parseInt(hex.slice(1 + i * 2, 3 + i * 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  };
  const lum = 0.2126 * chan(0) + 0.7152 * chan(1) + 0.0722 * chan(2);
  return lum > 0.3 ? "#221a00" : "#fff";
};

// --- the level ladder -----------------------------------------------------------
// How many lanes the timeline has, and which one an event type lands in when the user
// hasn't said otherwise.
//
// The ladder is as tall as the deepest inference in view: a D3 event stands on two layers
// of reasoning and reads as a claim about your life, a D1 event is a wire reading, so
// **depth inverted is the lane** — deepest at the top. That makes a brand-new definition
// land somewhere sensible with zero configuration, which is the same bet as events-as-data.
//
// Depth is not importance, though, so the default is only a default: `credit_card_payment`
// is D1 (a raw webhook, no inference at all) and belongs in the headlines. Those exceptions
// are the *only* thing stored — see DataProvider.
//
// The floor keeps the board sane before the deep derivations exist; the ladder grows on its
// own the first time something deeper fires. Growing
// re-points every default one lane down, which is the known cost of tying height to depth.
export const LANE_FLOOR = 3;
export const laneCount = (maxDepth: number) => Math.max(LANE_FLOOR, maxDepth);
export const defaultLevelOf = (depth: number, lanes: number) =>
  Math.min(lanes, Math.max(1, lanes - depth + 1));

// Named for what you'd say out loud. The top is always the day at a glance and the bottom
// is always raw signal; a taller ladder fills in between, and past the pool it falls back
// to the bare number.
const LANE_TOP = "Headlines", LANE_BOTTOM = "Signals";
const LANE_MIDDLE = ["Activity", "Micro", "Traces", "Detail"];
export function laneNames(n: number): string[] {
  if (n <= 1) return [LANE_TOP];
  const middle = Array.from({ length: n - 2 }, (_, i) => LANE_MIDDLE[i] ?? `Level ${i + 2}`);
  return [LANE_TOP, ...middle, LANE_BOTTOM];
}
export const LANE_BLURB: Record<string, string> = {
  Headlines: "the day at a glance",
  Activity: "what you actually did",
  Micro: "the moving parts",
  Traces: "the steps between",
  Detail: "the fine grain",
  Signals: "raw wire readings",
};

/** Category colour + icon for an event type. The `entered_<slug>` / `left_<slug>` prefixes are
 *  kept for HISTORY. Nothing produces them any more — the OwnTracks lane that minted them and the
 *  geofence engine that would have were both removed 2026-08-01 — but the raw events are retained
 *  in Neon deliberately (invariant 19), and for 07-11..07-25 they are the only record of those
 *  visits: `stay` had almost no coverage before Overland's dense sampling. Without this they would
 *  render as anonymous dots. */
export const catOf = (name: string): { c: string; Icon: LucideIcon } => {
  if (CAT[name]) return CAT[name];
  if (name.startsWith("entered_")) return { c: "#2f9e8f", Icon: MapPin };
  if (name.startsWith("left_")) return { c: "#59b0a4", Icon: MapPin };
  return { c: "#9298a6", Icon: Circle };
};
const pad = (n: number) => String(n).padStart(2, "0");
export const fmtTime = (d: Date) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
export const fmtTimeSec = (d: Date) => `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
const titleize = (s: string) => { const t = s.replace(/_/g, " "); return t.charAt(0).toUpperCase() + t.slice(1); };
/** An event carrying the `place` capability names itself after the place it matched — the label
 *  IS the useful noun ("Konditorei von Rotz Baar", not "Stay"). Falls back to the verb when the
 *  place is unknown, which is the honest reading: we know you stopped, not where. */
/** How a journey titles itself: by its **mode**, not by its endpoints.
 *
 *  The first version put the route in the title — "Home → ENNETSeeKLINIK für Kleintiere" — on the
 *  reasoning that `place` does the same for a stay. Two things were wrong with that, both visible on
 *  a real board:
 *
 *  - **It breaks the layout.** Titles are 15px semibold inside `.ev-head`, a `flex-wrap` row that
 *    also carries the chips, sitting in a fixed-width lane. A route runs to 38 characters against a
 *    stay's 30, so it wrapped and shoved the capsules around. (`.ev-title` now truncates with an
 *    ellipsis instead, which is the general guard — but the real fix is not putting a two-ended
 *    string there.)
 *  - **It is incomplete far too often.** 9 of the first 21 journeys had an unlabelled end — 7 with
 *    one, 2 with neither — so "To Home" and "From Home" were the common case, not the edge. A title
 *    that is usually half-missing is not a title.
 *
 *  Mode is the better noun: always complete (it falls back to the verb), 4-5 characters so it can
 *  never be the thing that overflows, and it carries the fact that makes this event *generic* — a
 *  walk and a drive read differently at a glance, which is the whole reason `trip` exists alongside
 *  `car_trip`. The route is real information and keeps its place, one rung down, in `routeOf`. */
const MODE_NOUN: Record<string, string> = {
  driving: "Drive", walking: "Walk", cycling: "Ride", running: "Run",
};
const tripLabel = (e: AwareEvent): string | null => {
  const mode = e.message.journey?.mode;
  return mode ? MODE_NOUN[mode] ?? null : null;
};

/** Where a journey went, as **one** place rather than two — for the card's detail line, never its
 *  title.
 *
 *  One end, not both, for the same reason a stay names itself after a single place: it halves the
 *  length and picks the half that answers the question. Destination wins because "where did I end
 *  up" is what you want from a finished journey; the origin is the fallback, which is what makes
 *  this degrade cleanly instead of rendering an arrow with a blank on one side.
 *
 *  Null when neither end matched a POI — the caller then shows duration alone, which is honest.
 *  Adding the `poi` row and re-deriving upgrades it (ADR 0007: a label is frozen at derive time). */
export const routeOf = (e: AwareEvent): string | null => {
  const j = e.message.journey;
  if (!j) return null;
  if (j.destination?.label) return `to ${j.destination.label}`;
  if (j.origin?.label) return `from ${j.origin.label}`;
  return null;
};

export const labelOf = (e: AwareEvent) =>
  tripLabel(e) ??
  (e.message.place?.label
    ? e.message.place.label
    : e.event_class === "derived" ? VERBS[e.name] || titleize(e.name) : RAW_LABEL[e.name] || titleize(e.name));
export const typeLabel = (n: string) => VERBS[n] || RAW_LABEL[n] || titleize(n);

/** An event that knows *where* it happened but not *what* that place is: the `place` capability
 *  is there (centroid + spread — geometry, always known) but nothing in the place registry
 *  matched, so `labelOf` falls back to the bare verb ("Stay").
 *
 *  Drawn weaker on the timeline. The inference is not less *true* — you did stop here for 40
 *  minutes — it is less *resolved*, and a board where "Konditorei von Rotz Baar" and "Stay" carry
 *  identical weight overstates what the second one tells you. Keyed on the capability rather than
 *  on `name === "stay"` so the next place-carrying event inherits the treatment for free; and on
 *  the *label* rather than on dwell length or fix accuracy, because the fix is to add a `poi` row
 *  and re-derive (ADR 0007 — a label is frozen at derive time). The weaker drawing therefore
 *  reads as "not named yet", which is an invitation, not an error.
 *
 *  **Drawn hollow**: the same capsule unfilled — transparent, with a dotted ring in the category
 *  colour — plus a muted title. A half-opacity fill was tried first and rejected on a real day:
 *  it was too close to a named stay to read as a different kind of claim at a glance, and *faded*
 *  already means "below the current altitude" on this board, so it made one channel carry two
 *  readings. Hollow-vs-filled is a channel nothing else uses, and it separates cleanly from the
 *  row's altitude opacity (different element, different property). */
export const placeUnknown = (e: AwareEvent) => !!e.message.place && !e.message.place.label;
export const dayKey = (d: Date) => d.toISOString().slice(0, 10);

// --- presentation config (dashboard-owned) --------------------------------------
// Which derived events render as a time *span* (a duration capsule on the day timeline).
// The backend emits the `interval` capability as data; whether to *draw* an event as a
// span is a view decision, so it lives here, not in the event definition. Every event that
// carries an interval today (a journey, a charge, a stay) reads naturally as a duration, so
// each renders as a capsule whose length is proportional to how long it lasted.
//
// This list is an allowlist, so a NEW interval-carrying event defaults to being drawn as a
// point — which is a silent wrong answer, not a missing feature: `trip` shipped with its
// `interval` correct in Neon on all 20 events and still drew as a disc next to
// `credit_card_payment`. If you add a definition with `capabilities: [interval, …]`, add it
// here in the same change.
export const SPAN_EVENTS = new Set<string>(["car_trip", "trip", "phone_is_charging", "stay"]);
export const intervalOf = (e: AwareEvent) => e.message.interval ?? null;

/** Spans that another span on the same day re-expresses more completely — mapped
 *  `redundant -> replacement`, by NAME, deliberately.
 *
 *  `trip` and `car_trip` describe the same drive whenever it happened in your own car
 *  (ADR 0010): `trip` derives the span from motion and carries `journey` (labelled origin and
 *  destination, distance, mode) plus `vehicle` (which car boundaries corroborated it), while
 *  `car_trip` carries a bare interval derived by *pairing* those boundaries. Drawing both puts
 *  two capsules on one drive, which reads as a duplicate rather than as two views of it.
 *
 *  **Preference, not deletion.** `car_trip` still renders on days `trip` cannot see — it needs
 *  >= 4 moving fixes, so every drive before the Overland lane landed (2026-07-25) has a
 *  `car_trip` and no `trip`. Suppressing the type outright would blank those days; suppressing
 *  it only where a `trip` actually covers the same drive keeps the history intact. Whether
 *  `car_trip` retires at all is issue #42, and this deliberately does not prejudge it.
 *
 *  **Keyed on name, unlike `placeUnknown` and `isEverydayPlace` above.** Those keep to
 *  capabilities so the next place-carrying event inherits the treatment for free. There is no
 *  capability to key on here: `car_trip` says nothing structurally that distinguishes it from
 *  any other bare-interval event, so a rule like "an interval superseded by an overlapping
 *  `journey`" would also swallow `phone_is_charging`, which merely *overlaps* a drive rather
 *  than restating it. Supersession is an editorial claim that one event replaces another, so it
 *  is written down as one. */
export const SUPERSEDED_BY = new Map<string, string>([["car_trip", "trip"]]);

/** Ids of spans suppressed because their replacement is present and covers the same episode.
 *
 *  **Overlap, not containment.** `trip`'s bounds are settled fixes just outside the movement
 *  while `car_trip`'s are the boundary signals, so the pairing usually sits *inside* the
 *  journey — but not always: measured over 25 Jul - 1 Aug, on two of fourteen own-car drives
 *  the entry boundary preceded the journey's first settled fix by ~105 s. Containment would
 *  have let those two draw twice, which is the exact case this exists to prevent. */
export function supersededIds(events: AwareEvent[]): Set<string> {
  const out = new Set<string>();
  for (const e of events) {
    const replacement = SUPERSEDED_BY.get(e.name);
    const iv = intervalOf(e);
    if (!replacement || !iv) continue;
    for (const r of events) {
      if (r.name !== replacement) continue;
      const rv = intervalOf(r);
      if (rv && iv.started_at <= rv.ended_at && iv.ended_at >= rv.started_at) { out.add(e.id); break; }
    }
  }
  return out;
}

/** An event at an **everyday** place — the one you live in (reference data, the `everyday`
 *  column on a `regions` POI row; see `inference.capabilities`).
 *
 *  Kept off the day timeline entirely, and the reason is not "it's boring". A stay somewhere
 *  you *visit* has natural boundaries: you arrive, you leave, and the cluster's edges are the
 *  visit. Home has none — you're there for fourteen hours, iOS stops sampling while you sleep,
 *  and `max_gap_seconds` chops the cluster wherever the outage fell, so what surfaces is one
 *  arbitrary fragment per sampling gap (2026-07-25: 453s and 8686s for what was really "home
 *  all morning" and "home all afternoon"). No engine parameter turns that into episodes, so
 *  the honest move is to keep deriving the fact and not draw it.
 *
 *  A *hard drop*, like a type parked on the levels board — not a low reveal, which would leave
 *  an invisible card in the layout and the tab order at every altitude. The flag rides on the
 *  event rather than being a hardcoded label here, so switching it is one `regions` row and
 *  a "show everyday places" toggle is one boolean away (`filter` below), no re-derive. */
export const isEverydayPlace = (e: AwareEvent) => e.message.place?.everyday === true;

/** Whether to draw this event as a duration capsule — and therefore which *lane* of the day
 *  timeline it belongs to.
 *
 *  Deliberately only about **kind**, not about data quality. A 32-second `car_trip` is a
 *  phantom trip, but it is still a *trip*: filing it as a moment because it's short put
 *  `car_trip` in both lanes on the same day, which reads as a broken categorisation rather
 *  than as the bad inference it actually is. Short spans stay in the activity lane and get a
 *  floor on their capsule height (CAP_MIN) so they remain legible; if a noisy type crowds the
 *  lane, the fix is to demote it on the levels board, not to re-file it here. */
export const isSpan = (e: AwareEvent) => SPAN_EVENTS.has(e.name) && !!e.message.interval;
/** Which of the day timeline's two lanes an event belongs to: intervals on the left as
 *  capsules, points in time on the right as small discs on their own track. */
export const laneOf = (e: AwareEvent): "activity" | "moment" => (isSpan(e) ? "activity" : "moment");

/** A span's start on the clock (its capsule top); a point event has no extent, so its
 *  timestamp is both. Used to order and place events by *when they began*. */
export const startOf = (e: AwareEvent) => (isSpan(e) ? intervalOf(e)!.started_at : e.epoch);
/** When an event ends on the clock: a span's end; a point event's instant. */
export const endOf = (e: AwareEvent) => (isSpan(e) ? intervalOf(e)!.ended_at : e.epoch);

/** The innermost span whose interval covers this moment — its *host*.
 *
 *  Time containment, not lineage: a card payment is not `derived_from` the trip it happened
 *  during, it merely happened during it. That distinction is the whole point of the second
 *  lane, and it's why this can't be read off `derived_from`. Innermost (shortest) wins so a
 *  6-hour charge doesn't claim a payment that fell inside a 15-minute trip. */
export function hostOf(moment: AwareEvent, spans: AwareEvent[]): AwareEvent | null {
  let best: AwareEvent | null = null;
  for (const s of spans) {
    const iv = intervalOf(s);
    if (!iv || moment.epoch < iv.started_at || moment.epoch > iv.ended_at) continue;
    if (!best || iv.duration_seconds < intervalOf(best)!.duration_seconds) best = s;
  }
  return best;
}

export function humanDur(sec: number): string {
  sec = Math.round(sec);
  if (sec < 60) return sec + "s";
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return s ? `${m}m ${s}s` : `${m} min`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

/** Two-lane day layout on one shared time scale.
 *
 *  **The scale is the whole trick.** Instead of packing events into a single column and
 *  deriving each capsule's height from its duration independently, this builds one
 *  piecewise-linear time→y map for the day and places *everything* on it. A span's height is
 *  then simply `Y(end) − Y(start)` — a capsule is proportional to its duration *because* it
 *  sits on the same scale as the discs beside it, which is what lets a moment render inside
 *  the activity that contains it without any extra alignment maths.
 *
 *  The map is deliberately "broken": a step between two consecutive instants is proportional
 *  to the elapsed minutes (PPM), but floored at MIN_STEP so labels have room, *compressed* past
 *  MAX_STEP so a lull doesn't run off-screen (see `stepFor`), and a genuinely quiet stretch
 *  (over QUIET_MIN, and with no activity in progress across it — see `busy`) collapses to a
 *  short labelled divider. Two consequences worth knowing: a span crowded with moments grows
 *  taller than its duration alone implies (each interior instant costs at least MIN_STEP), and a
 *  busy hour therefore gets more room than a dead one — which reads correctly even though it
 *  isn't linear.
 *
 *  Because of that, a capsule's *length* is only ever roughly proportional to its duration, and
 *  the error is worst for the longest activity on the board (it eats the compression, while short
 *  ones are inflated by the MIN_STEP floor on their interior instants). Measured on a real day
 *  (2026-07-27) px-per-minute ran 2.5 → 14.7, so a 9× duration difference drew as under 3×.
 *
 *  **That is a floor, not a bug to tune out.** One vertical channel cannot carry both legibility
 *  and the feed's real dynamic range: a day holding a 3-minute charge and a 2h39 café visit spans
 *  50×, and a capsule needs ~44px for its icon, so a linear day would be thousands of px tall. So
 *  the capsule answers *when, in what order, for roughly how long, and containing what* — and the
 *  exact figure is left to the text on the card (`humanDur`), which is where it was all along. An
 *  exactly-proportional bar was tried on the card and removed; see `EventBody` for why.
 *
 *  **Lanes.** `laneOf` puts intervals left and points right. Concurrent spans are packed into
 *  sub-columns (greedy, by start) rather than interlocked with a notch: on a true time scale a
 *  6-hour charge genuinely *does* span a 15-minute trip, and the real feed has exactly that,
 *  so they need to sit side by side instead of colliding in one 40px column. Consecutive
 *  capsules within a column are joined by a dotted `link` across the dead time between them,
 *  so the lane reads as a track rather than as capsules floating on a background.
 *
 *  **Containment.** A moment inside a span gets a `band` — a tinted stripe across the moments
 *  lane covering the host's vertical range. Figure/ground rather than a tether line per dot,
 *  which stays quiet when a host contains five moments. Bands are emitted longest-first so the
 *  innermost (shortest) host paints on top.
 *
 *  Hidden events (faded out by altitude) are interpolated onto the same scale so they sit at
 *  their true time and grow into place when you descend. Positions are keyed per event id. */
const VIS_EPS = 0.06;
const PPM = 3.2;            // px per minute between consecutive instants
const MIN_STEP = 34;        // …floored, so two close events still have label room
const MAX_STEP = 96;        // …and compressed past here (30 min at PPM), so a lull can't dominate
const KNEE = 24;            // how gently: px of compressed growth per e-fold past MAX_STEP
const QUIET_MIN = 50;       // a gap wider than this (minutes) collapses to a divider
const GAP_H = 56;           // height of a collapsed-gap divider
const CAP_MIN = 44;         // shortest a duration capsule can be (its icon must fit)
const ROW_MIN = 34;         // smallest vertical room a moment row needs
const MAX_COLS = 3;         // concurrent-span sub-columns before we stop fanning out
const HANDOFF = 300;        // an overlap this short (seconds) is a boundary, not concurrency
const CAP_GAP = 3;          // hairline between two capsules stacked in one sub-column
const LINK_MIN = 8;         // shorter than this, a connector is a smudge — draw nothing
const PAD_BOTTOM = 56;

/** The px curve: `rawPx` (= elapsed minutes × PPM) linear up to MAX_STEP, log-compressed past it.
 *  Concave and strictly increasing, which is what the two rules below both lean on.
 *
 *  **Rank must survive.** This used to be a hard `min(MAX_STEP, …)` clamp, which is not monotone —
 *  a 109-minute stretch and a 4-hour one drew as exactly the same px, so a long activity could not
 *  read as longer than a medium one. A logarithm keeps every extra minute worth strictly-positive
 *  px forever, so longer always draws taller. The knee is C1 (both sides have slope 1 in px at the
 *  join), so no kink is visible where a stretch crosses MAX_STEP.
 *
 *  **But the capsule is not the duration channel.** MAX_STEP is deliberately low — 30 minutes at
 *  PPM — because the card states the exact duration in text right next to it (`humanDur`), so
 *  buying proportionality with page height buys nothing you can't already read. The vertical
 *  extent's remaining jobs are cheap: show that the activity persisted, contain the moments that
 *  fell inside it, and rank it against its neighbours. None of those needs px proportional to a
 *  lull. It was tuned the wrong way once — widened to 210/150 so a 2h39 café visit wasn't clipped
 *  at all, which drew that one stay 495px on a 1185px page and read as one visit eating the day.
 *
 *  The floor on compression is a **rank** one, not an aesthetic one: at 48/24 a 96-minute *quiet*
 *  stay drew shorter than a 19-minute *busy* trip (see `render-check`), because MIN_STEP is charged
 *  per interior instant and doesn't care about elapsed time. 96/24 clears that with margin. */
const compress = (rawPx: number): number =>
  rawPx <= MAX_STEP ? rawPx : MAX_STEP + KNEE * Math.log1p((rawPx - MAX_STEP) / KNEE);

export interface SpanBox { top: number; height: number; col: number }
/** `weak`: the host is an unnamed place (see `placeUnknown`) — its stripe is drawn fainter, so
 *  the containment claim carries the same confidence as the capsule casting it. */
export interface Band { hostId: string; top: number; height: number; color: string; weak: boolean }
export interface Link { top: number; height: number; col: number }
export interface DayLayout {
  pos: Map<string, number>;          // event id → top y (capsule top, or a moment's disc centre line)
  spans: Map<string, SpanBox>;       // span id → capsule box + which sub-column it sits in
  cols: number;                      // how many sub-columns the activity lane needs
  links: Link[];                     // dotted connectors between consecutive capsules in a column
  bands: Band[];                     // containment stripes, longest host first
  hosts: Map<string, string>;        // moment id → the span id containing it
  gaps: { y: number; seconds: number }[];
  h: number;
}

export function dayLayout(
  events: AwareEvent[],
  reveal: (e: AwareEvent) => number,
  colorOf: (name: string) => string,
): DayLayout {
  const pos = new Map<string, number>();
  const spans = new Map<string, SpanBox>();
  const links: Link[] = [];
  const bands: Band[] = [];
  const hosts = new Map<string, string>();
  const gaps: { y: number; seconds: number }[] = [];
  const empty = { pos, spans, cols: 1, links, bands, hosts, gaps, h: 40 };
  if (!events.length) return empty;

  const vis = events.filter((e) => reveal(e) > VIS_EPS);
  if (!vis.length) {                                  // nothing visible — hold places as slivers
    let y = 0;
    for (const e of events) { pos.set(e.id, y); y += 16; }
    return { ...empty, h: y + ROW_MIN };
  }

  // 1. the shared scale: every instant any visible event begins or ends at
  const visSpans = vis.filter(isSpan).sort((a, b) => startOf(a) - startOf(b) || endOf(b) - endOf(a));
  const instants = [...new Set(vis.flatMap((e) => [startOf(e), endOf(e)]))].sort((a, b) => a - b);

  /** What was going on across this stretch: the spans whose interval overlaps it — and since every
   *  span boundary is itself an instant, an overlap here means the span covers the *whole* stretch.
   *
   *  Used for two things. It keeps a *long, quiet activity* off the collapse path: an hour and a
   *  half at a café produces no location fixes at all (ADR 0007 — the reason `stay` clusters rather
   *  than fences), so the stay has zero interior instants and would otherwise collapse its own
   *  duration to a divider labelled "1h 36m quiet", drawn on top of its own capsule (which also
   *  crushed the capsule to CAP_MIN-ish, hiding that the drive home starts *before* the stay ends).
   *  And it's what lets each activity be charged for its dwell **once** — see below. */
  const covering = (a: number, b: number) => visSpans.filter((s) => startOf(s) < b && endOf(s) > a);

  // How much raw px each span has already been charged for time inside it. One budget per
  // activity, spent down across that activity's stretches — NOT a fresh price per stretch.
  //
  // Pricing each stretch on its own made a span's height depend on **how many pieces its interior
  // moments chopped it into**, which is meaningless to a reader: because `compress` is nearly flat
  // out here, three stretches each pay almost the full concave price, so the same 159 minutes cost
  // 314px chopped by two card payments and 166px unchopped. A 2h39 café visit was tall because of
  // where the card got tapped. Charging the *span* means the second and third stretch pay only the
  // margin — the curve's tail, a few px — and the capsule ends up the size its duration earns
  // (216px) rather than the size its interruptions earn.
  //
  // A stretch under several spans is priced by the most generous claim on it, so a nested activity
  // still gets its own room: a 15-minute trip inside a 6-hour charge is charged against the trip's
  // fresh budget, not squeezed into the tail of the charge's. An *uncovered* stretch has no budget
  // to draw on and simply pays the curve.
  const dwell = new Map<string, number>();

  const Yat = new Map<number, number>();
  let cur = 0;
  instants.forEach((t, i) => {
    if (i) {
      const prev = instants[i - 1], dm = (t - prev) / 60, raw = dm * PPM;
      const inside = covering(prev, t);
      if (dm > QUIET_MIN && !inside.length) { gaps.push({ y: cur + GAP_H / 2, seconds: t - prev }); cur += GAP_H; }
      else {
        let px = inside.length ? 0 : compress(raw);
        for (const s of inside) {
          const spent = dwell.get(s.id) ?? 0;
          px = Math.max(px, compress(spent + raw) - compress(spent));
          dwell.set(s.id, spent + raw);
        }
        cur += Math.max(MIN_STEP, px);
      }
    }
    Yat.set(t, cur);
  });

  // interpolate for anything not on an anchor (an event faded out by altitude)
  const Y = (t: number): number => {
    const exact = Yat.get(t);
    if (exact != null) return exact;
    if (t <= instants[0]) return Yat.get(instants[0])!;
    const last = instants[instants.length - 1];
    if (t >= last) return Yat.get(last)!;
    let i = 1;
    while (i < instants.length && instants[i] < t) i++;
    const a = instants[i - 1], b = instants[i];
    const ya = Yat.get(a)!, yb = Yat.get(b)!;
    return b === a ? ya : ya + (yb - ya) * ((t - a) / (b - a));
  };

  // 2. the activity lane: capsule boxes, then greedy sub-columns for concurrent spans.
  //
  // A second sub-column is expensive — it widens the whole lane — so it has to be earned by
  // *genuine* concurrency. Two activities that merely touch at their boundary are a **handoff**,
  // and the boundary is only approximately known: a stay ends when the next location fix breaks
  // its cluster, which lands after the drive away has already started, so a café visit and the
  // trip home overlap by a minute and used to fan out into two columns — reading as "two things
  // at once" for what is one thing following another. Within HANDOFF the column is reused and
  // the later capsule is butted below the earlier one (CAP_GAP), which costs it a few px of
  // truth about its start time in exchange for the lane staying single-file.
  const colEnds: number[] = [];                       // last occupied epoch per sub-column
  const colBottoms: number[] = [];                    // …and its px bottom, so a reuse can't overlap it
  for (const s of visSpans) {
    let col = colEnds.findIndex((end) => end <= startOf(s) + HANDOFF);
    if (col === -1) { col = Math.min(colEnds.length, MAX_COLS - 1); }
    const prevBottom = colBottoms[col];
    const top = Math.max(Y(startOf(s)), prevBottom != null ? prevBottom + CAP_GAP : 0);
    const height = Math.max(CAP_MIN, Y(endOf(s)) - top);
    // The activity lane is a track, not a set of floating capsules: a dotted connector runs down
    // the dead time between one capsule and the next *in the same column*, which is what makes a
    // day read as one thing after another. Deliberately per-column — two capsules in different
    // columns are concurrent, so a line between them would claim a sequence that isn't there.
    if (prevBottom != null && top - prevBottom >= LINK_MIN) {
      links.push({ top: prevBottom, height: top - prevBottom, col });
    }
    colEnds[col] = Math.max(colEnds[col] ?? -Infinity, endOf(s));
    colBottoms[col] = top + height;
    spans.set(s.id, { top, height, col });
    pos.set(s.id, top);
  }
  const cols = Math.max(1, colEnds.length);

  // 3. the moments lane, in time order, nudged apart only when they'd truly collide
  const visMoments = vis.filter((e) => !isSpan(e)).sort((a, b) => a.epoch - b.epoch);
  let lastY = -Infinity;
  for (const mo of visMoments) {
    const y = Math.max(Y(mo.epoch), lastY + ROW_MIN);
    pos.set(mo.id, y);
    lastY = y;
    const host = hostOf(mo, visSpans);
    if (host) hosts.set(mo.id, host.id);
  }

  // 4. one band per host that actually contains a visible moment, longest first so the
  //    innermost host paints last and therefore reads as the nearer container
  const hosted = new Set(hosts.values());
  for (const s of visSpans) {
    if (!hosted.has(s.id)) continue;
    const box = spans.get(s.id)!;
    bands.push({ hostId: s.id, top: box.top, height: box.height, color: colorOf(s.name), weak: placeUnknown(s) });
  }
  bands.sort((a, b) => b.height - a.height);

  // 5. everything still unplaced is below the current altitude — park it at its true time
  for (const e of events) if (!pos.has(e.id)) pos.set(e.id, Y(startOf(e)));

  return { pos, spans, cols, links, bands, hosts, gaps, h: cur + PAD_BOTTOM };
}



/** When a span is on screen, its capsule already represents its start and end (a car trip's
 *  get-in/get-out ARE the capsule's ends), so showing those contributor events as separate
 *  rows is redundant. Return the ids to fold into the capsule — the caller zeros their reveal.
 *  They stay in the lineage (tap the capsule to trace them); they just don't clutter the day. */
export function absorbedIds(events: AwareEvent[], reveal: (e: AwareEvent) => number): Set<string> {
  const out = new Set<string>();
  for (const e of events) {
    if (isSpan(e) && reveal(e) > VIS_EPS) for (const p of e.message.derived_from || []) out.add(p.id);
  }
  return out;
}

export interface Prepared {
  all: AwareEvent[];
  byId: Record<string, AwareEvent>;
  days: string[];
  derivLevel: (e: AwareEvent | undefined) => number;
  /** Every event *type* in the window, deepest first — the rows of the levels board. */
  types: string[];
  /** A type's derivation depth, or null for a type with no events in the window. */
  depthOf: (name: string) => number | null;
  /** Every depth this type appears at in the window (a definition can change shape). */
  depthsOf: (name: string) => number[];
  /** Deepest chain in view — the height of the level ladder (see laneCount). */
  maxDepth: number;
}

/** Decorate API events with epoch/date and expose a memoized derivation-level function
 *  over the lineage graph. (car_trip is now a real derived event carrying its own
 *  interval — no client-side synthesis; spans render from message.interval, see isSpan.) */
export function prepare(events: AwareEvent[]): Prepared {
  const evs = events.map((e) => ({ ...e, epoch: +e.occurred_epoch, date: new Date(+e.occurred_epoch * 1000) }));
  evs.sort((a, b) => a.epoch - b.epoch);
  const byId: Record<string, AwareEvent> = Object.fromEntries(evs.map((e) => [e.id, e]));
  const all = evs;
  // No pre-split by event_class: the two boards left both work over `all` and ask `isSpan` /
  // `event_class` per event. (A `raw` / `derived` pair lived here for the Signals table.)

  const memo: Record<string, number> = {};
  const derivLevel = (e: AwareEvent | undefined): number => {
    if (!e) return 1;
    if (memo[e.id] != null) return memo[e.id];
    memo[e.id] = 1; // guard against cycles
    const ps = (e.message.derived_from || []).map((p) => byId[p.id]).filter(Boolean);
    memo[e.id] = ps.length ? 1 + Math.max(...ps.map(derivLevel)) : 1;
    return memo[e.id];
  };

  // Depth per event *type*, which is what the level ladder is keyed on. A type's depth is a
  // property of its instances and changes when a definition changes shape, so the window can
  // hold more than one: `evs` is ascending, so last-write-wins reports the **current** shape.
  // (Taking the oldest pinned each badge to the most obsolete lineage — car_trip was built on
  // the intermediate car_door_* derivations, and reads straight off got_into/got_out since
  // ADR 0005.) `depthsOf` owns up to the older ones for the tooltip.
  const depthByType: Record<string, number> = {};
  const depthsByType: Record<string, Set<number>> = {};
  all.forEach((e) => {
    const d = derivLevel(e);
    depthByType[e.name] = d;
    (depthsByType[e.name] ??= new Set<number>()).add(d);
  });
  const types = Object.keys(depthByType).sort(
    (a, b) => depthByType[b] - depthByType[a] || a.localeCompare(b)
  );
  const depths = Object.values(depthByType);
  const maxDepth = depths.length ? Math.max(...depths) : 1;
  const depthOf = (name: string) => depthByType[name] ?? null;
  const depthsOf = (name: string) => [...(depthsByType[name] ?? [])].sort((a, b) => a - b);

  // Every day present in the loaded set. No cap needed here: the API already serves a
  // trailing window (DAY_WINDOW in api.ts), so this list is bounded at the source.
  const days = [...new Set(all.map((e) => dayKey(e.date)))].sort();
  return { all, byId, days, derivLevel, types, depthOf, depthsOf, maxDepth };
}
