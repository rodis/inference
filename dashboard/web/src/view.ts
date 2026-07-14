import { Car, LogIn, LogOut, DoorOpen, DoorClosed, KeyRound, Smartphone, Plug, BatteryCharging, CreditCard, Circle } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { AwareEvent } from "./types";

export const VERBS: Record<string, string> = {
  car_trip: "Car trip", got_into_the_car: "Got into the car", got_out_the_car: "Got out of the car",
  car_door_opened: "Car door opened", car_door_closed: "Car door closed", phone_is_charging: "Phone charging",
};
export const RAW_LABEL: Record<string, string> = {
  device_connected_to_power: "Power connected", device_disconnected_from_power: "Power disconnected",
  device_connected_to_carplay: "CarPlay connected", device_disconnected_from_carplay: "CarPlay disconnected",
  car_lock_state_change: "Car lock changed",
  credit_card_payment: "Card payment",
};
export const CAT: Record<string, { c: string; Icon: LucideIcon }> = {
  car_trip: { c: "#3d6cf7", Icon: Car }, got_into_the_car: { c: "#18b26b", Icon: LogIn }, got_out_the_car: { c: "#12a89b", Icon: LogOut },
  car_door_opened: { c: "#7a5bff", Icon: DoorOpen }, car_door_closed: { c: "#9b7bff", Icon: DoorClosed }, car_lock_state_change: { c: "#e0567f", Icon: KeyRound },
  device_connected_to_carplay: { c: "#6b5bff", Icon: Smartphone }, device_disconnected_from_carplay: { c: "#8a7cff", Icon: Smartphone },
  device_connected_to_power: { c: "#f5a524", Icon: Plug }, device_disconnected_from_power: { c: "#e0892a", Icon: Plug },
  phone_is_charging: { c: "#27ae60", Icon: BatteryCharging },
  credit_card_payment: { c: "#14b8a6", Icon: CreditCard },
};

export const NLOG = 4;
export const LCHIP: Record<number, { bg: string; fg: string }> = {
  1: { bg: "#e7eeff", fg: "#2f58d8" }, 2: { bg: "#e6f7ef", fg: "#178a55" },
  3: { bg: "#efeaff", fg: "#6a4cd0" }, 4: { bg: "#eef0f4", fg: "#7a8294" },
};

