import type { AwareEvent } from "../../types";
import { catOf, endOf, fmtTime, humanDur, labelOf, placeUnknown, startOf } from "../../view";

/** The horizontal rendering of the day — the v1 sketch's strip, kept alongside the
 *  vertical spine as a deliberate A/B on real data. One track, spans positioned by time
 *  of day: stays filled in their category colour (hollow when unnamed), journeys thin and
 *  blue, dead time as bare track. Labels draw only for spans wide enough to carry them;
 *  every span has the full facts in its tooltip.
 *
 *  The window is the data's, not a fixed 08–22: from the first span's hour to the last
 *  end (or now, when the day is today), padded to whole hours, floored to 8h so a
 *  one-errand day doesn't stretch a 40-minute stay across the page. */
export default function TodayStrip({ events, isToday }: { events: AwareEvent[]; isToday: boolean }) {
  if (events.length === 0) return null;

  const now = Date.now() / 1000;
  const HOUR = 3600;
  let w0 = Math.floor(Math.min(...events.map(startOf)) / HOUR) * HOUR;
  let w1 = Math.ceil(Math.max(...events.map(endOf), isToday ? now : 0) / HOUR) * HOUR;
  if (w1 - w0 < 8 * HOUR) w1 = w0 + 8 * HOUR;
  const span = w1 - w0;
  const pct = (t: number) => ((Math.min(Math.max(t, w0), w1) - w0) / span) * 100;

  // Hour ticks: aim for ~6 labels whatever the window covers.
  const stepH = Math.max(1, Math.round(span / HOUR / 6));
  const ticks: number[] = [];
  for (let t = w0; t <= w1; t += stepH * HOUR) ticks.push(t);

  return (
    <section className="panel hstrip">
      <h3 className="hc-head">Today, across</h3>
      <div className="psub">the same day as the spine, horizontal — an exploration, not a replacement</div>
      <div className="hstrip-track">
        {events.map((e) => {
          const s = startOf(e), en = endOf(e);
          const cat = catOf(e.name).c;
          const stay = !!e.message.place;
          const hollow = placeUnknown(e);
          return (
            <div
              key={e.id}
              className={"hstrip-span" + (stay ? " stay" : " jour") + (hollow ? " hollow" : "")}
              style={{
                left: `${pct(s)}%`,
                width: `${Math.max(pct(en) - pct(s), 0.6)}%`,
                ["--cat" as string]: cat,
              }}
              title={`${labelOf(e)} · ${fmtTime(new Date(s * 1000))}–${fmtTime(new Date(en * 1000))} · ${humanDur(en - s)}`}
            />
          );
        })}
        {isToday && <div className="hstrip-now" style={{ left: `${pct(now)}%` }} aria-hidden="true" />}
      </div>
      <div className="hstrip-labels">
        {events.map((e) => {
          const s = startOf(e), en = endOf(e);
          const wide = pct(en) - pct(s) >= 8; // narrower spans keep their tooltip only
          if (!wide) return null;
          return (
            <span key={e.id} className="hstrip-label" style={{ left: `${Math.min(pct(s), 86)}%` }}>
              {labelOf(e)} · {humanDur(en - s)}
            </span>
          );
        })}
      </div>
      <div className="hstrip-hours" aria-hidden="true">
        {ticks.map((t) => (
          <span key={t} style={{ left: `${pct(t)}%` }}>
            {String(new Date(t * 1000).getHours()).padStart(2, "0")}
          </span>
        ))}
      </div>
    </section>
  );
}
