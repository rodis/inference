import { MapPin } from "lucide-react";
import { useAware } from "../../app/useAware";
import { useQuery } from "../../app/useQuery";
import { catOf } from "../../view";
import { chf, SCOPE_DAYS, spendUrl } from "./spend";
import type { SpendReport } from "./spend";

const MONEY = catOf("credit_card_payment").c;
const PLACE = catOf("stay").c;
const DOW = ["S", "M", "T", "W", "T", "F", "S"];

/** The Money board (portal P2, #64): spend over the shared period, aggregated by its own
 *  backend route. One series, so no legend; the value label sits on the biggest day only
 *  and every bar carries its facts in a tooltip — the chart states the week, the merchant
 *  list explains it. */
export default function SpendDashboard() {
  const { userId, scope, status } = useAware();
  const { data, error } = useQuery<SpendReport>(spendUrl(userId, scope ?? "week"));

  if (status) return <div className="statusline">{status}</div>;
  if (error) return <div className="statusline">Spend query failed: {error}</div>;
  if (!data) return <div className="statusline">Loading…</div>;

  const days = SCOPE_DAYS[scope ?? "week"] ?? 7;
  const max = Math.max(...data.by_day.map((d) => d.total), 1);
  const maxDay = data.by_day.reduce((a, b) => (b.total > a.total ? b : a), data.by_day[0]);
  const today = new Date().toISOString().slice(0, 10);
  const delta = data.prev_total > 0 ? (data.total - data.prev_total) / data.prev_total : null;

  return (
    <>
      <div className="pagehead">
        <div className="eyebrow">Money</div>
        <h1 className="ptitle">Spend — last {days} days</h1>
      </div>

      <div className="mny-cols">
        <section className="panel">
          <h3>By day</h3>
          <div className="psub">credit_card_payment · trailing {days} whole days (UTC)</div>
          <div className="mny-stat">
            <span className="big">{chf(data.total)}</span>
            {delta != null && (
              <span className={"delta" + (delta <= 0 ? " down" : "")}>
                {delta <= 0 ? "−" : "+"}{Math.abs(delta * 100).toFixed(0)}% vs previous {days} days
              </span>
            )}
          </div>
          <div className="mny-chart" role="img"
            aria-label={`Spend per day: ${data.by_day.map((d) => `${d.day} ${d.total.toFixed(2)}`).join(", ")} francs`}>
            {data.by_day.map((d) => {
              const dt = new Date(d.day + "T00:00:00");
              const label = days <= 7 ? DOW[dt.getDay()] : (dt.getDay() === 1 ? String(dt.getDate()) : "");
              return (
                <div key={d.day} className={"mny-bar" + (d.day === today ? " today" : "")}>
                  {d.total === maxDay.total && d.total > 0 && (
                    <span className="bv">{d.total.toFixed(2)}</span>
                  )}
                  <div className="fill" style={{ height: `${Math.max(2, (d.total / max) * 100)}%`, background: MONEY }}
                    title={`${d.day} · ${chf(d.total)} · ${d.count} ${d.count === 1 ? "payment" : "payments"}`} />
                  <span className="bl">{label}</span>
                </div>
              );
            })}
          </div>
          <div className="psub" style={{ marginTop: 10 }}>
            {data.count} payments · {data.matched} matched a stay
          </div>
        </section>

        <section className="panel">
          <h3>Where it went</h3>
          <div className="psub">
            a payment inside a stay's interval inherits its place label — containment, not merchant-string parsing
          </div>
          {data.merchants.length === 0 && <div className="dt-empty">no payments in this window</div>}
          <div className="mny-list">
            {data.merchants.map((m) => (
              <div key={m.label} className="mny-row">
                <span className="ptile" style={{ background: m.placed ? PLACE : MONEY }} aria-hidden="true">
                  {m.placed ? <MapPin size={13} strokeWidth={2.5} /> : m.label.slice(0, 1).toUpperCase()}
                </span>
                <div style={{ minWidth: 0 }}>
                  <div className="pname" title={m.label}>{m.label}</div>
                  <div className="msub">
                    {m.count} {m.count === 1 ? "payment" : "payments"}{m.placed ? "" : " · unmatched merchant"}
                  </div>
                </div>
                <span className="mv">{m.total.toFixed(2)}</span>
              </div>
            ))}
          </div>
        </section>
      </div>

      <footer>
        <b>Money</b> — aggregated server-side (<b>/api/money/spend</b>, the module's own route), never
        from the shared events window. The merchant list groups by the <b>place label</b> the
        payment×stay containment join assigns — the same innermost-container rule the timeline's
        moments lane uses — falling back to the raw merchant string when no labelled stay contains
        the payment (a pay-at-pump fuel stop is the expected miss).
      </footer>
    </>
  );
}
