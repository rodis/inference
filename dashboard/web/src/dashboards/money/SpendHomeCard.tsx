import { useNavigate } from "react-router-dom";
import { useAware } from "../../app/useAware";
import { useQuery } from "../../app/useQuery";
import { chf, spendUrl } from "./spend";
import type { SpendReport } from "./spend";

/** Home card: the week's spend as one number plus the biggest destination — a door into
 *  the Money board, never the analysis. Always the 7-day window regardless of the shared
 *  scope: Home is a glance, and the glance is "this week". */
export default function SpendHomeCard() {
  const { userId } = useAware();
  const navigate = useNavigate();
  const { data } = useQuery<SpendReport>(spendUrl(userId, "week"));

  if (!data || data.count === 0) return null; // nothing to say — the card doesn't exist

  const top = data.merchants[0];
  const delta = data.prev_total > 0 ? (data.total - data.prev_total) / data.prev_total : null;

  return (
    <section className="panel">
      <h3 className="hc-head">
        This week's spend
        <button type="button" className="hc-go" onClick={() => navigate("/d/spend")}>open money →</button>
      </h3>
      <div className="psub">{data.count} payments · last 7 days</div>
      <div className="mny-stat">
        <span className="big">{chf(data.total)}</span>
        {delta != null && (
          <span className={"delta" + (delta <= 0 ? " down" : "")}>
            {delta <= 0 ? "−" : "+"}{Math.abs(delta * 100).toFixed(0)}% vs last week
          </span>
        )}
      </div>
      {top && (
        <div className="hc-chips">
          <span className="hc-chip">largest — {top.label} · {top.total.toFixed(2)}</span>
        </div>
      )}
    </section>
  );
}
