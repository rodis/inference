import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { House, Monitor, Moon, Search, Sun } from "lucide-react";
import AwareMark from "../components/AwareMark";
import Palette from "../components/Palette";
import { MODULES, sidebarSections } from "./registry";
import type { Scope } from "./registry";
import { useAware } from "./useAware";
import { applyTheme, readTheme, THEMES } from "./theme";
import type { Theme } from "./theme";

const THEME_ICON = { system: Monitor, light: Sun, dark: Moon };
const THEME_LABEL: Record<Theme, string> = {
  system: "matching your system", light: "light", dark: "dark",
};

/** Cycles system → light → dark. "System" is the default and the honest one: the palette
 *  follows `prefers-color-scheme` until you overrule it. */
function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(readTheme);
  const Icon = THEME_ICON[theme];
  const next = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  return (
    <button type="button" className="themebtn"
      aria-label={`Theme: ${THEME_LABEL[theme]}. Switch to ${THEME_LABEL[next]}.`}
      title={`Theme: ${THEME_LABEL[theme]}`}
      onClick={() => { applyTheme(next); setTheme(next); }}>
      <Icon size={15} strokeWidth={2.25} />
    </button>
  );
}

const SCOPES: Scope[] = ["day", "week", "month"];

/** The shared period control. Rendered only while the active module declares `scopes`
 *  (registry.tsx) — the day timeline owns its own week strip and never sees this. */
function ScopeSeg({ allowed }: { allowed: Scope[] }) {
  const ctx = useAware();
  const scope = ctx.scope ?? "week";
  return (
    <span className="seg" role="group" aria-label="period">
      {SCOPES.filter((s) => allowed.includes(s)).map((s) => (
        <button key={s} type="button" aria-pressed={scope === s} onClick={() => ctx.setScope?.(s)}>
          {s[0].toUpperCase() + s.slice(1)}
        </button>
      ))}
    </span>
  );
}

/** The portal frame. It knows three things — sections, modules (both registry data) and
 *  the shared context in the top bar — and never what a module renders or fetches. The
 *  sidebar is derived the same way the old top nav was; ghost entries are the sections'
 *  declared direction. Adding a module or a whole section never edits this file. */
export default function Shell() {
  const { users, userId, setUserId, saved } = useAware();
  const location = useLocation();
  const active = MODULES.find((m) => location.pathname === `/d/${m.slug}`);
  const [palOpen, setPalOpen] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPalOpen((v) => !v);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="portal">
      <aside className="side">
        <div className="appbar">
          <div className="applogo"><AwareMark size={22} /></div>
          <span className="appname">Aware</span>
        </div>
        <nav className="snav" aria-label="sections">
          <NavLink to="/" end className={({ isActive }) => "sn" + (isActive ? " on" : "")}>
            <House size={15} strokeWidth={2.25} />Home
          </NavLink>
          {sidebarSections().map(({ section, modules, planned }) => (
            <div key={section.id} className="sgroup">
              <div className="sect">{section.title}</div>
              {modules.map((m) => (
                <NavLink key={m.slug} to={`/d/${m.slug}`}
                  className={({ isActive }) => "sn" + (isActive ? " on" : "")}>
                  {m.Icon && <m.Icon size={15} strokeWidth={2.25} />}{m.title}
                </NavLink>
              ))}
              {planned.map((p) => (
                <span key={p.title} className="sn planned" title="planned — not built yet">
                  {p.Icon && <p.Icon size={15} strokeWidth={2.25} />}{p.title}
                  <i className="soon">soon</i>
                </span>
              ))}
            </div>
          ))}
        </nav>
      </aside>

      <div className="maincol">
        <header className="tbar">
          <span className="crumb"><span className="dim">Aware / </span>{active ? active.title : "Home"}</span>
          {active?.scopes?.length ? <ScopeSeg allowed={active.scopes} /> : null}
          <button type="button" className="searchpill" onClick={() => setPalOpen(true)}
            aria-haspopup="dialog" aria-expanded={palOpen}>
            <Search size={12} strokeWidth={2.5} />
            Jump to… <kbd>⌘K</kbd>
          </button>
          <span className={"saveflag" + (saved ? " show" : "")}>saved ✓</span>
          <ThemeToggle />
          {users.length > 0 && (
            <span className="userselect">
              <label htmlFor="usersel">user</label>
              <select id="usersel" value={userId} onChange={(e) => setUserId(e.target.value)}>
                {users.map((u) => <option key={u} value={u}>{u}</option>)}
              </select>
            </span>
          )}
        </header>
        <div className="content"><Outlet /></div>
      </div>

      <Palette open={palOpen} onClose={() => setPalOpen(false)} />
    </div>
  );
}
