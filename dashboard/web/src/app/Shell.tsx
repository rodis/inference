import { useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { Monitor, Moon, Sun } from "lucide-react";
import AwareMark from "../components/AwareMark";
import { DASHBOARDS } from "./registry";
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

/** Persistent app frame: brand, registry-driven nav, global controls (user + save state),
 *  and the routed dashboard in the outlet. */
export default function Shell() {
  const { users, userId, setUserId, saved } = useAware();
  return (
    <div className="wrap">
      <header className="appbar">
        <div className="applogo"><AwareMark size={22} /></div>
        <span className="appname">Aware</span>
        <nav className="topnav">
          {DASHBOARDS.map((d) => (
            <NavLink key={d.slug} to={`/d/${d.slug}`} className={({ isActive }) => "navlink" + (isActive ? " on" : "")}>
              {d.Icon && <d.Icon size={15} strokeWidth={2.25} className="ni" />}{d.title}
            </NavLink>
          ))}
        </nav>
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
      <Outlet />
    </div>
  );
}
