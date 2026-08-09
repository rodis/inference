import type { AwareEvent } from "../../types";
import { catOf, endOf, fmtTime, humanDur, iconOf, inkOn, labelOf, placeUnknown, startOf } from "../../view";

/** How wide (in % of the window) a capsule must be to carry text rather than its icon
 *  alone. Below it the form simplifies instead of the content squeezing — the day board's
 *  CAP_MIN instinct. */
const CAPSULE_TEXT_MIN = 7;

/** The horizontal rendering of the day — the vertical spine's exact objects rotated 90°:
 *  every event is a filled capsule in its category colour (hollow when the place is
 *  unnamed), journeys included — a short drive is a small blue capsule with its time
 *  beneath, never a different shape. Dead time is the dotted connector. A narrow capsule
 *  floors to icon width (min-width), trading a few px of proportion for legibility, the
 *  same trade the board's CAP_MIN makes vertically.
 *
 *  The window is the data's own: first span to last end (or now, when the day is today),
 *  padded to whole hours, floored to 8h so a one-errand day doesn't stretch a 40-minute
 *  stay across the page. */
export default function TodayStrip({ events, isToday, onOpen }: {
  events: AwareEvent[]; isToday: boolean; onOpen: () => void;
}) {
  if (events.length === 0) return null;

  const now = Date.now() / 1000;
  const HOUR = 3600;
  let w0 = Math.floor(Math.min(...events.map(startOf)) / HOUR) * HOUR;
  let w1 = Math.ceil(Math.max(...events.map(endOf), isToday ? now : 0) / HOUR) * HOUR;
  if (w1 - w0 < 8 * HOUR) w1 = w0 + 8 * HOUR;
  const span = w1 - w0;
  const pct = (t: number) => ((Math.min(Math.max(t, w0), w1) - w0) / span) * 100;

  const stepH = Math.max(1, Math.round(span / HOUR / 6));
  const ticks: number[] = [];
  for (let t = w0; t <= w1; t += stepH * HOUR) ticks.push(t);

  const narrow = events.filter((e) => pct(endOf(e)) - pct(startOf(e)) < CAPSULE_TEXT_MIN);

  return (
    <section className="panel hstrip">
      <h3 className="hc-head">Today, across</h3>
      <div className="psub">the same day as the spine, horizontal — the same capsules, rotated</div>
      <div className="hstrip-row">
        <div className="hstrip-link" aria-hidden="true" />
        {events.map((e) => {
          const s = startOf(e), en = endOf(e);
          const left = pct(s), width = pct(en) - pct(s);
          const stay = !!e.message.place;
          const cat = catOf(e.name).c;
          const Icon = iconOf(e);
          const tip = `${labelOf(e)} · ${fmtTime(new Date(s * 1000))}–${fmtTime(new Date(en * 1000))} · ${humanDur(en - s)}`;
          const hollow = placeUnknown(e);
          const withText = width >= CAPSULE_TEXT_MIN;
          return (
            <button key={e.id} type="button"
              className={"hstrip-cap" + (hollow ? " hollow" : "") + (withText ? "" : " iconly")}
              style={{
                left: `${left}%`, width: `${width}%`,
                ["--cat" as string]: cat,
                color: hollow ? cat : inkOn(cat),
              }}
              title={tip} onClick={onOpen}>
              <Icon size={13} strokeWidth={2.4} />
              {withText && stay && <span className="nm">{labelOf(e)}</span>}
              {withText && <span className="du">{humanDur(en - s)}</span>}
            </button>
          );
        })}
        {isToday && <div className="hstrip-now" style={{ left: `${pct(now)}%` }} aria-hidden="true" />}
      </div>
      {narrow.length > 0 && (
        <div className="hstrip-sub" aria-hidden="true">
          {narrow.map((e) => (
            <span key={e.id} style={{ left: `${pct(startOf(e)) + (pct(endOf(e)) - pct(startOf(e))) / 2}%` }}>
              {fmtTime(new Date(startOf(e) * 1000))}
            </span>
          ))}
        </div>
      )}
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
