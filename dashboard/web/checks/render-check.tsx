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
 * 6-hour span concurrent with a 15-minute trip (originally a phone charge — retired with the
 * charger signals, #39 — now a stay at the same instants, because the layout must keep handling
 * concurrency whatever type expresses it), a payment inside both (the innermost must win), a
 * 60-second phantom car_trip whose capsule has to be floored to stay legible, and a café stay
 * whose end lands a minute *after* the drive home began (a handoff, not concurrency).
 *
 * Adding a case is one `check(...)` line. Throws at the end if anything failed, which is what
 * makes `npm run check` exit non-zero for CI.
 */
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import Shell from "../src/app/Shell";
import HomeView from "../src/dashboards/home/HomeView";
import { AwareContext } from "../src/app/useAware";
import type { AwareCtx } from "../src/app/useAware";
import DayTimeline from "../src/components/DayTimeline";
import EventBody from "../src/components/EventBody";
import EventModal from "../src/components/EventModal";
import LevelsDashboard from "../src/dashboards/levels/LevelsDashboard";
import ProcessesDashboard from "../src/dashboards/processes/ProcessesDashboard";
import TasksDashboard from "../src/dashboards/tasks/TasksDashboard";
import { ageLabel, gmailLink, groupTasks, subjectOf } from "../src/dashboards/tasks/task";
import type { Task } from "../src/dashboards/tasks/task";
import { chipsOf, cronText, statusOf } from "../src/dashboards/processes/process";
import type { Cycle, ProcessDef } from "../src/dashboards/processes/process";
import processGraph from "../../processes.json";
import TimelineDashboard from "../src/dashboards/timeline/TimelineDashboard";
import { carCorroborated, catOf, dayLayout, processOf, defaultLevelOf, hostOf, inkOn, isEverydayPlace, isSpan, labelOf, laneCount, laneNames, laneOf, placeUnknown, prepare, routeOf, supersededIds, typeLabel } from "../src/view";
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
  opts: { parents?: string[]; span?: [string, string]; amount?: number;
          place?: { label: string | null; everyday?: boolean } } = {},
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
      ...(opts.place ? { place: { lat: 47.2, lon: 8.57, spread_m: 12, ...opts.place } } : {}),
    },
  };
};

