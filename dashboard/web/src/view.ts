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

/** Reveal-weighted vertical layout for semantic zoom. Every event keeps a slot, but the
 *  slot height grows with the event's reveal (0..1): hidden events collapse to a sliver
 *  (holding their place), revealed events take a full row. Because nothing enters or
 *  leaves the layout as altitude changes, lower-layer events *grow* in/out smoothly
 *  instead of popping. Positions are keyed per event id (handles same-timestamp events). */
const PACK_MIN = 16;
const HIDDEN_RUN_MAX = 26;   // a *run* of consecutive hidden events collapses to at most this,
const VIS_EPS = 0.06;        // so long quiet stretches don't stack into a tall empty gap (mobile)
export function packScale(events: AwareEvent[], reveal: (e: AwareEvent) => number): { pos: Map<string, number>; h: number } {
  const sorted = [...events].sort((a, b) => a.epoch - b.epoch);
  const pos = new Map<string, number>();
  const ys: number[] = [];
  let y = 0, first = true, runH = 0;
  for (const e of sorted) {
    const r = Math.max(0, Math.min(1, reveal(e)));
    if (!first) {
      if (r > VIS_EPS) {                                       // (partly) visible: full row, grows with reveal
        y += PACK_MIN + (ROW - PACK_MIN) * r;
        runH = 0;
      } else {                                                 // hidden: add to the run, capped so N slivers ≈ one
        const add = Math.min(PACK_MIN, Math.max(0, HIDDEN_RUN_MAX - runH));
        y += add;
        runH += add;
      }
    }
    pos.set(e.id, y);
    ys.push(y);
    first = false;
  }
  // Trim collapsed head/tail: the timeline should start at the first visible event and end
  // at the last, so leading/trailing hidden slivers don't add whitespace. Interior slivers
  // stay (they hint at detail between visible events).
  let lo = 0; while (lo < sorted.length && reveal(sorted[lo]) < 0.02) lo++;
  let hi = sorted.length - 1; while (hi >= 0 && reveal(sorted[hi]) < 0.02) hi--;
  if (lo > hi) return { pos, h: y + ROW }; // nothing clearly visible — leave as-is
  const offset = ys[lo];
  for (const [k, v] of pos) pos.set(k, Math.max(0, v - offset));
  return { pos, h: ys[hi] - offset + ROW };
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

/** Decorate API events with epoch/date, synthesize trips when none are real, and
 *  expose a memoized derivation-level function over the lineage graph. */
export function prepare(events: AwareEvent[]): Prepared {
  const evs = events.map((e) => ({ ...e, epoch: +e.occurred_epoch, date: new Date(+e.occurred_epoch * 1000) }));
  evs.sort((a, b) => a.epoch - b.epoch);
  const byId: Record<string, AwareEvent> = Object.fromEntries(evs.map((e) => [e.id, e]));
  const raw = evs.filter((e) => e.event_class === "raw");
  const derived = evs.filter((e) => e.event_class === "derived");

  // car_trip is a real derived event (session_window). Only synthesize start/end
  // pairs when none have been produced, so we never double-count a trip.
  const hasRealTrips = evs.some((e) => e.name === "car_trip");
  const synthTrips: AwareEvent[] = [];
  if (!hasRealTrips) {
    evs.filter((e) => e.name === "got_into_the_car").forEach((start, i) => {
      const end = evs.find((e) => e.name === "got_out_the_car" && e.epoch >= start.epoch);
      if (!end) return;
      synthTrips.push({
        id: "trip-" + i, name: "car_trip", event_class: "derived", synthetic: true,
        occurred_epoch: start.epoch, epoch: start.epoch, date: start.date,
        endEpoch: end.epoch, durationSec: end.epoch - start.epoch,
        message: {
          id: "trip-" + i, name: "car_trip", inference_type: "planned rollup · synthesized in view",
          confidence_score: null, derived_from: [{ id: start.id }, { id: end.id }],
        },
      });
    });
  }
  synthTrips.forEach((ev) => { byId[ev.id] = ev; });
  const all = [...evs, ...synthTrips];

  const memo: Record<string, number> = {};
  const derivLevel = (e: AwareEvent | undefined): number => {
    if (!e) return 1;
    if (memo[e.id] != null) return memo[e.id];
    memo[e.id] = 1; // guard against cycles
    const ps = (e.message.derived_from || []).map((p) => byId[p.id]).filter(Boolean);
    memo[e.id] = ps.length ? 1 + Math.max(...ps.map(derivLevel)) : 1;
    return memo[e.id];
  };

  const days = [...new Set(all.map((e) => dayKey(e.date)))].sort();
  return { all, byId, raw, derived, days, derivLevel };
}
