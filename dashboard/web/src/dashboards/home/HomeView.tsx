import { useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { useAware } from "../../app/useAware";
import { MODULES } from "../../app/registry";
import TodayStrip from "./TodayStrip";
import type { AwareEvent } from "../../types";
import {
  catOf, dayKey, endOf, fmtTime, humanDur, iconOf, isEverydayPlace, isSpan, labelOf,
  placeUnknown, routeOf, startOf, supersededIds,
} from "../../view";

const MON = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const DOW = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

/** Height of a spine capsule: proportional to duration, floored so a 5-minute drive stays
 *  legible and capped so a 6-hour stay doesn't own the column. Same instinct as the day
 *  board's MIN_STEP, at card scale. */
const capH = (sec: number) => Math.round(Math.min(120, Math.max(26, (sec / 60) * 0.55)));

/** A quiet-gap chip appears between two rows when the dead time is long enough to be a
 *  fact about the day rather than layout noise. */
const GAP_CHIP_SECONDS = 45 * 60;

/** Home: the latest day as a vertical spine (the day board's own grammar, shrunk), and one
 *  glanceable card per module that registered a HomeCard. The frame composes; it never
 *  knows what a card draws — that is the portal invariant (registry.tsx). */
export default function HomeView() {
  const { prepared, status, setSelectedDay } = useAware();
  const navigate = useNavigate();

  const day = prepared.days.length ? prepared.days[prepared.days.length - 1] : "";

  // The spine shows the day's *activities*: spans, minus restatements (a car_trip under its
  // journey) and minus everyday places — the same exclusions the day board makes, for the
  // same reasons (a home "visit" boundary is a sampling artifact, ADR 0007).
  const spine = useMemo(() => {
    const spans = prepared.all.filter((e) => dayKey(e.date) === day && isSpan(e));
    const superseded = supersededIds(spans);
    return spans
      .filter((e) => !superseded.has(e.id) && !isEverydayPlace(e))
      .sort((a, b) => startOf(a) - startOf(b));
  }, [prepared, day]);

  const cards = MODULES.filter((m) => m.HomeCard);
  const openDay = () => { setSelectedDay(day); navigate("/d/timeline"); };

  if (status) return <div className="statusline">{status}</div>;

  const dh = day ? new Date(day + "T00:00:00") : null;
  const isToday = day === dayKey(new Date());

  return (
    <>
      {dh && (
        <div className="pagehead">
          <div className="eyebrow">{isToday ? "today" : "latest day"}</div>
          <h1 className="ptitle">
            {DOW[dh.getDay()]}, {dh.getDate()} {MON[dh.getMonth()]} <span className="hm-year">{dh.getFullYear()}</span>
          </h1>
        </div>
      )}

      <TodayStrip events={spine} isToday={isToday} onOpen={openDay} />

      <div className="hm-cols">
        <section className="panel hm-spine">
          <h3 className="hc-head">
            Today
            <button type="button" className="hc-go" onClick={openDay}>open day timeline →</button>
          </h3>
          <div className="psub">stays and journeys · everyday places stay off the spine, as on the day board</div>
          {spine.length === 0 && <div className="dt-empty">nothing derived for this day yet</div>}
          <div className="vt">
            {spine.map((e, i) => (
              <SpineRow key={e.id} e={e} prev={i > 0 ? spine[i - 1] : null} onOpen={openDay} />
            ))}
            {isToday && spine.length > 0 && (
              <div className="vt-now">
                <span className="vt-t">{fmtTime(new Date())}</span>
                <span className="nd" aria-hidden="true" />
                <span className="nl">now</span>
              </div>
            )}
          </div>
        </section>

        <div className="hm-cards">
          {cards.map((m) => {
            const Card = m.HomeCard!;
            return <Card key={m.slug} />;
          })}
        </div>
      </div>
    </>
  );
}

function SpineRow({ e, prev, onOpen }: { e: AwareEvent; prev: AwareEvent | null; onOpen: () => void }) {
  const iv = { start: startOf(e), end: endOf(e) };
  const gap = prev ? iv.start - endOf(prev) : 0;
  const cat = catOf(e.name);
  const Icon = iconOf(e);
  const hollow = placeUnknown(e);
  const meta =
    e.message.place ? `stay · ${humanDur(iv.end - iv.start)}`
    : `${e.message.journey?.mode ?? "journey"} · ${humanDur(iv.end - iv.start)}${routeOf(e) ? ` · ${routeOf(e)}` : ""}`;
  return (
    <>
      {gap > GAP_CHIP_SECONDS && (
        <div className="vt-gap"><span>{humanDur(gap)} quiet</span></div>
      )}
      <div className="vrow">
        <span className="vt-t">{fmtTime(new Date(iv.start * 1000))}</span>
        <button
          type="button" className={"vt-cap" + (hollow ? " hollow" : "")}
          style={{ height: capH(iv.end - iv.start), ["--cat" as string]: cat.c }}
          onClick={onOpen} title={`${labelOf(e)} · ${fmtTime(new Date(iv.start * 1000))}–${fmtTime(new Date(iv.end * 1000))}`}
        >
          <Icon size={15} strokeWidth={2.25} />
        </button>
        <div className="vt-body">
          <div className={"vt-name" + (hollow ? " dim" : "")}>{labelOf(e)}</div>
          <div className="vt-meta">{meta}</div>
        </div>
      </div>
    </>
  );
}
