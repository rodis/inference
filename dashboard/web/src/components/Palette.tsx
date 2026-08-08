import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Calendar, MapPin } from "lucide-react";
import { MODULES, SECTIONS } from "../app/registry";
import type { PaletteItem } from "../app/registry";
import { useAware } from "../app/useAware";
import { catOf, dayKey, isSpan, labelOf, placeUnknown } from "../view";

const DOW = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const MON = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const fmtDay = (d: string) => {
  const dt = new Date(d + "T00:00:00");
  return `${DOW[dt.getDay()]} ${dt.getDate()} ${MON[dt.getMonth()]}`;
};

/** ⌘K. Results are computed, never hand-registered: modules from the registry, days and
 *  places from the loaded window, plus whatever items each module's own `palette` provider
 *  returns. Selecting a day or place drives the same shared `selectedDay` seam the week
 *  strip uses — the palette is navigation-as-search over the frame's existing context. */
export default function Palette({ open, onClose }: { open: boolean; onClose: () => void }) {
  const ctx = useAware();
  const { prepared, setSelectedDay } = ctx;
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [sel, setSel] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) { setQuery(""); setSel(0); setTimeout(() => inputRef.current?.focus(), 0); }
  }, [open]);

  const items = useMemo<PaletteItem[]>(() => {
    if (!open) return [];
    const sectionTitle = (id: string) => SECTIONS.find((s) => s.id === id)?.title ?? id;

    const modules: PaletteItem[] = MODULES.map((m) => ({
      key: `mod:${m.slug}`, group: "Dashboards", label: m.title,
      detail: `${sectionTitle(m.section)} · /d/${m.slug}`, Icon: m.Icon,
      run: (nav) => nav(`/d/${m.slug}`),
    }));

    // Days, newest first, with what each held — counts over the same span set the boards draw.
    const days: PaletteItem[] = [...prepared.days].reverse().map((d) => {
      const spans = prepared.all.filter((e) => dayKey(e.date) === d && isSpan(e));
      const stays = spans.filter((e) => !!e.message.place).length;
      const journeys = spans.length - stays;
      return {
        key: `day:${d}`, group: "Days", label: fmtDay(d),
        detail: `${stays} stays · ${journeys} journeys`, Icon: Calendar,
        run: (nav) => { setSelectedDay(d); nav("/d/timeline"); },
      };
    });

    // Labelled places in the window; opening one lands on its most recent day.
    const byPlace = new Map<string, { count: number; lastDay: string }>();
    for (const e of prepared.all) {
      if (!isSpan(e) || !e.message.place || placeUnknown(e)) continue;
      const label = labelOf(e);
      const d = dayKey(e.date);
      const cur = byPlace.get(label);
      if (cur) { cur.count += 1; if (d > cur.lastDay) cur.lastDay = d; }
      else byPlace.set(label, { count: 1, lastDay: d });
    }
    const places: PaletteItem[] = [...byPlace.entries()].map(([label, v]) => ({
      key: `place:${label}`, group: "Places", label,
      detail: `${v.count} ${v.count === 1 ? "stay" : "stays"} · last ${v.lastDay}`,
      color: catOf("stay").c, Icon: MapPin,
      run: (nav) => { setSelectedDay(v.lastDay); nav("/d/timeline"); },
    }));

    const fromModules = MODULES.flatMap((m) => m.palette?.(query) ?? []);
    return [...modules, ...days, ...places, ...fromModules];
  }, [open, prepared, setSelectedDay, query]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    const match = q
      ? items.filter((i) => (i.label + " " + (i.detail ?? "")).toLowerCase().includes(q))
      : items;
    return match.slice(0, 12);
  }, [items, query]);

  useEffect(() => { setSel((s) => Math.min(s, Math.max(0, shown.length - 1))); }, [shown]);

  if (!open) return null;

  const run = (item: PaletteItem) => { onClose(); item.run((to) => navigate(to)); };
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setSel((s) => Math.min(s + 1, shown.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setSel((s) => Math.max(s - 1, 0)); }
    else if (e.key === "Enter" && shown[sel]) { e.preventDefault(); run(shown[sel]); }
    else if (e.key === "Escape") onClose();
  };

  let lastGroup = "";
  return (
    <div className="ov show pal-ov" role="dialog" aria-modal="true" aria-label="Jump to"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="pal">
        <div className="palin">
          <input ref={inputRef} type="text" value={query} placeholder="Jump to a dashboard, a day, a place…"
            onChange={(e) => { setQuery(e.target.value); setSel(0); }} onKeyDown={onKey} />
          <kbd>esc</kbd>
        </div>
        <div className="pallist">
          {shown.length === 0 && <div className="palempty">nothing matches</div>}
          {shown.map((item, i) => {
            const head = item.group !== lastGroup ? item.group : null;
            lastGroup = item.group;
            const tile = item.color ?? "var(--accent)";
            return (
              <div key={item.key}>
                {head && <div className="palgroup">{head}</div>}
                <button type="button" className={"palrow" + (i === sel ? " sel" : "")}
                  onMouseEnter={() => setSel(i)} onClick={() => run(item)}>
                  <span className="pri" style={{ background: tile }} aria-hidden="true">
                    {item.Icon && <item.Icon size={13} strokeWidth={2.5} />}
                  </span>
                  <span className="prk">{item.label}</span>
                  {item.detail && <span className="prd">{item.detail}</span>}
                </button>
              </div>
            );
          })}
        </div>
        <div className="palfoot">
          <span><kbd>↑↓</kbd> navigate</span><span><kbd>↵</kbd> open</span>
          <span>results derive from the registry + loaded events</span>
        </div>
      </div>
    </div>
  );
}
