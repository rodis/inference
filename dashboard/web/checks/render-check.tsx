/* Render + model checks for the level ladder. Run with `npm run check` (and in CI).
 *
 * Not a unit-test suite — there's no test runner in this package. It's a single SSR pass that
 * drives the two dashboards that depend on the ladder through a *real* `prepare()` lineage
 * graph, so the depth→lane defaults, the override flags and the hidden-type handling are
 * exercised rather than eyeballed. It exists because this logic is easy to break silently:
 * the lane a type lands in is computed from the shape of the lineage graph, so a change to
 * `derivLevel`, `laneCount` or `defaultLevelOf` quietly re-points every default.
 *
 * Adding a case is one `check(...)` line. Throws at the end if anything failed, which is
 * what makes `npm run check` exit non-zero for CI.
 */
import { renderToString } from "react-dom/server";
import { AwareContext } from "../src/app/useAware";
import type { AwareCtx } from "../src/app/useAware";
import LevelsDashboard from "../src/dashboards/levels/LevelsDashboard";
import TimelineDashboard from "../src/dashboards/timeline/TimelineDashboard";
import { defaultLevelOf, laneCount, laneNames, prepare } from "../src/view";
import type { AwareEvent } from "../src/types";

// Both dashboards use useLayoutEffect (scroll anchoring, focus-after-move) — correct on the
// client, inert on the server. Drop that one known-benign warning so CI output stays clean
// and a *real* React error still shows.
const realError = console.error;
console.error = (...args: unknown[]) => {
  if (typeof args[0] === "string" && args[0].includes("useLayoutEffect does nothing on the server")) return;
  realError(...args);
};

let n = 0;
const ev = (name: string, cls: "raw" | "derived", parents: string[] = [], extra: Record<string, unknown> = {}) => ({
  id: `e${++n}`,
  name,
  event_class: cls,
  occurred_epoch: 1784966400 + n * 60,   // 2026-07-25T08:00Z, so dayKey() lands on the day we ask for
  message: { name, derived_from: parents.map((id) => ({ id, name: "" })), ...extra },
});

// The real lineage shape: raw signals → got_into/got_out (D2) → car_trip (D3).
const rows = [
  ev("device_connected_to_carplay", "raw"),                       // e1  D1
  ev("car_lock_state_change", "raw"),                             // e2  D1
  ev("got_into_the_car", "derived", ["e1", "e2"]),                // e3  D2
  ev("device_disconnected_from_carplay", "raw"),                  // e4  D1
  ev("got_out_the_car", "derived", ["e4"]),                       // e5  D2
  ev("car_trip", "derived", ["e3", "e5"], {                       // e6  D3
    interval: { started_at: 1784966460, ended_at: 1784967660, duration_seconds: 1200 },
  }),
  ev("credit_card_payment", "raw", [], { amount: 6.2 }),          // e7  D1
  ev("location_ping", "raw"),                                     // e8  D1
];

const prepared = prepare(rows as unknown as AwareEvent[]);
const lanes = laneCount(prepared.maxDepth);

// the config as migrated: one promotion, one type dropped, nothing else
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
  overrides: 2, configured: ["credit_card_payment", "location_ping"],
  setLevel: () => {}, setHidden: () => {}, resetLevel: () => {}, resetAll: () => {}, saved: false,
} as unknown as AwareCtx;

// React SSR injects an empty comment at every {interpolation}; strip them before matching text
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
check("got_into_the_car (D2) defaults to lane 2", defaultOf("got_into_the_car") === 2, `got ${defaultOf("got_into_the_car")}`);
check("a raw signal (D1) defaults to lane 3", defaultOf("device_connected_to_carplay") === 3, `got ${defaultOf("device_connected_to_carplay")}`);
check("credit_card_payment defaults to 3 and the override lifts it to 1",
  defaultOf("credit_card_payment") === 3 && levelOf("credit_card_payment") === 1,
  `default ${defaultOf("credit_card_payment")} / level ${levelOf("credit_card_payment")}`);
check("a deeper ladder re-points D3 down a lane", defaultLevelOf(3, 4) === 2, `got ${defaultLevelOf(3, 4)}`);
check("an unseen type has no default", defaultOf("never_fired") === null, `got ${defaultOf("never_fired")}`);
check("depthsOf reports every depth seen", prepared.depthsOf("car_trip").join(",") === "3", prepared.depthsOf("car_trip").join(","));
check("types are deepest-first", prepared.types[0] === "car_trip", prepared.types[0]);

console.log("\n— levels board —");
const html = strip(renderToString(<AwareContext.Provider value={ctx}><LevelsDashboard /></AwareContext.Provider>));
check("one rail per lane", (html.match(/class="lane-rail"/g) || []).length === 3,
  `${(html.match(/class="lane-rail"/g) || []).length} rails`);
check("lanes are colour-coded", html.includes('class="lane l1"'));
check("top lane is named", html.includes("Headlines"));
check("bottom lane is named", html.includes("Signals"));
check("a promoted type carries an up-flag", /ovrflag up[\s\S]{0,80}↑ L1/.test(html));
check("a ghost marks the lane an override left", html.includes("· default"));
check("a dropped type sits in the tray", html.includes("Off the timeline")
  && html.indexOf("Location ping") > html.indexOf("Off the timeline"));
check("keyboard equivalents are offered", html.includes("promote one lane"));
check("the preview panel reports the altitude", /In view at L1/.test(html));

console.log("\n— day timeline —");
const tl = strip(renderToString(<AwareContext.Provider value={ctx}><TimelineDashboard /></AwareContext.Provider>));
check("the Assign & lift sidebar is gone", !tl.includes("Assign"));
check("the timeline renders", tl.includes("vtwrap"));
check("a dropped type is absent from the DOM entirely", !tl.includes("Location ping"));
check("the zoom control names the current lane", tl.includes("headlines"));
check("a duration event renders as a capsule", tl.includes("vt-capsule"));

if (fails.length) throw new Error(`${fails.length} check(s) failed: ${fails.join("; ")}`);
console.log("\nall checks passed\n");
