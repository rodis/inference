import { NavLink, Outlet } from "react-router-dom";
import { Car } from "lucide-react";
import { DASHBOARDS } from "./registry";
import { useAware } from "./useAware";

/** Persistent app frame: brand, registry-driven nav, global controls (user + save state),
 *  and the routed dashboard in the outlet. */
export default function Shell() {
  const { users, userId, setUserId, saved } = useAware();
  return (
    <div className="wrap">
      <header className="appbar">
        <div className="applogo"><Car size={22} strokeWidth={2.25} color="#fff" /></div>
        <span className="appname">Aware</span>
        <nav className="topnav">
          {DASHBOARDS.map((d) => (
            <NavLink key={d.slug} to={`/d/${d.slug}`} className={({ isActive }) => "navlink" + (isActive ? " on" : "")}>
              {d.Icon && <d.Icon size={15} strokeWidth={2.25} className="ni" />}{d.title}
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
