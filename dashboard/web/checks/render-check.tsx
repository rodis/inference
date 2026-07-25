/* Render + model checks for the day timeline and the level ladder. `npm run check`, and CI.
 *
 * Not a unit-test suite — there's no test runner in this package. It's an SSR pass that drives
 * the components through a *real* `prepare()` lineage graph and a real `dayLayout()`, so the
 * things that are easy to break silently are exercised rather than eyeballed:
 *
 *   - the lane a type lands in is computed from the shape of the lineage graph, so a change to
 *     derivLevel / laneCount / defaultLevelOf quietly re-points every default;
 *   - the two-lane layout's invariant is geometric (a moment must render inside the activity
 *     that contains it), which no type signature can enforce.
 *
 * The fixture mirrors shapes that actually occur in the feed, including the awkward ones: a
 * 6-hour charge spanning a 15-minute trip (concurrent activities), a payment inside both (the
 * innermost must win), and a 32-second "charging session" that must NOT draw as a duration.
 *
 * Adding a case is one `check(...)` line. Throws at the end if anything failed, which is what
 * makes `npm run check` exit non-zero for CI.
 */
import { renderToString } from "react-dom/server";
import { AwareContext } from "../src/app/useAware";
import type { AwareCtx } from "../src/app/useAware";
import DayTimeline from "../src/components/DayTimeline";
import LevelsDashboard from "../src/dashboards/levels/LevelsDashboard";
import TimelineDashboard from "../src/dashboards/timeline/TimelineDashboard";
import { catOf, dayLayout, defaultLevelOf, hostOf, isSpan, laneCount, laneNames, prepare } from "../src/view";
import type { AwareEvent } from "../src/types";

// Both dashboards use useLayoutEffect (scroll anchoring, focus-after-move) — correct on the
// client, inert on the server. Drop that one known-benign warning so CI output stays clean
// and a *real* React error still shows.
const realError = console.error;
console.error = (...args: unknown[]) => {
  if (typeof args[0] === "string" && args[0].includes("useLayoutEffect does nothing on the server")) return;
  realError(...args);
};

const DAY = 1784937600;                                   // 2026-07-25T00:00:00Z
const at = (hhmm: string) => {
  const [h, m] = hhmm.split(":").map(Number);
  return DAY + h * 3600 + m * 60;
};
let n = 0;
const ev = (
  name: string, cls: "raw" | "derived", when: string,
  opts: { parents?: string[]; span?: [string, string]; amount?: number } = {},
) => {
  const id = `e${++n}`;
  const iv = opts.span
    ? { started_at: at(opts.span[0]), ended_at: at(opts.span[1]), duration_seconds: at(opts.span[1]) - at(opts.span[0]) }
    : undefined;
  return {
    id, name, event_class: cls, occurred_epoch: at(when),
    message: {
      name,
      derived_from: (opts.parents ?? []).map((p) => ({ id: p, name: "" })),
      ...(iv ? { interval: iv } : {}),
      ...(opts.amount ? { amount: opts.amount } : {}),
    },
  };
};

const rows = [
  ev("device_connected_to_power", "raw", "05:20"),                              // e1
  ev("car_lock_state_change", "raw", "07:52"),                                  // e2
  ev("device_connected_to_carplay", "raw", "07:53"),                            // e3
  ev("got_into_the_car", "derived", "07:53", { parents: ["e2", "e3"] }),         // e4  D2
  ev("credit_card_payment", "raw", "08:00", { amount: 6.2 }),                   // e5  inside trip AND charge
  ev("device_disconnected_from_carplay", "raw", "08:10"),                        // e6
  ev("got_out_the_car", "derived", "08:11", { parents: ["e6"] }),                // e7  D2
  ev("car_trip", "derived", "08:11", { parents: ["e4", "e7"], span: ["07:52", "08:11"] }),   // e8  D3, 19 min
  ev("device_disconnected_from_power", "raw", "11:22"),                          // e9
  ev("phone_is_charging", "derived", "11:22", { parents: ["e1", "e9"], span: ["05:20", "11:22"] }), // e10 D2, 6h
  ev("phone_is_charging", "derived", "14:16", { parents: ["e1", "e9"], span: ["14:15", "14:16"] }), // e11 60s — junk
  ev("credit_card_payment", "raw", "19:00", { amount: 62.4 }),                   // e12 orphan
];
const prepared = prepare(rows as unknown as AwareEvent[]);
// Always go through prepare() — it decorates each row with `epoch` and `date`, which every
// layout function reads. Using the raw rows silently gives `undefined` epochs, which makes
// every containment test vacuously true rather than throwing.
const byId = prepared.byId;
const all = prepared.all;
const E = (id: string) => byId[id];
const lanes = laneCount(prepared.maxDepth);
const override: Record<string, number> = { credit_card_payment: 1 };
const hidden = new Set(["location_ping"]);
const defaultOf = (nm: string) => {
  const d = prepared.depthOf(nm);
  return d == null ? null : defaultLevelOf(d, lanes);
};
const levelOf = (nm: string) => override[nm] ?? defaultOf(nm) ?? lanes;

