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

export function fetchEvents(userId: string): Promise<AwareEvent[]> {
  return getJSON<AwareEvent[]>(`/api/events?user_id=${encodeURIComponent(userId)}`);
}

export function fetchPreferences(userId: string): Promise<Preferences> {
  return getJSON<Preferences>(`/api/preferences?user_id=${encodeURIComponent(userId)}`);
}

export async function savePreferences(userId: string, prefs: Preferences): Promise<void> {
  const r = await fetch(`/api/preferences?user_id=${encodeURIComponent(userId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(prefs),
  });
  if (!r.ok) throw new Error(`save preferences → ${r.status}`);
}
