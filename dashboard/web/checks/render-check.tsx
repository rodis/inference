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
 * innermost must win), a 60-second charge whose capsule has to be floored to stay legible, and a
 * café stay whose end lands a minute *after* the drive home began (a handoff, not concurrency).
 *
 * Adding a case is one `check(...)` line. Throws at the end if anything failed, which is what
 * makes `npm run check` exit non-zero for CI.
 */
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import Shell from "../src/app/Shell";
import { AwareContext } from "../src/app/useAware";
import type { AwareCtx } from "../src/app/useAware";
import DayTimeline from "../src/components/DayTimeline";
import EventModal from "../src/components/EventModal";
import LevelsDashboard from "../src/dashboards/levels/LevelsDashboard";
import TimelineDashboard from "../src/dashboards/timeline/TimelineDashboard";
import { catOf, dayLayout, defaultLevelOf, hostOf, inkOn, isEverydayPlace, isSpan, labelOf, laneCount, laneNames, placeUnknown, prepare } from "../src/view";
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
// Lane is about KIND, not data quality: a 60-second charge is still a charge, and a 32-second
// car_trip is still a trip. Filing short intervals as moments put car_trip in both lanes on the
// same day, which read as a broken categorisation rather than the bad inference it is.
check("a 60-second charge is still a span", isSpan(E("e11")));
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
// 3 since stays joined the fixture: the trip, the 6h charge, and the named stay (which hosts the
// 09:00 ping). A stay overlapping a charge — being home while the phone charges — is a real
// concurrency shape, so it belongs here rather than in a fixture of its own.
check("a band per hosting activity", L.bands.length === 3, `${L.bands.length} bands`);
// 6h charge, then the 96-minute stay, then the 19-minute trip — i.e. the same order as their
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
// "two things at once". (The 6h charge over the 19min trip above is the real thing, and still fans.)
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
const weakRows = (dt.match(/class="dt-act unnamed"/g) || []).length;
check("exactly the unnamed stay is drawn hollow", weakRows === 1, `${weakRows} of 8 rows`);
check("the other seven activities draw at full strength",
  (dt.match(/class="dt-act"/g) || []).length === 7, `${(dt.match(/class="dt-act"/g) || []).length}`);
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

// The duration bar is the card's one *exactly* proportional channel, and the reason it exists is
// that the capsule beside it is not: the vertical scale is floored for legibility and compressed
// past MAX_STEP, so px-per-minute varied ~6× across a real day (2026-07-27) and hit the LONGEST
// activity hardest — a 9× duration difference drew as 2.6×. Horizontal space has no such
// constraint, so it can be linear. Asserted numerically because a bar that renders but is scaled
// wrong looks entirely plausible on a screenshot.
const barW = [...dt.matchAll(/class="ev-bar"[^>]*>\s*<i style="width:\s*([\d.]+)%/g)].map((m) => +m[1]);
check("every activity card carries a duration bar — and only they do",
  barW.length === 8, `${barW.length} bars vs 8 capsules`);
check("the day's longest activity fills its bar", Math.max(...barW) === 100, `max ${Math.max(...barW)}%`);
// 96 minutes against the 6-hour charge that is the day's longest — 26%, and the capsule ratio for
// the same pair is nothing like it.
const stayPct = Math.round((96 * 60 / E("e10").message.interval!.duration_seconds) * 100);
check("a 96-minute stay reads as its true share of the day's longest",
  barW.some((w) => Math.abs(w - stayPct) < 1), `expected ~${stayPct}%, got ${barW.join(",")}`);
// A floor for *presence*, not proportion: 60s of 6h is 0.28% and would round to a sub-pixel
// sliver, reading as "no bar" — i.e. as missing data rather than as a short event. Same bargain as
// CAP_MIN on the capsule, which is why the exact figure stays in text right above it.
check("a 60-second span still shows a sliver rather than nothing", Math.min(...barW) === 2,
  `min ${Math.min(...barW)}%`);
// It restates the duration text, so it must not also claim to be interactive or announce itself
// twice to a screen reader.
check("the bar is decorative to assistive tech", dt.includes('class="ev-bar" aria-hidden="true"'));
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
check("the mark is four dots", (shell.match(/<circle/g) || []).length === 4,
  `${(shell.match(/<circle/g) || []).length} circles`);
check("...on an opacity ramp", ["0.26", "0.48", "0.74"].every((o) => shell.includes(`opacity="${o}"`)));
check("the mark inherits the tile's ink", shell.includes('fill="currentColor"'));
check("the wordmark is next to it", shell.includes(">Aware<"));

if (fails.length) throw new Error(`${fails.length} check(s) failed: ${fails.join("; ")}`);
console.log("\nall checks passed\n");
