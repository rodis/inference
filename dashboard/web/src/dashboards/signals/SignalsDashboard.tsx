import { useMemo } from "react";
import { useAware } from "../../app/useAware";
import { catOf, dayKey, fmtTimeSec, typeLabel } from "../../view";
import WeekStrip from "../../components/WeekStrip";

/** A deliberately small second dashboard — a reverse-chronological log of raw signals.
 *  It exists mainly to prove the registry seam: this file + one line in registry.tsx was
 *  the entire cost of adding a dashboard. Reuses the same shared data via useAware().
 *
 *  Scoped to the *selected day* (shared with the timeline via context, so switching
 *  dashboards keeps you on the same day). A high-rate source like the location tracker
 *  makes an all-time log unreadable, so the day is the unit here too. */
export default function SignalsDashboard() {
  const { prepared, status, userId, selectedDay } = useAware();

  const signals = useMemo(
    () => prepared.raw.filter((e) => dayKey(e.date) === selectedDay).sort((a, b) => b.epoch - a.epoch),
    [prepared, selectedDay],
  );

  if (status) return <div className="statusline">{status}</div>;

  return (
    <>
      <div className="datehead">Signals <span className="chev">›</span></div>
      <p className="page-intro">Raw signals for <b>{userId}</b> on the selected day, newest first — the ground truth the inferences are built from.</p>
      <WeekStrip />
      <div className="card-box">
        {signals.length === 0 ? (
          <div className="vt-empty">— no raw signals this day —</div>
        ) : (
          <table className="sigtable">
            <thead>
              <tr><th>Time</th><th>Signal</th><th>Source</th></tr>
            </thead>
            <tbody>
              {signals.map((e) => {
                const cat = catOf(e.name);
                return (
                  <tr key={e.id}>
                    <td className="t">{fmtTimeSec(e.date)}</td>
                    <td><span className="dot" style={{ background: cat.c }} />{typeLabel(e.name)}</td>
                    <td className="src">{e.source_app || "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
