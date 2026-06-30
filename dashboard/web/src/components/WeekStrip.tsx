import { useAware } from "../app/useAware";
import { catOf, dayKey } from "../view";

const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/** The horizontal day selector, shared by day-based dashboards. Reads the day list and the
 *  shared selected day from context, so switching dashboards keeps you on the same day. */
export default function WeekStrip() {
  const { prepared, selectedDay, setSelectedDay } = useAware();
  const { all, days } = prepared;
  return (
    <div className="weekstrip">
      {days.map((dk) => {
        const d = new Date(dk + "T00:00:00");
        const cats = [...new Set(all.filter((e) => dayKey(e.date) === dk).map((e) => catOf(e.name).c))].slice(0, 4);
        return (
          <button key={dk} className={"daycell" + (dk === selectedDay ? " sel" : "")} onClick={() => setSelectedDay(dk)}>
            <div className="dow">{DOW[d.getDay()]}</div>
            <div className="dn">{d.getDate()}</div>
            <div className="dots">{cats.map((c, i) => <i key={i} style={{ background: c }} />)}</div>
          </button>
        );
      })}
    </div>
  );
}
