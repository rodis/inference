import { useAware } from "../../app/useAware";
import { catOf, fmtTimeSec, typeLabel } from "../../view";

/** A deliberately small second dashboard — a reverse-chronological log of raw signals.
 *  It exists mainly to prove the registry seam: this file + one line in registry.tsx was
 *  the entire cost of adding a dashboard. Reuses the same shared data via useAware(). */
export default function SignalsDashboard() {
  const { prepared, status, userId } = useAware();
  if (status) return <div className="statusline">{status}</div>;

  const signals = [...prepared.raw].sort((a, b) => b.epoch - a.epoch);

  return (
    <>
      <div className="datehead">Signals <span className="chev">›</span></div>
      <p className="page-intro">Every raw signal for <b>{userId}</b>, newest first — the ground truth the inferences are built from.</p>
      <div className="card-box">
        {signals.length === 0 ? (
          <div className="vt-empty">— no raw signals —</div>
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
                    <td className="t">{e.date.toLocaleDateString()} {fmtTimeSec(e.date)}</td>
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
