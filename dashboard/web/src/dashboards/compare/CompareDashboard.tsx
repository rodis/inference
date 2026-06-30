import { useEffect, useMemo, useState } from "react";
import { useAware } from "../../app/useAware";
import type { AwareEvent } from "../../types";
import { buildScale, CAT, catOf, dayKey, typeLabel } from "../../view";
import VTimeline from "../../components/VTimeline";
import WeekStrip from "../../components/WeekStrip";
import EventModal from "../../components/EventModal";

const MON = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const DEFAULT_SERIES = ["car_lock_state_change", "device_connected_to_carplay", "device_disconnected_from_carplay", "car_door_opened", "car_door_closed"];

/** Compare series: pick any event types and see them as parallel lanes on one shared
 *  per-day time scale, so the same moment lines up across columns. */
export default function CompareDashboard() {
  const { prepared, getL, getCeil, status, selectedDay } = useAware();
  const { all, byId, derivLevel } = prepared;

  const [series, setSeries] = useState<Set<string>>(new Set());
  const [modalEvent, setModalEvent] = useState<AwareEvent | null>(null);

  // seed with the car-signal cluster whenever a new dataset loads
  useEffect(() => {
    const present = new Set(all.map((e) => e.name));
    setSeries(new Set(DEFAULT_SERIES.filter((n) => present.has(n))));
  }, [prepared]);

  const allTypes = useMemo(() => [...new Set(all.map((e) => e.name))], [all]);
  const chipOrder = useMemo(
    () => allTypes.slice().sort((a, b) => (CAT[a] ? 0 : 1) - (CAT[b] ? 0 : 1) || a.localeCompare(b)),
    [allTypes]
  );

  // one shared scale across every selected lane, so rows at the same time align horizontally
  const { lanes, scale } = useMemo(() => {
    const dayEvents = all.filter((e) => dayKey(e.date) === selectedDay);
    const picked = chipOrder.filter((n) => series.has(n));
    const lanes = picked.map((name) => ({ name, events: dayEvents.filter((e) => e.name === name) }));
    const scale = buildScale(lanes.flatMap((l) => l.events).map((e) => e.epoch));
    return { lanes, scale };
  }, [all, selectedDay, series, chipOrder]);

  function toggle(name: string) {
    setSeries((prev) => {
      const next = new Set(prev);
      next.has(name) ? next.delete(name) : next.add(name);
      return next;
    });
  }

  if (status) return <div className="statusline">{status}</div>;

  const dh = selectedDay ? new Date(selectedDay + "T00:00:00") : null;

  return (
    <>
      <div className="datehead">
        Compare{dh ? <> · <span className="dnum">{dh.getDate()}.</span> <span className="dmon">{MON[dh.getMonth()]}</span> <span className="dyear">{dh.getFullYear()}</span></> : null} <span className="chev">›</span>
      </div>
      <p className="page-intro">Pick any event types — each becomes a lane on one shared time scale, so co-occurring signals line up across columns.</p>

      <WeekStrip />

      <div className="cmpchips cmpchips-wide">
        {chipOrder.map((name) => {
          const cat = catOf(name), on = series.has(name);
          return (
            <button key={name} className={"cmp-chip" + (on ? " on" : "")}
              style={on ? { background: cat.c, borderColor: cat.c, color: "#fff" } : undefined}
              onClick={() => toggle(name)}>
              <span className="cd" style={{ background: on ? "#fff" : cat.c }} />{typeLabel(name)}
            </button>
          );
        })}
      </div>

      <div className="sheet">
        <div className="handle" />
        {lanes.length === 0 ? (
          <div className="vt-empty">— pick one or more series above —</div>
        ) : (
          <div className="cmplanes">
            {lanes.map((lane) => (
              <div className="cmplane" key={lane.name}>
                <div className="cmplane-head">
                  <span className="cd" style={{ background: catOf(lane.name).c }} />
                  <span className="cln">{typeLabel(lane.name)}</span>
                  <span className="clc">{lane.events.length}</span>
                </div>
                <VTimeline events={lane.events} scale={scale} getL={getL} getCeil={getCeil} derivLevel={derivLevel} onSelect={setModalEvent} />
              </div>
            ))}
          </div>
        )}
      </div>

      <EventModal event={modalEvent} byId={byId} getL={getL} derivLevel={derivLevel} onClose={() => setModalEvent(null)} />
    </>
  );
}
