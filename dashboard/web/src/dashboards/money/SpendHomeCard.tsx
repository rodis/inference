import { useNavigate } from "react-router-dom";
import { useAware } from "../../app/useAware";
import { useQuery } from "../../app/useQuery";
import { chf, spendUrl } from "./spend";
import type { SpendReport } from "./spend";

/** Home card: what today cost — Home is a today-glance, so the card matches the spine and
 *  strip beside it. days=1, so the delta compares against yesterday; the merchant chips
 *  name today's actual payments (there are rarely more than a few in a day). The week and
 *  month live behind the door, on the board with the period control. */
export default function SpendHomeCard() {
  const { userId } = useAware();
  const navigate = useNavigate();
  const { data } = useQuery<SpendReport>(spendUrl(userId, "day"));

  if (!data || data.count === 0) return null; // nothing spent today — the card doesn't exist

  const delta = data.prev_total > 0 ? (data.total - data.prev_total) / data.prev_total : null;

  return (
    <section className="panel">
      <h3 className="hc-head">
        Today's spend
        <button type="button" className="hc-go" onClick={() => navigate("/d/spend")}>open money →</button>
      </h3>
      <div className="psub">{data.count} {data.count === 1 ? "payment" : "payments"} today</div>
      <div className="mny-stat">
        <span className="big">{chf(data.total)}</span>
        {delta != null && (
          <span className={"delta" + (delta <= 0 ? " down" : "")}>
            {delta <= 0 ? "−" : "+"}{Math.abs(delta * 100).toFixed(0)}% vs yesterday
          </span>
        )}
      </div>
      {data.merchants.length > 0 && (
        <div className="hc-chips">
          {data.merchants.slice(0, 3).map((m) => (
            <span key={m.label} className="hc-chip">{m.label} · {m.total.toFixed(2)}</span>
          ))}
        </div>
      )}
    </section>
  );
}
