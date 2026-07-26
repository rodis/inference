import type { AwareEvent } from "../types";
import type { DayLayout } from "../view";
import { catOf, fmtTime, hostOf, humanDur, inkOn, intervalOf, isSpan, labelOf, placeUnknown, startOf } from "../view";
import EventBody from "./EventBody";

interface Props {
  events: AwareEvent[];
  layout: DayLayout;
  onSelect: (e: AwareEvent) => void;
  /** 0..1 — how revealed an event is at the current altitude (1 = full detail). */
  revealOf: (e: AwareEvent) => number;
}

const CAP_W = 46;      // one capsule sub-column, must match --capw in styles.css
const HIT_EPS = 0.1;   // below this an event is decorative — no pointer, no tab stop

/** The day as two parallel timelines on one shared time scale.
 *
 *  Left lane — **activities**: events with a meaningful duration, as capsules whose length is
 *  `Y(end) − Y(start)` on the shared scale. Concurrent activities sit in sub-columns.
 *
 *  Right lane — **moments**: points in time, as smaller hollow discs on their own dotted rail.
 *  A moment that fell inside an activity renders within that activity's vertical range, and the
 *  activity casts a tinted **band** across the lane to say so — figure/ground rather than a
 *  tether line per dot, so five payments inside one visit stay legible.
 *
 *  Half the visual weight on the right is deliberate: the left lane is the shape of the day,
 *  the right lane is texture within it. See `dayLayout` for the scale and the lane rules.
 *
 *  Cards here carry no L / override / D chips and no "N below" rollup: the day is for reading
 *  what happened, and the taxonomy is one tap away in the event modal (`EventModal`). */
export default function DayTimeline({ events, layout, onSelect, revealOf }: Props) {
  const { pos, spans, cols, links, bands, hosts, gaps, h } = layout;

  if (!events.length) return <div className="dt-wrap"><div className="dt-empty">— nothing here —</div></div>;

  const activities = events.filter(isSpan).sort((a, b) => startOf(a) - startOf(b));
  const moments = events.filter((e) => !isSpan(e)).sort((a, b) => a.epoch - b.epoch);

  return (
    /* --capcols lives on the wrapper so the lane headers and the lanes derive the same
       boundary from it — the activity lane widens when a day needs side-by-side capsules. */
    <div className="dt-wrap" style={{ ["--capcols" as string]: cols }}>
      <div className="dt-head">
        <div>
          <span className="lh-title">Activities</span>
          <span className="lh-sub">intervals · capsule ∝ duration</span>
        </div>
        <div>
          <span className="lh-title">Moments</span>
          <span className="lh-sub">points in time · placed inside their host</span>
        </div>
      </div>

      <div className="dt-lanes" style={{ height: h }}>
      <div className="dt-rule" />
      <div className="dt-rail" />

      {/* the activity lane's own track: dotted connectors down the dead time between capsules */}
      {links.map((l, i) => (
        <div key={"link-" + i} className="dt-link"
          style={{ top: l.top, height: l.height, ["--lcol" as string]: l.col }} />
      ))}

      {/* containment: a host's stripe across the moments lane, longest host first */}
      {bands.map((b) => (
        <div key={"band-" + b.hostId} className={"dt-band" + (b.weak ? " unnamed" : "")}
          style={{
            top: b.top, height: b.height, color: b.color,
            background: `color-mix(in srgb, ${b.color} calc(var(--band-a) * 100%), transparent)`,
          }} />
      ))}

      {/* a genuinely quiet stretch, collapsed to a labelled divider */}
      {gaps.map((g, i) => (
        <div key={"gap-" + i} className="dt-gap" style={{ top: g.y }}>
          <span>{humanDur(g.seconds)} quiet</span>
        </div>
      ))}

      {activities.map((e) => {
        const box = spans.get(e.id);
        const top = box?.top ?? pos.get(e.id) ?? 0;
        const cat = catOf(e.name), r = revealOf(e), iv = intervalOf(e);
        // An activity at an unnamed place is drawn hollow (see `placeUnknown`) via a class, never
        // by lowering `opacity` on this row: that number is the altitude reveal, and folding two
        // meanings into it would make "faded" ambiguous between "deep" and "unnamed". The class
        // restyles the capsule *inside* the row, so the two readings stay on separate channels.
        return (
          <div key={e.id} className={"dt-act" + (placeUnknown(e) ? " unnamed" : "")}
            style={{ top, opacity: r, pointerEvents: r < HIT_EPS ? "none" : undefined }}>
            <div className="t">{fmtTime(new Date((iv?.started_at ?? e.epoch) * 1000))}</div>
            <div className="caps" style={{ width: cols * CAP_W }}>
              {/* The category colour reaches CSS as `--cat` (and its readable ink as `--capink`)
                  rather than as a fixed `background`, so the unnamed variant can restate that
                  same hue as a dotted border and as the icon. */}
              <div className="capsule"
                style={{
                  ["--cat" as string]: cat.c, ["--capink" as string]: inkOn(cat.c),
                  height: box?.height ?? 44, marginLeft: (box?.col ?? 0) * CAP_W,
                }}>
                <cat.Icon size={18} strokeWidth={2.25} />
              </div>
            </div>
            <button className="dt-body" onClick={() => onSelect(e)} tabIndex={r < HIT_EPS ? -1 : undefined}>
              <EventBody event={e} />
            </button>
          </div>
        );
      })}

      {moments.map((e) => {
        const y = pos.get(e.id) ?? 0;
        const cat = catOf(e.name), r = revealOf(e);
        // Three honest states, and the band already covers the first:
        //   band on screen        → say nothing, figure/ground has it
        //   host exists but is above the altitude → name it in text, since nothing draws it
        //   no containing span at all            → flag it; it may be an activity we can't infer yet
        const banded = hosts.has(e.id);
        const anyHost = banded ? null : hostOf(e, activities);
        return (
          <div key={e.id} className="dt-mom" style={{ top: y, opacity: r, pointerEvents: r < HIT_EPS ? "none" : undefined }}>
            <div className="t">{fmtTime(e.date)}</div>
            <div className="disc" style={{ color: cat.c }}><cat.Icon size={13} strokeWidth={2.4} /></div>
            <button className="dt-body" onClick={() => onSelect(e)} tabIndex={r < HIT_EPS ? -1 : undefined}>
              <EventBody event={e} compact
                orphan={!banded && !anyHost}
                hostLabel={anyHost ? labelOf(anyHost) : undefined} />
            </button>
          </div>
        );
      })}
      </div>
    </div>
  );
}