const rows = [
  ev("location_ping", "raw", "05:20"),                                          // e1
  ev("car_lock_state_change", "raw", "07:52"),                                  // e2
  ev("device_connected_to_carplay", "raw", "07:53"),                            // e3
  ev("got_into_the_car", "derived", "07:53", { parents: ["e2", "e3"] }),         // e4  D2
  ev("credit_card_payment", "raw", "08:00", { amount: 6.2 }),                   // e5  inside trip AND the 6h stay
  ev("device_disconnected_from_carplay", "raw", "08:10"),                        // e6
  ev("got_out_the_car", "derived", "08:11", { parents: ["e6"] }),                // e7  D2
  ev("car_trip", "derived", "08:11", { parents: ["e4", "e7"], span: ["07:52", "08:11"] }),   // e8  D3, 19 min
  ev("location_ping", "raw", "11:22"),                                           // e9
  ev("stay", "derived", "11:22", { parents: ["e1", "e9"], span: ["05:20", "11:22"],
                                   place: { label: "Grandma's" } }),             // e10 D2, 6h
  ev("car_trip", "derived", "14:16", { parents: ["e4", "e7"], span: ["14:15", "14:16"] }), // e11 60s — junk
  ev("credit_card_payment", "raw", "19:00", { amount: 62.4 }),                   // e12 orphan
  ev("location_ping", "raw", "09:00"),                                           // e13
  ev("stay", "derived", "10:36", { parents: ["e13"], span: ["09:00", "10:36"],
                                   place: { label: "Konditorei von Rotz Baar" } }), // e14 96min, named
  ev("stay", "derived", "15:00", { parents: ["e13"], span: ["14:20", "15:00"],
                                   place: { label: null } }),                    // e15 40min, unknown place
  // The real 25 July shape: the stay's end is the fix that broke its cluster, which arrives
  // after the drive away has started — so the two overlap by a minute without being concurrent.
  ev("stay", "derived", "13:20", { parents: ["e13"], span: ["11:43", "13:20"],
                                   place: { label: "Café Frisch" } }),           // e16 97min
  ev("car_trip", "derived", "13:33", { parents: ["e4", "e7"], span: ["13:19", "13:33"] }), // e17 14min
  // An `everyday` place — the one you live in. Derived and persisted like any other stay, but
  // kept off the day (isEverydayPlace): home dwell has no natural boundaries, so what surfaces
  // is one arbitrary fragment per sampling gap rather than a visit.
  ev("stay", "derived", "22:40", { parents: ["e13"], span: ["20:10", "22:40"],
                                   place: { label: "Home", everyday: true } }),   // e18 150min
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
check("a raw signal (D1) defaults to lane 3", defaultOf("car_lock_state_change") === 3, `got ${defaultOf("car_lock_state_change")}`);
check("credit_card_payment defaults to 3 and the override lifts it to 1",
  defaultOf("credit_card_payment") === 3 && levelOf("credit_card_payment") === 1,
  `default ${defaultOf("credit_card_payment")} / level ${levelOf("credit_card_payment")}`);
check("a deeper ladder re-points D3 down a lane", defaultLevelOf(3, 4) === 2, `got ${defaultLevelOf(3, 4)}`);
check("an unseen type has no default", defaultOf("never_fired") === null);
check("types are deepest-first", prepared.types[0] === "car_trip", prepared.types[0]);

console.log("\n— lanes: what draws as a duration —");
check("a 19-minute trip is a span", isSpan(E("e8")));
check("a 6-hour stay is a span", isSpan(E("e10")));
// Lane is about KIND, not data quality: a 60-second car_trip is a phantom, but it is still a
// trip. Filing short intervals as moments put car_trip in both lanes on the
// same day, which read as a broken categorisation rather than the bad inference it is.
check("a 60-second phantom car_trip is still a span", isSpan(E("e11")));
check("a payment is never a span", !isSpan(E("e5")));
check("a stay is a span", isSpan(E("e14")));
// The label IS the place when one matched — the whole point of naming places, and the reason a
// stay reads as "Konditorei von Rotz Baar" rather than the type's verb.
check("a named stay is labelled with its place", labelOf(E("e14")) === "Konditorei von Rotz Baar",
      labelOf(E("e14")));
check("an unnamed stay falls back to its verb", labelOf(E("e15")) === "Stay", labelOf(E("e15")));
// …and that fallback is what `placeUnknown` reports, so the drawing and the label can't disagree.
// Keyed on the label being absent, NOT on the place capability being absent: a trip carries no
// place at all and is not "unnamed", it simply isn't a place-shaped event.
check("placeUnknown is about the label, not the capability",
  placeUnknown(E("e15")) && !placeUnknown(E("e14")) && !placeUnknown(E("e8")),
  `e15 ${placeUnknown(E("e15"))} / e14 ${placeUnknown(E("e14"))} / e8 ${placeUnknown(E("e8"))}`);
// Depth is not importance: a stay stands on raw pings alone, so the ladder defaults it DOWN even
// though a 96-minute named visit belongs in the headlines. That is the documented tension — the
// fix is the levels board (stored prefs), not a special case here. Asserted so the surprise is
// recorded rather than rediscovered.
check("a stay defaults to a deep lane despite being headline-worthy",
      defaultOf("stay") === 2, `got ${defaultOf("stay")}`);

console.log("\n— containment —");
const spansAll = all.filter(isSpan);
check("the innermost host wins (trip, not the 6h stay)",
  hostOf(E("e5"), spansAll)?.id === "e8", hostOf(E("e5"), spansAll)?.name);
check("a moment outside every span has no host", hostOf(E("e12"), spansAll) === null);

console.log("\n— layout geometry —");
const L = dayLayout(all, () => 1, (nm) => catOf(nm).c);
const trip = L.spans.get("e8")!, morning = L.spans.get("e10")!;
check("both concurrent activities got a box", !!trip && !!morning);
check("two sub-columns are needed", L.cols === 2, `got ${L.cols}`);
check("they sit in different sub-columns", trip.col !== morning.col, `trip ${trip.col} / stay ${morning.col}`);
check("the trip nests inside the 6h stay vertically",
  trip.top >= morning.top && trip.top + trip.height <= morning.top + morning.height,
  `trip ${trip.top}..${trip.top + trip.height} vs stay ${morning.top}..${morning.top + morning.height}`);
check("the 6h stay is taller than the 19min trip", morning.height > trip.height, `${morning.height} vs ${trip.height}`);
const payY = L.pos.get("e5")!;
check("the payment renders inside its host's vertical range",
  payY >= trip.top && payY <= trip.top + trip.height, `${payY} not in ${trip.top}..${trip.top + trip.height}`);
check("the payment's band is its innermost host", L.hosts.get("e5") === "e8", L.hosts.get("e5"));
// Both activities host something at full detail: the morning pings fall inside the 6h stay, the
// carplay/lock signals inside the trip. Bands go longest-first so the innermost paints on top.
// 3 hosts: the trip, the 6h stay, and the named café stay (which hosts the 09:00 ping).
check("a band per hosting activity", L.bands.length === 3, `${L.bands.length} bands`);
// 6h stay, then the 96-minute stay, then the 19-minute trip — i.e. the same order as their
// durations, which only holds because a span's own quiet stretch no longer collapses (see below).
check("bands are ordered longest host first",
  L.bands.map((b) => b.hostId).join(",") === "e10,e14,e8", L.bands.map((b) => b.hostId).join(","));
check("a brief span still gets a capsule box", L.spans.has("e11"));
check("a brief span's capsule is floored for legibility", L.spans.get("e11")!.height === 44,
  `${L.spans.get("e11")!.height}px`);
check("time order is preserved down the page", L.pos.get("e5")! < L.pos.get("e12")!);

// A quiet stretch collapses only where *nothing was happening*. A stay produces no location
// fixes while you sit still (ADR 0007), so a 96-minute café visit has zero interior instants and
// used to collapse its own duration into a divider labelled "1h 36m quiet" — drawn on top of its
// own capsule, and crushing the capsule so an overlapping trip couldn't be seen to overlap.
const stayBox = L.spans.get("e14")!, tripBox = L.spans.get("e8")!;
check("a quiet stretch with nothing in it still collapses to a divider", L.gaps.length > 0, `${L.gaps.length} gaps`);
check("no divider lands inside an activity's capsule",
  !L.gaps.some((g) => [...L.spans.values()].some((b) => g.y > b.top && g.y < b.top + b.height)),
  L.gaps.map((g) => g.y).join(","));
check("a 96-minute stay is taller than a 19-minute trip", stayBox.height > tripBox.height,
  `stay ${stayBox.height} vs trip ${tripBox.height}`);

// A stretch past MAX_STEP used to be *clamped*, which is not monotone: two very different lulls
// drew as identical px, so the longest activity of the day could not read as the longest.
// `stepFor` log-compresses instead, so every extra minute is worth strictly-positive px forever.
// Asserted as an ascending chain rather than against a px constant: MAX_STEP/KNEE are a tuning
// knob and have already moved once (210/150 -> 48/24, when the duration bar took over the job of
// stating duration), while the *ordering* is the property that must never break.
// Measured on a lone span, which has no interior instants and so renders exactly one step.
const soloHeight = (from: string, to: string) => {
  const one = prepare([ev("stay", "derived", to, { span: [from, to], place: { label: "solo" } })] as unknown as AwareEvent[]);
  return dayLayout(one.all, () => 1, (nm) => catOf(nm).c).spans.get(one.all[0].id)!.height;
};
const [h109, h240, h480] = [soloHeight("00:00", "01:49"), soloHeight("00:00", "04:00"), soloHeight("00:00", "08:00")];
const chain = `${Math.round(h109)}/${Math.round(h240)}/${Math.round(h480)}px`;
check("longer always draws taller — the scale never clips flat", h109 < h240 && h240 < h480, chain);
// …and it's heavily compressed on purpose, because the capsule is NOT the duration channel (the
// bar is): 4.4× the minutes buys under 1.5× the px. This is what keeps a 2h39 café visit from
// turning a 7-hour day into a 1185px page, which is exactly what 210/150 did.
check("…but 4.4× the minutes buys well under 1.5× the px", h480 < h109 * 1.5,
  `${chain} — ratio ${(h480 / h109).toFixed(2)}`);

// An activity is charged for its dwell ONCE (`dwell` in dayLayout). Pricing each stretch on its own
// made a capsule's height depend on how many pieces its interior moments chopped it into — because
// `compress` is nearly flat out there, every piece paid almost the full concave price, so one real
// 2h39 café visit drew 314px with two card payments inside it and 166px without. The stay was tall
// because of where the card got tapped, which tells a reader nothing. Same span, same duration, two
// interior payments: the difference must be the rows those payments need, not a doubling.
const ROW_MIN_PX = 34;   // = MIN_STEP / ROW_MIN in view.ts: the vertical room one moment row needs
const stayHeight = (moments: string[]) => {
  const rows = [
    ev("stay", "derived", "13:28", { span: ["10:49", "13:28"], place: { label: "dwell" } }),
    ...moments.map((m) => ev("credit_card_payment", "raw", m, { amount: 5 })),
  ];
  const one = prepare(rows as unknown as AwareEvent[]);
  const L2 = dayLayout(one.all, () => 1, (nm) => catOf(nm).c);
  return L2.spans.get(one.all.find(isSpan)!.id)!.height;
};
const plain = stayHeight([]), chopped = stayHeight(["10:51", "12:40"]);
const both = `plain ${Math.round(plain)}px vs chopped ${Math.round(chopped)}px`;
check("height comes from an activity's duration, not from how its moments chop it",
  chopped - plain <= 2 * ROW_MIN_PX + 8, both);
check("…though each interior moment still earns its own row", chopped > plain, both);

// A second sub-column is earned by genuine concurrency only. A stay ends when the fix that broke
// its cluster arrives — after the drive away has begun — so the café visit and the trip home
// overlap by a minute. That's a handoff: one lane, capsules stacked, not two columns reading as
// "two things at once". (The 6h stay over the 19min trip above is the real thing, and still fans.)
const cafe = L.spans.get("e16")!, home = L.spans.get("e17")!;
check("a stay and the drive away from it share one sub-column", cafe.col === home.col,
  `stay ${cafe.col} / trip ${home.col}`);
check("a boundary overlap doesn't widen the lane", L.cols === 2, `got ${L.cols}`);
check("the drive's capsule is butted below the stay's, not painted over it",
  home.top >= cafe.top + cafe.height, `trip top ${home.top} vs stay bottom ${cafe.top + cafe.height}`);

// The lane is a track: consecutive capsules in one column are joined across the dead time
// between them. Per column only — a line between two *concurrent* capsules would claim a
// sequence that isn't there — and never as a smudge between two capsules that already touch.
check("consecutive capsules in a column are joined", L.links.length > 0, `${L.links.length} links`);
check("every connector sits in a real sub-column",
  L.links.every((l) => l.col < L.cols), L.links.map((l) => l.col).join(","));
check("a connector spans only dead time — never over a capsule",
  !L.links.some((l) => [...L.spans.values()].some(
    (b) => b.col === l.col && l.top < b.top + b.height && l.top + l.height > b.top)),
  L.links.map((l) => `${l.col}@${l.top}+${l.height}`).join(" "));
check("no hairline connector between the stay and the drive away from it",
  !L.links.some((l) => l.col === cafe.col && l.top >= cafe.top + cafe.height && l.height < 8),
  L.links.filter((l) => l.col === cafe.col).map((l) => l.height).join(","));

console.log("\n— ink on a category fill —");
// Every category colour is currently dark enough for white ink, so inkOn is a guard rather than a
// live branch — assert the branch itself, so the palette can't gain a light colour that erases
// its own icon. (`stay` was that light colour, briefly.)
check("a light fill takes dark ink", inkOn("#f2b705") === "#221a00", inkOn("#f2b705"));
check("a dark trip keeps white ink", inkOn(catOf("car_trip").c) === "#fff", inkOn(catOf("car_trip").c));
check("the stay's brown is dark enough for white", inkOn(catOf("stay").c) === "#fff", inkOn(catOf("stay").c));

console.log("\n— day timeline renders —");
const dt = strip(renderToString(
  <AwareContext.Provider value={ctx}>
    <DayTimeline events={all} layout={L} onSelect={() => {}} revealOf={() => 1} />
  </AwareContext.Provider>));
// Eight, INCLUDING the everyday-place stay: DayTimeline draws whatever it is handed, and
// deciding what to hand it is the dashboard's job (see the TimelineDashboard checks below,
// where the same event is dropped). Keeping the component dumb here is the point — one filter
// site, not a rule duplicated in every consumer.
check("eight activity capsules drawn", (dt.match(/class="capsule"/g) || []).length === 8,
  `${(dt.match(/class="capsule"/g) || []).length}`);
// A stay at a place nothing matched is drawn hollow, not filled (placeUnknown), via the `unnamed`
// class. Counted rather than merely found, so a treatment that leaks onto the named stays — or
// onto every capsule in the lane — fails here instead of being noticed on a screenshot. The class
// is also what keeps the weakening off `.dt-act`'s own opacity, which belongs to the altitude
// reveal.
const actRows = dt.match(/class="dt-act[^"]*"/g) || [];
const weakRows = actRows.filter((c) => c.includes("unnamed")).length;
check("exactly the unnamed stay is drawn hollow", weakRows === 1, `${weakRows} of 8 rows`);
check("the other seven activities draw at full strength",
  actRows.length - weakRows === 7, `${actRows.length - weakRows}`);
// A row whose label would overflow into the next row's box (next activity starts within
// LABEL_FULL) clamps its meta to one line via `tight` — the resting half of the #63 fix, the
// hover half (un-clamp + raise) is CSS-only. Bounded on both sides: zero tight rows means the
// clamp stopped applying (the fixture's clustered shapes guarantee some), all-tight means it
// leaked onto rows with room.
const tightRows = actRows.filter((c) => c.includes("tight")).length;
check("clustered activities clamp their labels (tight)", tightRows >= 1 && tightRows < actRows.length,
  `${tightRows} of ${actRows.length} rows`);
// The category colour reaches CSS as a custom property, which is what lets the unnamed variant
// restate the fill as a dotted border + icon colour. Hard-coding `background` inline again would
// silently fill the hollow capsule back in.
check("a capsule exposes its category colour to CSS", dt.includes("--cat:"));
check("moments drawn on the right rail", (dt.match(/class="dt-mom"/g) || []).length >= 5,
  `${(dt.match(/class="dt-mom"/g) || []).length}`);
check("a containment band is drawn", dt.includes("dt-band"));
check("the moments rail is drawn", dt.includes("dt-rail"));
check("the activity lane's connectors are drawn", (dt.match(/class="dt-link"/g) || []).length === L.links.length,
  `${(dt.match(/class="dt-link"/g) || []).length} of ${L.links.length}`);
check("a connector carries its sub-column", dt.includes("--lcol"));
check("the lane divider is drawn", dt.includes("dt-rule"));
check("both lanes are named in a header", dt.includes(">Activities<") && dt.includes(">Moments<"));
check("the header shares the lanes' boundary variable", dt.includes("--capcols"));
check("the trip shows its duration", dt.includes("19 min"));

check("a payment shows its amount", dt.includes("CHF 6.20"));
// "no host" appears exactly on the moments the layout found no container for — here the
// orphan payment. A hosted moment says
// nothing, because its band already does.
const orphans = all.filter((e) => !isSpan(e) && !L.hosts.has(e.id));
check("the orphan payment is flagged", dt.includes("no host"));
check("exactly the host-less moments are flagged",
  (dt.match(/no host/g) || []).length === orphans.length,
  `${(dt.match(/no host/g) || []).length} flags vs ${orphans.length} host-less`);
check("the hosted payment is not among them", L.hosts.has("e5"));
// The classification grammar (L lane, ↑/↓ override, D depth, "N below") is deliberately absent
// from the day: it's taxonomy about the event, not what happened, and it lives in the modal.
check("no L chips on the day's cards", !dt.includes('class="lchip'));
check("no D badges on the day's cards", !dt.includes("dbadge"));
check("no override flags on the day's cards", !dt.includes("ovrflag"));
check("no rollup counter on the day's cards", !dt.includes("below"));
// ...but the payment's "no host" flag stays: that's about the day's shape, not the ladder.
check("the host flag survives the chip cleanup", dt.includes("no host"));

console.log("\n— event modal carries the classification instead —");
const modalOf = (id: string, reveal: number) => strip(renderToString(
  <EventModal event={E(id)} byId={byId} levelOf={levelOf} derivLevel={prepared.derivLevel}
    defaultOf={defaultOf} revealOf={() => reveal} onClose={() => {}} />));
const mTrip = modalOf("e8", 0);
check("the modal shows the L chip", mTrip.includes('class="lchip'));
check("the modal shows the D badge", mTrip.includes("dbadge"));
check("the modal counts contributors collapsed below the altitude", /↓ 2 below/.test(mTrip));
check("nothing is 'below' when the lineage is fully revealed", !modalOf("e8", 1).includes("below"));
check("the modal flags a lifted type", /ovrflag up[\s\S]{0,80}↑ L1/.test(modalOf("e5", 1)));

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
// A stay at an everyday place is off the day entirely — a hard drop, not a faded card, so it
// isn't in the layout or the tab order at any altitude. The event itself still exists (it is
// in `all`, and in Neon); only the timeline declines to draw it.
check("a stay at an everyday place is dropped from the day", !tl.includes(">Home<"),
  "Home stay rendered");
check("...while a stay at a real destination still draws", tl.includes("Café Frisch"));
check("the everyday stay is still in the loaded event set", all.some((e) => e.id === "e18"));
check("isEverydayPlace keys on the flag, not the label",
  isEverydayPlace(E("e18")) && !isEverydayPlace(E("e16")) && !isEverydayPlace(E("e15")));

console.log("\n— the brand mark —");
// The Shell hosts the theme toggle, which reads `localStorage` while rendering — correct in the
// browser (the app never server-renders), missing under this SSR harness. A three-line in-memory
// shim is the honest fix: guarding the real `readTheme` would be production code existing only
// for a check. Same spirit as the useLayoutEffect silencer above. Assigned rather than
// `??=`-defaulted because merely *reading* node's absent `localStorage` prints an experimental
// warning, and this harness's output is read by humans in CI.
(globalThis as { localStorage?: unknown }).localStorage = {
  getItem: () => null, setItem: () => {}, removeItem: () => {},
};
// The appbar tile carries the Aware mark (four moments of a day arriving from faint to solid),
// not an event-category glyph. Asserted because the previous logo was a `Car` — a fossil of
// car_trip being the first derivation — and reaching into lucide for the brand is exactly the
// easy mistake to make twice. The opacity ramp is the mark's whole idea, so it's checked too:
// four dots at one strength is a different logo.
const shell = strip(renderToString(
  <MemoryRouter><AwareContext.Provider value={ctx}><Shell /></AwareContext.Provider></MemoryRouter>));
check("the appbar draws the brand tile", shell.includes('class="applogo"'));
// The portal sidebar is full of lucide glyphs that legitimately contain circles, so the
// four-dot assertion is scoped to the applogo tile's own svg rather than the whole shell.
const logoSvg = shell.slice(shell.indexOf('class="applogo"'));
const mark = logoSvg.slice(0, logoSvg.indexOf("</svg>") + 6);
check("the mark is four dots", (mark.match(/<circle/g) || []).length === 4,
  `${(mark.match(/<circle/g) || []).length} circles`);
check("...on an opacity ramp", ["0.26", "0.48", "0.74"].every((o) => mark.includes(`opacity="${o}"`)));
check("the mark inherits the tile's ink", mark.includes('fill="currentColor"'));
check("the wordmark is next to it", shell.includes(">Aware<"));

console.log("\n— the portal frame —");
// The sidebar derives from the registry (sections → modules → ghosts) and the frame never
// names a module itself. These pin the derivation, not the styling: a module entry must
// land under its section as a link, a planned entry as a non-link ghost, and Home must be
// a real route of its own.
check("Home links to /", shell.includes('href="/"'));
check("sections render as groups", [">Life<", ">Money<", ">Brain<", ">Config<"].every((s) => shell.includes(s)));
check("modules render under their section", shell.includes(">Day timeline") && shell.includes(">Levels"));
check("planned entries are ghosts, not links",
  shell.includes("sn planned") && !shell.match(/<a[^>]*sn planned/));
check("the palette trigger is in the top bar", shell.includes("⌘K"));
check("the period control hides until a module opts in", !shell.includes('aria-label="period"'));

// Home is composed, not hand-written: the spine draws the latest day's surviving spans in
// the board's capsule grammar, and the cards column holds whatever modules registered.
const homeHtml = strip(renderToString(
  <MemoryRouter><AwareContext.Provider value={ctx}><HomeView /></AwareContext.Provider></MemoryRouter>));
check("home splits into spine + cards", homeHtml.includes("hm-cols") && homeHtml.includes("hm-cards"));
check("the spine draws capsules", (homeHtml.match(/vt-cap/g) || []).length >= 2);
check("an unnamed stay is hollow on the spine too", homeHtml.includes("vt-cap hollow"));
check("a registered HomeCard renders", homeHtml.includes("Last journey"));

console.log("\n— a journey draws as a journey (ADR 0010) —");
// Built standalone rather than added to `rows`: a `trip` overlapping the day's car_trip would
// add a concurrent span and change the layout's sub-column count, breaking the geometry checks
// above for reasons that have nothing to do with what is being tested here.
const journeyEv = (
  id: string, name: string, span: [string, string],
  j?: { from: string | null; to: string | null }, vehicle?: string[],
  mode: string | null = "driving",
) => ({
  id, name, event_class: "derived" as const, occurred_epoch: at(span[1]),
  epoch: at(span[1]), date: new Date(at(span[1]) * 1000),
  message: {
    name,
    derived_from: [],
    interval: { started_at: at(span[0]), ended_at: at(span[1]), duration_seconds: at(span[1]) - at(span[0]) },
    ...(j ? { journey: {
      origin: { lat: 47.2, lon: 8.57, spread_m: 0, label: j.from },
      destination: { lat: 47.16, lon: 8.44, spread_m: 0, label: j.to },
      straight_line_m: 11350, path_m: 23960, ...(mode ? { mode } : {}),
    } } : {}),
    ...(vehicle ? { vehicle: { evidence: vehicle, confirmed: vehicle.length >= 2 } } : {}),
  },
}) as unknown as AwareEvent;

const ownCar = journeyEv("t1", "trip", ["07:50", "08:13"], { from: "Home", to: "Konditorei von Rotz Baar" },
                         ["got_out_the_car", "got_into_the_car"]);
const onFoot = journeyEv("t6", "trip", ["19:00", "19:20"], { from: "Home", to: null }, undefined, "walking");
const modeless = journeyEv("t7", "trip", ["20:00", "20:20"], { from: null, to: null }, undefined, null);
const borrowed = journeyEv("t2", "trip", ["14:50", "15:16"], { from: "Home", to: "ENNETSeeKLINIK" });
const toOnly = journeyEv("t3", "trip", ["16:00", "16:20"], { from: null, to: "Home" });
const fromOnly = journeyEv("t4", "trip", ["17:00", "17:20"], { from: "Home", to: null });
const bare = journeyEv("t5", "trip", ["18:00", "18:20"], { from: null, to: null });

// The bug this group exists for: `trip` shipped with a correct `interval` on all 20 events in Neon
// and still drew as a disc in the moments lane, next to `credit_card_payment` — because SPAN_EVENTS
// is an allowlist and nobody added it. An interval in the data is not a span on the board.
check("a trip is a span", isSpan(ownCar));
check("a trip is not filed as a moment", laneOf(ownCar) === "activity", laneOf(ownCar));
// Titled by MODE, not by its endpoints. The route in the title ran to 38 chars against a stay's 30,
// wrapped `.ev-head`, and shoved the capsule lane around — and 9 of the first 21 journeys had an
// unlabelled end, so the half-missing "To Home" form was the common case rather than the edge.
check("a drive is titled by its mode", labelOf(ownCar) === "Drive", labelOf(ownCar));
check("a walk is titled as a walk", labelOf(onFoot) === "Walk", labelOf(onFoot));
check("a journey with no mode falls back to its verb", labelOf(modeless) === "Trip", labelOf(modeless));
// Short enough that a trip can never be the title that overflows the lane.
check("every trip title is short", [ownCar, onFoot, modeless, toOnly, bare].every((t) => labelOf(t).length <= 6),
  [ownCar, onFoot, modeless].map(labelOf).join("/"));
// The route keeps its place one rung down, as ONE end — the destination when known, else the origin.
check("the route shows the destination when known", routeOf(ownCar) === "to Konditorei von Rotz Baar",
  String(routeOf(ownCar)));
check("...and falls back to the origin", routeOf(onFoot) === "from Home" && routeOf(fromOnly) === "from Home",
  String(routeOf(onFoot)));
check("...and is absent when neither end matched", routeOf(bare) === null, String(routeOf(bare)));
check("a non-journey event has no route", routeOf(E("e14")) === null, String(routeOf(E("e14"))));
// A journey must not change colour depending on which event expressed it; the icon carries the
// difference. Guards against `trip` falling through catOf to the anonymous grey circle.
check("a trip is not an anonymous dot", catOf("trip").c === catOf("car_trip").c && catOf("trip").c !== "#9298a6",
  catOf("trip").c);
check("...but its icon differs from car_trip's", catOf("trip").Icon !== catOf("car_trip").Icon);

// The own-car glyph: a journey the `vehicle` capability corroborated wears a small car beside its
// title. Presence of evidence, not `confirmed` — one boundary inside the span already separates
// own-car from borrowed perfectly on real data (ADR 0010), while `confirmed` (two boundaries)
// undercounts. And it decorates rather than re-titles: a borrowed drive still reads "Drive".
const singleBoundary = journeyEv("t8", "trip", ["21:00", "21:15"], { from: null, to: "Home" }, ["got_out_the_car"]);
check("an own-car journey is car-corroborated", carCorroborated(ownCar));
check("...one boundary inside the span is enough", carCorroborated(singleBoundary));
check("a borrowed-car journey is not", !carCorroborated(borrowed));
{
  const card = (e: AwareEvent) => strip(renderToString(<EventBody event={e} />));
  check("the own-car glyph renders beside the title", card(ownCar).includes("ev-car"));
  check("...even off a single boundary", card(singleBoundary).includes("ev-car"));
  check("a borrowed drive wears no car glyph", !card(borrowed).includes("ev-car"));
  check("...and is still titled Drive", labelOf(borrowed) === "Drive", labelOf(borrowed));
}

console.log("\n— supersession: one drive, one capsule —");
const pairedInside = journeyEv("c1", "car_trip", ["07:52", "08:11"]);      // inside the trip
const pairedEarly = journeyEv("c2", "car_trip", ["07:48", "08:05"]);       // starts BEFORE it
const otherDay = journeyEv("c3", "car_trip", ["20:00", "20:30"]);          // no trip covers it
const overlapStay = journeyEv("c4", "stay", ["07:00", "12:00"]);            // merely overlaps
{
  const sup = supersededIds([ownCar, borrowed, pairedInside, pairedEarly, otherDay, overlapStay]);
  check("a car_trip inside the trip is suppressed", sup.has("c1"));
  // Overlap, not containment: measured over 25 Jul - 1 Aug, on 2 of 14 own-car drives the entry
  // boundary preceded the journey's first settled fix by ~105s. Containment would draw those twice.
  check("a car_trip that starts before the trip is still suppressed", sup.has("c2"));
  // Preference, not deletion — every drive before the Overland lane landed has a car_trip and no
  // trip, and suppressing the type outright would blank those days.
  check("a car_trip no trip covers survives", !sup.has("c3"));
  // The reason this is name-keyed: a stay has an interval and overlaps the drive without
  // restating it, so a structural "interval superseded by an overlapping journey" rule would eat it.
  check("an overlapping stay is NOT superseded", !sup.has("c4"));
  check("the trips themselves are never superseded", !sup.has("t1") && !sup.has("t2"));
}
{
  // With no trip present nothing is suppressed — the pre-Overland shape of every day.
  const sup = supersededIds([pairedInside, otherDay, overlapStay]);
  check("no trip on the day means no suppression at all", sup.size === 0, `${sup.size} suppressed`);
}

console.log("\n— card chrome —");
// The kind chip ("inferred" / "signal") is gone from both lanes. It labelled the machinery rather
// than the day: every derived event said "inferred", which is true of almost everything on the board
// and so carries no information at the point of reading it. Depth/level chips still express it in
// the modal, where taxonomy belongs.
check("no lane labels events as inferred/signal", !dt.includes("ev-kind") && !dt.includes(">inferred<"),
  dt.includes("ev-kind") ? "ev-kind still rendered" : ">inferred< still rendered");
// A truncated title must still be readable without opening the modal.
check("titles carry a hover tooltip", dt.includes('title="Konditorei von Rotz Baar"'));

console.log("\n— the process tier (ADR 0012) —");
{
  // The REAL generated graph, not a fixture: `processes.json` is the contract the board reads,
  // so a renamed stage or a changed `after` breaks these checks rather than the deployed page.
  const invoice = (processGraph.processes as ProcessDef[])
    .find((p) => p.name === "dreamhost_invoice")!;
  check("the generated graph carries the invoice process", !!invoice);
  check("the genesis milestone is prepended as a stage", invoice.stages[0].name === "cycle_opened");

  const ms = (stage: string, epoch: number, message: Record<string, unknown> = {}) =>
    ({ name: `dreamhost_invoice_${stage}`, epoch, message });
  const cycle = (key: string, milestones: ReturnType<typeof ms>[]): Cycle => ({
    cycle_key: key, opened_epoch: milestones[0]?.epoch ?? 0,
    last_epoch: milestones[milestones.length - 1]?.epoch ?? 0,
    milestone_count: milestones.length, milestones,
  });

  // Cycle 009 as it actually stands: eight milestones, parked at gate ②.
  const live = statusOf(invoice, cycle("dh_invoice_2026_009", [
    ms("cycle_opened", 1788443748), ms("computed_lines", 1788443846),
    ms("data_approved", 1788443848), ms("approval_requested", 1788443870),
    ms("manual_lines", 1788445302), ms("total_computed", 1788445302, { total: "16128.00", currency: "USD" }),
    ms("invoice_generated", 1788445304), ms("invoice_emailed", 1788445326),
  ]));
  check("a part-way cycle counts its recorded steps", live.done === 8, `done=${live.done}`);
  check("the frontier is the first unreached stage", live.frontier?.name === "invoice_approved",
    live.frontier?.name ?? "none");
  check("the frontier reads `waiting`, not `pending`",
    live.stages.find((s) => s.stage.name === "invoice_approved")?.state === "waiting");
  // The distinction the prior art could not express: one gate is being actively polled, the
  // stages behind it are merely unreachable. Collapsing both to "not done" is what made a
  // stalled process indistinguishable from a waiting one.
  check("stages behind the frontier read `pending`",
    live.stages.find((s) => s.stage.name === "payment_processed")?.state === "pending");
  check("a cycle still in flight is not voided", !live.voided);

  // THE ORDERING TRAP, and the reason this check exists. A satisfied `await` is stamped with
  // its EVIDENCE's time rather than the run clock, so `data_approved` (the approval mail's own
  // Date header) legitimately predates the `approval_requested` that asked for it — observed on
  // BOTH real invoice cycles. Anything that sorted these rows by `epoch` would render the
  // approval above the request and read as though the process ran backwards.
  const shuffled = statusOf(invoice, cycle("dh_invoice_2026_009", [
    ms("data_approved", 1788443848), ms("approval_requested", 1788443870),
    ms("cycle_opened", 1788443748), ms("computed_lines", 1788443846),
  ]));
  check("order comes from the definition, not the timestamps",
    shuffled.stages.map((s) => s.stage.name).slice(0, 4).join(",")
      === "cycle_opened,computed_lines,approval_requested,data_approved");
  check("an out-of-order await is still done",
    shuffled.stages.find((s) => s.stage.name === "data_approved")?.state === "done");

  // Cycle 008: every stage recorded.
  const complete = statusOf(invoice, cycle("dh_invoice_2026_008",
    invoice.stages.map((st, i) => ms(st.name, 1786884650 + i))));
  check("a complete cycle has no frontier", complete.frontier === undefined);
  check("a complete cycle is all done", complete.done === complete.total,
    `${complete.done}/${complete.total}`);

  // Voided: terminal, so nothing is waiting on anything. Correction is a re-run under a new
  // cycle_key, never an amendment — a `waiting` gate here would imply the reconciler is still
  // watching a cycle it has abandoned.
  const voided = statusOf(invoice, cycle("dh_invoice_2026_010", [
    ms("cycle_opened", 1788000000), ms("computed_lines", 1788000010),
    { name: invoice.void_event, epoch: 1788000020, message: {} },
  ]));
  check("a voided cycle is flagged", voided.voided);
  check("a voided cycle has no frontier", voided.frontier === undefined);
  check("unreached stages of a voided cycle read `skipped`",
    voided.stages.find((s) => s.stage.name === "total_computed")?.state === "skipped");

  // chipsOf is shape-driven so process #2 renders without a code change.
  const chips = chipsOf({
    id: "x", name: "n", process: "p", user_id: "u", cycle_key: "k", timestamp: 1,
    total: "16128.00", currency: "USD", line_count: 1, lines: [1, 2, 3],
  }, 8);
  check("envelope keys are not rendered as facts",
    !chips.some((c) => ["id", "name", "process", "user_id", "cycle_key", "timestamp"].includes(c.key)),
    chips.map((c) => c.key).join(","));
  check("a collection collapses to a count",
    chips.find((c) => c.key === "lines")?.value === "3");
  const linked = chipsOf({ pdf_url: "https://example.com/output.pdf" });
  check("a URL becomes a link", linked[0]?.href === "https://example.com/output.pdf");

  check("a monthly cron reads as English", cronText("0 9 1 * *") === "monthly on the 1st, 09:00",
    cronText("0 9 1 * *"));
  check("an unrecognised cron falls back to the expression",
    cronText("*/5 3 * 7 2") === "*/5 3 * 7 2");

  // --- and on the TIMELINE, where a milestone is just another raw row ---
  check("a milestone is recognised as a process event",
    processOf("dreamhost_invoice_total_computed") === "dreamhost_invoice");
  check("an ordinary event is not", processOf("credit_card_payment") === null);
  // The prefix must be matched with its separator, or a process named `dreamhost` would claim
  // every event whose name merely starts with those letters.
  check("a bare prefix without the separator does not match",
    processOf("dreamhost_invoicexyz") === null);
  // Through prepare(), like every other fixture here — it decorates the row with `epoch` and
  // `date`, which labelOf's AwareEvent contract requires.
  const milestone = prepare(
    [ev("dreamhost_invoice_total_computed", "raw", "14:21")] as unknown as AwareEvent[]).all[0];
  check("a milestone titles itself by its stage", labelOf(milestone) === "Total computed",
    labelOf(milestone));
  check("a milestone is not an anonymous grey dot",
    catOf("dreamhost_invoice_total_computed").c !== "#9298a6");
  // The Levels board has no day around it, so there the process name stays.
  check("the levels board keeps the process name",
    typeLabel("dreamhost_invoice_total_computed") === "Dreamhost invoice: total computed",
    typeLabel("dreamhost_invoice_total_computed"));

  // And it renders. The board fetches through ctx.client, so on the server it draws its
  // loading line — enough to catch an import cycle or a crash in the module's top level.
  const html = renderToString(
    <AwareContext.Provider value={ctx}>
      <MemoryRouter><ProcessesDashboard /></MemoryRouter>
    </AwareContext.Provider>,
  );
  check("the processes board renders", html.length > 0);
}

console.log("\n— email todo tasks —");
{
  const NOW = 1788000000;
  const D = 86400;
  const task = (id: string, agedays: number, over: Partial<Task> = {}): Task => ({
    upstream_id: id, subject: "Renew car insurance", from_name: "AXA",
    from: "service@axa.ch", thread_id: "t" + id,
    opened_epoch: NOW - agedays * D, closed_epoch: null, closed_via: null, closed: false,
    ...over,
  });

  const g = groupTasks([
    task("a", 12), task("b", 9), task("c", 3), task("d", 1),
    task("e", 20, { closed: true, closed_epoch: NOW - 2 * D, closed_via: "sweep" }),
  ], NOW);

  check("tasks over a week old are grouped as stale", g.stale.length === 2, `${g.stale.length}`);
  check("tasks inside a week are grouped as recent", g.recent.length === 2, `${g.recent.length}`);
  check("a closed task leaves the open groups", g.open === 4, `open=${g.open}`);
  // Oldest FIRST inside a group: the thing most likely to be a problem is the thing furthest
  // from the top in a conventional newest-first list, which is exactly backwards for this board.
  check("the oldest task is at the top of its group",
    g.stale[0].upstream_id === "a", g.stale[0].upstream_id);
  check("the age headline ignores closed tasks", g.oldestDays === 12, `${g.oldestDays}`);
  check("closed-this-week counts only recent closes", g.closedThisWeek === 1);

  // A reopened task (label re-applied after a close) arrives from the API already marked open,
  // because the SQL compares the LATEST open against the LATEST close. If that ever regressed to
  // an IS NULL anti-join it would vanish from the board while sitting labelled in Gmail — and
  // the hourly sweep would emit a fresh open every hour, forever.
  const reopened = groupTasks([task("r", 2, { closed: false, closed_epoch: NOW - 30 * D })], NOW);
  check("a reopened task is open again", reopened.open === 1 && reopened.recent.length === 1);

  check("an empty list does not report an age", groupTasks([], NOW).oldestDays === 0);

  check("ages read as ages", ageLabel(NOW - 12 * D, NOW) === "12d", ageLabel(NOW - 12 * D, NOW));
  check("a fresh task reads in hours", ageLabel(NOW - 3600 * 5, NOW) === "5h");
  check("a just-arrived task says so", ageLabel(NOW - 30, NOW) === "just now");

  check("a task links to its Gmail thread",
    gmailLink(task("a", 1)).includes("/#all/ta"), gmailLink(task("a", 1)));
  // Falls back to a search rather than producing a dead link, for rows recorded before the
  // thread id was captured.
  check("a task with no thread still links somewhere",
    gmailLink(task("a", 1, { thread_id: null })).includes("rfc822msgid"));
  check("a subjectless mail still has a line to read",
    subjectOf(task("a", 1, { subject: null })) === "(no subject)");

  const html = renderToString(
    <AwareContext.Provider value={ctx}>
      <MemoryRouter><TasksDashboard /></MemoryRouter>
    </AwareContext.Provider>,
  );
  check("the tasks board renders", html.length > 0);
}

if (fails.length) throw new Error(`${fails.length} check(s) failed: ${fails.join("; ")}`);
console.log("\nall checks passed\n");
