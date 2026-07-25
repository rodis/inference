import type { AwareEvent, Preferences } from "./types";

// Same-origin in prod (FastAPI serves the bundle); Vite proxies /api in dev.
async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} → ${r.status}`);
  return r.json() as Promise<T>;
}

export function fetchUsers(): Promise<string[]> {
  return getJSON<string[]>("/api/users");
}

/** How many trailing days of history the app loads — and therefore how many days the
 *  picker offers. The dashboards render one day at a time, so this is the whole working
 *  set; the server does the windowing (`/api/events?days=`) so the payload stays flat as
 *  history accrues instead of growing with every event ever recorded. */
export const DAY_WINDOW = 7;

export function fetchEvents(userId: string, days = DAY_WINDOW): Promise<AwareEvent[]> {
  return getJSON<AwareEvent[]>(
    `/api/events?user_id=${encodeURIComponent(userId)}&days=${days}`,
  );
}

/** Normalised on the way in: a rolling deploy can briefly pair a new bundle with the old
 *  `{levels, lift}` API (or the reverse), and a missing key would otherwise crash the app
 *  rather than degrade to "everything sits at its depth default". */
export async function fetchPreferences(userId: string): Promise<Preferences> {
  const raw = await getJSON<Partial<Preferences>>(
    `/api/preferences?user_id=${encodeURIComponent(userId)}`,
  );
  return {
    level: typeof raw?.level === "object" && raw.level ? raw.level : {},
    hidden: Array.isArray(raw?.hidden) ? raw.hidden : [],
  };
}

export async function savePreferences(userId: string, prefs: Preferences): Promise<void> {
  const r = await fetch(`/api/preferences?user_id=${encodeURIComponent(userId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(prefs),
  });
  if (!r.ok) throw new Error(`save preferences → ${r.status}`);
}
