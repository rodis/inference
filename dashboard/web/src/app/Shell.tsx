import { NavLink, Outlet } from "react-router-dom";
import { DASHBOARDS } from "./registry";
import { useAware } from "./useAware";

/** Persistent app frame: brand, registry-driven nav, global controls (user + save state),
 *  and the routed dashboard in the outlet. */
export default function Shell() {
  const { users, userId, setUserId, saved } = useAware();
  return (
    <div className="wrap">
      <header className="appbar">
        <div className="applogo">🚗</div>
        <span className="appname">Aware</span>
        <nav className="topnav">
          {DASHBOARDS.map((d) => (
            <NavLink key={d.slug} to={`/d/${d.slug}`} className={({ isActive }) => "navlink" + (isActive ? " on" : "")}>
              {d.icon && <span className="ni">{d.icon}</span>}{d.title}
            </NavLink>
          ))}
        </nav>
        <span className={"saveflag" + (saved ? " show" : "")}>saved ✓</span>
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