const ctx = {
  users: ["rods"], userId: "rods", setUserId: () => {}, status: "", eventsCount: rows.length,
  prepared, selectedDay: "2026-07-25", setSelectedDay: () => {},
  lanes, levelOf, defaultOf, isHidden: (nm: string) => hidden.has(nm),
  overrides: 1, configured: ["credit_card_payment"],
  setLevel: () => {}, setHidden: () => {}, resetLevel: () => {}, resetAll: () => {}, saved: false,
} as unknown as AwareCtx;

const strip = (h: string) => h.replace(/<!-- -->/g, "");
const fails: string[] = [];
const check = (label: string, cond: boolean, detail = "") => {
  if (!cond) fails.push(label);
  console.log(`${cond ? "  ok" : "FAIL"}  ${label}${detail && !cond ? " — " + detail : ""}`);
};

console.log("\n— the ladder —");
check("maxDepth is 3", prepared.maxDepth === 3, `got ${prepared.maxDepth}`);
check("ladder is 3 lanes", lanes === 3, `got ${lanes}`);
check("lanes are Headlines/Activity/Signals", laneNames(3).join("/") === "Headlines/Activity/Signals", laneNames(3).join("/"));
check("car_trip (D3) defaults to lane 1", defaultOf("car_trip") === 1, `got ${defaultOf("car_trip")}`);
check("phone_is_charging (D2) defaults to lane 2", defaultOf("phone_is_charging") === 2, `got ${defaultOf("phone_is_charging")}`);
check("a raw signal (D1) defaults to lane 3", defaultOf("car_lock_state_change") === 3, `got ${defaultOf("car_lock_state_change")}`);
check("credit_card_payment defaults to 3 and the override lifts it to 1",
  defaultOf("credit_card_payment") === 3 && levelOf("credit_card_payment") === 1,
  `default ${defaultOf("credit_card_payment")} / level ${levelOf("credit_card_payment")}`);
check("a deeper ladder re-points D3 down a lane", defaultLevelOf(3, 4) === 2, `got ${defaultLevelOf(3, 4)}`);
check("an unseen type has no default", defaultOf("never_fired") === null);
check("types are deepest-first", prepared.types[0] === "car_trip", prepared.types[0]);

console.log("\n— lanes: what draws as a duration —");
check("a 19-minute trip is a span", isSpan(E("e8")));
check("a 6-hour charge is a span", isSpan(E("e10")));
check("a 60-second charge is NOT a span", !isSpan(E("e11")));
check("a payment is never a span", !isSpan(E("e5")));

console.log("\n— containment —");
const spansAll = all.filter(isSpan);
check("the innermost host wins (trip, not the 6h charge)",
  hostOf(E("e5"), spansAll)?.id === "e8", hostOf(E("e5"), spansAll)?.name);
check("a moment outside every span has no host", hostOf(E("e12"), spansAll) === null);

console.log("\n— layout geometry —");
const L = dayLayout(all, () => 1, (nm) => catOf(nm).c);
const trip = L.spans.get("e8")!, charge = L.spans.get("e10")!;
check("both concurrent activities got a box", !!trip && !!charge);
check("two sub-columns are needed", L.cols === 2, `got ${L.cols}`);
check("they sit in different sub-columns", trip.col !== charge.col, `trip ${trip.col} / charge ${charge.col}`);
check("the trip nests inside the charge vertically",
  trip.top >= charge.top && trip.top + trip.height <= charge.top + charge.height,
  `trip ${trip.top}..${trip.top + trip.height} vs charge ${charge.top}..${charge.top + charge.height}`);
