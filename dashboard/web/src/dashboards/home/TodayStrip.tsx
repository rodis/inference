import type { AwareEvent } from "../../types";
import { catOf, endOf, fmtTime, humanDur, iconOf, inkOn, labelOf, placeUnknown, startOf } from "../../view";

/** How wide (in % of the window) a journey must be to render as a capsule rather than a
 *  disc, and a capsule must be to carry text rather than its icon alone. The same instinct
 *  as the day board's CAP_MIN floor: below the threshold the *form* changes instead of the
 *  content squeezing. */
const JOURNEY_CAPSULE_MIN = 4.5;
const CAPSULE_TEXT_MIN = 7;

/** The horizontal rendering of the day — the day board's own grammar rotated 90° (variant
 *  B of the style sheet): stays as shadowed capsules with icon + name + duration inside,
 *  journeys as the moments lane's hollow discs riding a dotted connector — graduating to a
 *  blue capsule when the drive is long enough to be one — and dead time as the connector
 *  itself. Hollow capsule = unnamed place, as everywhere.
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

  const discs = events.filter((e) => !e.message.place && pct(endOf(e)) - pct(startOf(e)) < JOURNEY_CAPSULE_MIN);

  return (
    <section className="panel hstrip">
      <h3 className="hc-head">Today, across</h3>
      <div className="psub">the same day as the spine, horizontal — the board's capsules and discs, rotated</div>
      <div className="hstrip-row">
        <div className="hstrip-link" aria-hidden="true" />
        {events.map((e) => {
          const s = startOf(e), en = endOf(e);
          const left = pct(s), width = pct(en) - pct(s);
          const stay = !!e.message.place;
          const cat = catOf(e.name).c;
          const Icon = iconOf(e);
          const tip = `${labelOf(e)} · ${fmtTime(new Date(s * 1000))}–${fmtTime(new Date(en * 1000))} · ${humanDur(en - s)}`;

          // A short journey is a moment-weight object: a hollow disc at its midpoint.
          if (!stay && width < JOURNEY_CAPSULE_MIN) {
            return (
              <button key={e.id} type="button" className="hstrip-disc"
                style={{ left: `${left + width / 2}%`, ["--cat" as string]: cat }}
                title={tip} onClick={onOpen}>
                <Icon size={12} strokeWidth={2.5} />
              </button>
            );
          }

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
      {discs.length > 0 && (
        <div className="hstrip-sub" aria-hidden="true">
          {discs.map((e) => (
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
