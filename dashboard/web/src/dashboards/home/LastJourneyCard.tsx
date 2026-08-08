import { useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { useAware } from "../../app/useAware";
import type { AwareEvent } from "../../types";
import {
  carCorroborated, dayKey, endOf, fmtTime, humanDur, intervalOf, isSpan, startOf, supersededIds,
} from "../../view";

const DRIVE_NAMES = new Set(["journey", "trip", "car_trip"]);

/** Home card: the most recent journey, as a door into the day it happened. One fact per
 *  line — route, span, and the claims the event itself carries (mode, support, vehicle) —
 *  never the analysis. Registered on the Life module (registry.tsx) until a Journeys
 *  module exists to own it. */
export default function LastJourneyCard() {
  const { prepared, setSelectedDay } = useAware();
  const navigate = useNavigate();

  const last = useMemo<AwareEvent | null>(() => {
    const drives = prepared.all.filter((e) => DRIVE_NAMES.has(e.name) && isSpan(e));
    const superseded = supersededIds(drives);
    let best: AwareEvent | null = null;
    for (const e of drives) {
      if (superseded.has(e.id)) continue;
      if (!best || endOf(e) > endOf(best)) best = e;
    }
    return best;
  }, [prepared]);

  if (!last) return null; // no journeys in the window — the card simply doesn't exist

  const iv = intervalOf(last)!;
  const j = last.message.journey;
  const from = j?.origin?.label ?? null;
  const to = j?.destination?.label ?? null;
  const mode = j?.mode ?? (last.name === "car_trip" ? "driving" : null);
  const support = last.message.support?.level ?? null;
  const day = dayKey(last.date);
  const open = () => { setSelectedDay(day); navigate("/d/timeline"); };

  return (
    <section className="panel">
      <h3 className="hc-head">
        Last journey
        <button type="button" className="hc-go" onClick={open}>open that day →</button>
      </h3>
      <div className="psub">{day} · {fmtTime(new Date(startOf(last) * 1000))} → {fmtTime(new Date(endOf(last) * 1000))}</div>
      <div className="hc-route">
        <span className="hc-dot" aria-hidden="true" />
        <div className="hc-end">
          <div className="hc-place">{from ?? "somewhere"}</div>
          <div className="hc-meta">left {fmtTime(new Date(startOf(last) * 1000))}</div>
        </div>
        <div className="hc-line" aria-hidden="true" />
        <div className="hc-end right">
          <div className="hc-place">{to ?? "somewhere"}</div>
          <div className="hc-meta">arrived {fmtTime(new Date(endOf(last) * 1000))}</div>
        </div>
        <span className="hc-dot dest" aria-hidden="true" />
      </div>
      <div className="hc-chips">
        {mode && <span className="hc-chip">{mode} · {humanDur(iv.duration_seconds)}</span>}
        {!mode && <span className="hc-chip">{humanDur(iv.duration_seconds)}</span>}
        {support && <span className={"hc-chip" + (support === "corroborated" ? " good" : "")}>{support.replace("_", " ")}</span>}
        {carCorroborated(last) && <span className="hc-chip veh">your car</span>}
      </div>
    </section>
  );
}
