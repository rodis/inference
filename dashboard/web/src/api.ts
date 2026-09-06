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

// --- naming a place ---------------------------------------------------------------
// A stay always knows *where* it happened and only sometimes *what* that place is, because
// the label is a lookup against the POI registry (ADR 0007). Naming one adds the missing
// reference-data row — it does not correct the geometry, and it does not relabel the event:
// labels are stamped at derive time, so the stay you just named keeps reading "Stay" until
// it is re-derived. `PlaceSaved.labels_from_now` is the server saying exactly that.

/** One geocoder suggestion, measured from the stay's own centroid. */
export interface PlaceCandidate {
  name: string;
  display_name: string;
  categories: string[];
  lat: number;
  lon: number;
  /** How far this hit sits from where your phone actually was — the column that settles
   *  "is this the one I was standing in?", which is all the list has to answer. */
  distance_m: number;
}

export interface PlaceSaved {
  ok: boolean;
  id: number;
  name: string;
  radius_m: number;
  categories: string[];
  labels_from_now: boolean;
}

export interface NewPlace {
  name: string;
  lat: number;
  lon: number;
  radius_m: number;
  categories: string[];
  everyday: boolean;
}

/** Candidate names near a point. Bounded server-side to ~500m around (lat, lon), so a hit
 *  from the next town cannot come back — picking one writes a permanent row. */
export function searchPlaces(q: string, lat: number, lon: number,
                             signal?: AbortSignal): Promise<PlaceCandidate[]> {
  const qs = new URLSearchParams({ q, lat: String(lat), lon: String(lon) });
  return fetch(`/api/places/search?${qs}`, { signal }).then((r) => {
    if (!r.ok) {
      throw new Error(r.status === 502 ? "the geocoder didn't answer" : `search → ${r.status}`);
    }
    return r.json() as Promise<PlaceCandidate[]>;
  });
}

/** Write the POI row. A 409 means the name is taken by a row at different coordinates —
 *  surfaced as its own message because the fix is a different name, not a retry. */
export async function createPlace(userId: string, place: NewPlace): Promise<PlaceSaved> {
  const r = await fetch(`/api/places?user_id=${encodeURIComponent(userId)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(place),
  });
  if (r.status === 409) throw new Error(`you already have a place called “${place.name}”`);
  if (!r.ok) throw new Error(`save place → ${r.status}`);
  return r.json() as Promise<PlaceSaved>;
}