check("the 6h charge is taller than the 19min trip", charge.height > trip.height, `${charge.height} vs ${trip.height}`);
const payY = L.pos.get("e5")!;
check("the payment renders inside its host's vertical range",
  payY >= trip.top && payY <= trip.top + trip.height, `${payY} not in ${trip.top}..${trip.top + trip.height}`);
check("the payment's band is its innermost host", L.hosts.get("e5") === "e8", L.hosts.get("e5"));
// Both activities host something at full detail: the power events fall inside the charge, the
// carplay/lock signals inside the trip. Bands go longest-first so the innermost paints on top.
check("a band per hosting activity", L.bands.length === 2, `${L.bands.length} bands`);
check("bands are ordered longest host first", L.bands[0].hostId === "e10" && L.bands[1].hostId === "e8",
  L.bands.map((b) => b.hostId).join(","));
check("the junk charge is placed as a moment, not a span", !L.spans.has("e11"));
check("a quiet stretch collapsed to a divider", L.gaps.length > 0, `${L.gaps.length} gaps`);
check("time order is preserved down the page", L.pos.get("e5")! < L.pos.get("e12")!);

console.log("\n— day timeline renders —");
const dt = strip(renderToString(
  <AwareContext.Provider value={ctx}>
    <DayTimeline events={all} layout={L} levelOf={levelOf} defaultOf={defaultOf}
      derivLevel={prepared.derivLevel} onSelect={() => {}} revealOf={() => 1} byId={byId} />
  </AwareContext.Provider>));
check("two activity capsules drawn", (dt.match(/class="capsule"/g) || []).length === 2,
  `${(dt.match(/class="capsule"/g) || []).length}`);
check("moments drawn on the right rail", (dt.match(/class="dt-mom"/g) || []).length >= 5,
  `${(dt.match(/class="dt-mom"/g) || []).length}`);
check("a containment band is drawn", dt.includes("dt-band"));
check("the moments rail is drawn", dt.includes("dt-rail"));
check("the lane divider is drawn", dt.includes("dt-rule"));
check("the trip shows its duration", dt.includes("19 min"));
check("a payment shows its amount", dt.includes("CHF 6.20"));
// "no host" appears exactly on the moments the layout found no container for — here the
// orphan payment and the junk charge that fell out of the activity lane. A hosted moment says
// nothing, because its band already does.
const orphans = all.filter((e) => !isSpan(e) && !L.hosts.has(e.id));
check("the orphan payment is flagged", dt.includes("no host"));
check("exactly the host-less moments are flagged",
  (dt.match(/no host/g) || []).length === orphans.length,
  `${(dt.match(/no host/g) || []).length} flags vs ${orphans.length} host-less`);
check("the hosted payment is not among them", L.hosts.has("e5"));

console.log("\n— levels board —");
const html = strip(renderToString(<AwareContext.Provider value={ctx}><LevelsDashboard /></AwareContext.Provider>));
check("one rail per lane", (html.match(/class="lane-rail"/g) || []).length === 3,
  `${(html.match(/class="lane-rail"/g) || []).length} rails`);
check("lanes are colour-coded", html.includes('class="lane l1"'));
check("top and bottom lanes are named", html.includes("Headlines") && html.includes("Signals"));
check("a promoted type carries an up-flag", /ovrflag up[\s\S]{0,80}↑ L1/.test(html));
check("a ghost marks the lane an override left", html.includes("· default"));
check("the tray exists", html.includes("Off the timeline"));
check("keyboard equivalents are offered", html.includes("promote one lane"));
check("the preview panel reports the altitude", /In view at L1/.test(html));

console.log("\n— timeline dashboard at its default altitude (L1) —");
const tl = strip(renderToString(<AwareContext.Provider value={ctx}><TimelineDashboard /></AwareContext.Provider>));
check("the Assign & lift sidebar is gone", !tl.includes("Assign"));
check("the two-lane timeline renders", tl.includes("dt-rail"));
check("the zoom control names the current lane", tl.includes("headlines"));
check("the trip capsule is visible at L1", tl.includes("class=\"capsule\""));
check("the day's spend is summed", tl.includes("68.60"));

if (fails.length) throw new Error(`${fails.length} check(s) failed: ${fails.join("; ")}`);
console.log("\nall checks passed\n");