export const catOf = (name: string) => CAT[name] || { c: "#9298a6", Icon: Circle };
const pad = (n: number) => String(n).padStart(2, "0");
export const fmtTime = (d: Date) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
export const fmtTimeSec = (d: Date) => `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
const titleize = (s: string) => s.replace(/_/g, " ");
export const labelOf = (e: AwareEvent) =>
  e.event_class === "derived" ? VERBS[e.name] || titleize(e.name) : RAW_LABEL[e.name] || titleize(e.name);
export const typeLabel = (n: string) => VERBS[n] || RAW_LABEL[n] || titleize(n);
export const dayKey = (d: Date) => d.toISOString().slice(0, 10);

// --- presentation config (dashboard-owned) --------------------------------------
// Which derived events render as a time *span* (a duration capsule on the day timeline).
// The backend emits the `interval` capability as data; whether to *draw* an event as a
// span is a view decision, so it lives here, not in the event definition. Both events that
// carry an interval today (a trip, a charge) read naturally as durations, so both render as
// capsules whose length is proportional to how long they lasted.
export const SPAN_EVENTS = new Set<string>(["car_trip", "phone_is_charging"]);
export const intervalOf = (e: AwareEvent) => e.message.interval ?? null;
export const isSpan = (e: AwareEvent) => SPAN_EVENTS.has(e.name) && !!e.message.interval;
/** A span's start on the clock (its capsule top); a point event has no extent, so its
 *  timestamp is both. Used to order and place events by *when they began*. */
export const startOf = (e: AwareEvent) => (isSpan(e) ? intervalOf(e)!.started_at : e.epoch);

export function humanDur(sec: number): string {
  sec = Math.round(sec);
  if (sec < 60) return sec + "s";
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return s ? `${m}m ${s}s` : `${m} min`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

// shared per-day time scale: same timestamp -> same y in both timelines
const ROW = 92, GAP_MAX = 150, PXMIN = 1.2;
export interface Scale { y: Record<number, number>; h: number }
export function buildScale(epochs: number[]): Scale {
  const E = [...new Set(epochs)].sort((a, b) => a - b);
  const y: Record<number, number> = {};
  let cur = 0;
  E.forEach((t, i) => {
    if (i > 0) { const gapMin = (t - E[i - 1]) / 60; cur += Math.min(GAP_MAX, Math.max(ROW, gapMin * PXMIN)); }
    y[t] = cur;
  });
  return { y, h: cur + ROW };
}
export { ROW };

/** Time-proportional day layout with duration capsules, collapsed quiet gaps, and
 *  semantic-zoom reveal.
 *
 *  Vertical position maps to *time of day* — the fix for the old equal-spacing spine that
 *  didn't read as "a day". Events are ordered and placed by when they *began* (a span's
 *  start; a point event's timestamp). A duration event (a trip, a charge) renders as a
 *  capsule whose height is proportional to how long it lasted, on a shared px-per-minute
 *  scale — so a long activity is visibly longer than a short one, and its length reads
 *  against the day. Each event reserves vertical room (its capsule height, or a card row for
 *  a point event) so nothing overlaps; the step to the next event adds proportional extra for
 *  the elapsed time, and a genuinely quiet stretch (gap over QUIET_GAP_MIN) collapses to a
 *  short labeled divider instead of a big blank (the "broken scale" pattern). When an event
 *  starts while an earlier one is still running, the two *interlock* — the later capsule tucks
 *  into the tail of the earlier and is flagged "overlapping" (the Structured convention),
 *  rather than being pushed below it after a false gap. Idle time is measured from the latest
 *  end still open, so an ongoing span is never mislabeled "quiet". Hidden events (faded by
 *  altitude) are interpolated onto the same scale. Positions are keyed per event id. */
const VIS_EPS = 0.06;
const PPM = 2.5;             // px per minute — shared by capsule heights and gap proportionality
const CAP_MIN = 50;         // shortest a duration capsule can be (its icon must fit)
const CAP_MAX = 420;        // …and tallest, so a multi-hour span doesn't dominate the column
const SLOPE = 1.4;          // px per minute of *extra* spacing between two events…
const EXTRA_MAX = 64;       // …capped, so a busy afternoon doesn't run off-screen
const QUIET_GAP_MIN = 50;   // a gap wider than this collapses to a divider instead of stretching
const GAP_H = 52;           // height of a collapsed-gap divider
const NOTCH = 18;           // px two overlapping capsules interlock by
/** Height of a span's capsule: proportional to its duration, clamped for legibility. */
export const spanHeight = (e: AwareEvent): number => {
  const iv = intervalOf(e); if (!iv) return CAP_MIN;
  return Math.max(CAP_MIN, Math.min(CAP_MAX, (iv.duration_seconds / 60) * PPM));
};
/** When an event ends on the clock: a span's end; a point event's instant. */
const endOf = (e: AwareEvent) => (isSpan(e) ? intervalOf(e)!.ended_at : e.epoch);
export interface DayLayout {
  pos: Map<string, number>;                                // event id → top y (capsule/card top)
  spans: Map<string, { top: number; height: number }>;     // span id → proportional capsule box
  gaps: { y: number; seconds: number }[];                  // collapsed quiet gaps (for dividers)
  overlaps: Set<string>;                                   // ids that start while an earlier event runs
  h: number;
}
export function dayScale(events: AwareEvent[], reveal: (e: AwareEvent) => number): DayLayout {
  const sorted = [...events].sort((a, b) => startOf(a) - startOf(b));
  const pos = new Map<string, number>();
  const spans = new Map<string, { top: number; height: number }>();
  const gaps: { y: number; seconds: number }[] = [];
  const overlaps = new Set<string>();
  if (!sorted.length) return { pos, spans, gaps, overlaps, h: 40 };

  const vis = (e: AwareEvent) => reveal(e) > VIS_EPS;
  const foot = (e: AwareEvent) => (isSpan(e) ? spanHeight(e) : ROW);   // vertical room an event needs
  const anchors = sorted.filter(vis);
  if (!anchors.length) {                                    // nothing visible — hold places as slivers
    let y = 0; for (const e of sorted) { pos.set(e.id, y); y += 16; }
    return { pos, spans, gaps, overlaps, h: y + ROW };
  }

  // Walk the visible events in start order. Each reserves its footprint; the step to the next
  // is: interlock (overlap) if it starts before everything so far has ended; otherwise the
  // idle time since the last end, collapsed to a divider when quiet.
  const yAt = new Map<number, number>();                    // anchor start-time → y (for interpolation)
  const place = (e: AwareEvent, y: number) => {
    pos.set(e.id, y);
    yAt.set(startOf(e), y);
    if (isSpan(e)) spans.set(e.id, { top: y, height: spanHeight(e) });
  };
  let y = 0;
  place(anchors[0], 0);
  let openEnd = endOf(anchors[0]);                          // latest end among everything placed so far
  for (let i = 1; i < anchors.length; i++) {
    const prev = anchors[i - 1], cur = anchors[i];
    if (startOf(cur) < openEnd - 30) {                      // still-open event → interlock, flag overlap
      y += Math.max(NOTCH, foot(prev) - NOTCH);
      overlaps.add(cur.id);
    } else {
      const idleMin = Math.max(0, (startOf(cur) - openEnd) / 60);
      if (idleMin > QUIET_GAP_MIN) {
        gaps.push({ y: y + foot(prev) + GAP_H / 2, seconds: startOf(cur) - openEnd });
        y += foot(prev) + GAP_H;
      } else {
        y += foot(prev) + Math.min(EXTRA_MAX, idleMin * SLOPE);
      }
    }
    place(cur, y);
    openEnd = Math.max(openEnd, endOf(cur));
  }
  const lastY = y + foot(anchors[anchors.length - 1]);

  // Hidden events (faded by altitude) interpolate onto the same scale so they sit at their
  // true time and grow into place when you descend, without opening dead space.
  const A = anchors.map(startOf);
  const yOf = (t: number): number => {
    if (t <= A[0]) return pos.get(anchors[0].id)!;
    if (t >= A[A.length - 1]) return pos.get(anchors[anchors.length - 1].id)!;
    let i = 1; while (i < A.length && A[i] < t) i++;
    const t0 = A[i - 1], t1 = A[i], y0 = yAt.get(t0)!, y1 = yAt.get(t1)!;
    return t1 === t0 ? y0 : y0 + (y1 - y0) * ((t - t0) / (t1 - t0));
  };
  for (const e of sorted) if (!pos.has(e.id)) pos.set(e.id, yOf(startOf(e)));

  return { pos, spans, gaps, overlaps, h: lastY };
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

export interface GroupDef { key: string; label: string; Icon: LucideIcon; color: string; test: (n: string) => boolean }
export const GROUP_DEFS: GroupDef[] = [
  { key: "trip", label: "Car & trip", Icon: Car, color: "#3d6cf7", test: (n) => n === "car_trip" || n.includes("got_in") || n.includes("got_out") || n.includes("trip") },
  { key: "door", label: "Doors", Icon: DoorClosed, color: "#7a5bff", test: (n) => n.includes("door") },
  { key: "lock", label: "Lock", Icon: KeyRound, color: "#e0567f", test: (n) => n.includes("lock") },
  { key: "carplay", label: "CarPlay", Icon: Smartphone, color: "#6b5bff", test: (n) => n.includes("carplay") },
  { key: "power", label: "Power", Icon: Plug, color: "#f5a524", test: (n) => n.includes("power") || n.includes("charg") },
  { key: "spend", label: "Spending", Icon: CreditCard, color: "#14b8a6", test: (n) => n.includes("payment") || n.includes("card") },
  { key: "other", label: "Other", Icon: Circle, color: "#9298a6", test: () => true },
];
export const groupKey = (name: string) => (GROUP_DEFS.find((g) => g.test(name)) || GROUP_DEFS[GROUP_DEFS.length - 1]).key;

export interface Prepared {
  all: AwareEvent[];
  byId: Record<string, AwareEvent>;
  raw: AwareEvent[];
  derived: AwareEvent[];
  days: string[];
  derivLevel: (e: AwareEvent | undefined) => number;
}

/** Decorate API events with epoch/date and expose a memoized derivation-level function
 *  over the lineage graph. (car_trip is now a real derived event carrying its own
 *  interval — no client-side synthesis; spans render from message.interval, see isSpan.) */
export function prepare(events: AwareEvent[]): Prepared {
  const evs = events.map((e) => ({ ...e, epoch: +e.occurred_epoch, date: new Date(+e.occurred_epoch * 1000) }));
  evs.sort((a, b) => a.epoch - b.epoch);
  const byId: Record<string, AwareEvent> = Object.fromEntries(evs.map((e) => [e.id, e]));
  const raw = evs.filter((e) => e.event_class === "raw");
  const derived = evs.filter((e) => e.event_class === "derived");
  const all = evs;

  const memo: Record<string, number> = {};
  const derivLevel = (e: AwareEvent | undefined): number => {
    if (!e) return 1;
    if (memo[e.id] != null) return memo[e.id];
    memo[e.id] = 1; // guard against cycles
    const ps = (e.message.derived_from || []).map((p) => byId[p.id]).filter(Boolean);
    memo[e.id] = ps.length ? 1 + Math.max(...ps.map(derivLevel)) : 1;
    return memo[e.id];
  };

  // Cap the day selector to the most recent week so it never grows unbounded.
  const days = [...new Set(all.map((e) => dayKey(e.date)))].sort().slice(-7);
  return { all, byId, raw, derived, days, derivLevel };
}
