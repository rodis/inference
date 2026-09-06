import { useCallback, useEffect, useRef, useState } from "react";
import { createPlace, searchPlaces, type PlaceCandidate } from "../api";
import { useAware } from "../app/useAware";
import type { AwareEvent } from "../types";
import { PLACE_ICON, placeIcon } from "../view";

/** Naming a stay — the panel behind `placeUnknown`'s hollow capsule.
 *
 *  `view.ts` already draws a stay we couldn't name as an unfilled ring and says why: the
 *  inference is not less *true* (you did stop here for 40 minutes), it is less *resolved*, and
 *  the weaker drawing "reads as *not named yet*, which is an invitation, not an error". This is
 *  that invitation becoming clickable.
 *
 *  **What it writes is reference data, not a label.** A place is a `regions` POI row (ADR 0007);
 *  the label on an event is a lookup against that book, frozen at derive time. So saving here
 *  teaches the runtime the place — every future stay in that spot names itself within the place
 *  book's TTL — and changes nothing about the stay in front of you. Saying so is the panel's
 *  job; see `SavedNote`. Backfilling the past is a re-derive, deliberately not a button.
 *
 *  **The geocoder suggests, the stay decides where.** Candidates contribute a name and
 *  categories; the row is written at the stay's own centroid, because that is the point we
 *  measured while OSM's is wherever a building polygon's centre falls. So the search box is
 *  free text — a shop name or a street address, whichever you remember.
 */

const DEBOUNCE_MS = 400;     // one person typing; the server throttles Nominatim to 1/s anyway
const MIN_QUERY = 2;

/** Metres, the way you'd say them: a POI's neighbourhood is tens of metres, so no decimals. */
const metres = (m: number) => `${Math.round(m)}m`;

/** The category chips on a candidate row — the glyph vocabulary `PLACE_ICON` can draw, plus
 *  the bare word for anything it can't yet. An unmapped category is shown rather than hidden:
 *  it is what will be stored, and it is how you notice the vocabulary needs a new glyph. */
function Categories({ categories }: { categories: string[] }) {
  if (!categories.length) return null;
  return (
    <span className="np-cats">
      {categories.map((c) => (
        <span key={c} className={"np-cat" + (PLACE_ICON[c] ? "" : " np-cat-plain")}>{c}</span>
      ))}
    </span>
  );
}

/** The confirmation. It states the two halves separately because they are genuinely different
 *  facts, and collapsing them into "Saved!" would let the UI imply a relabelling that did not
 *  happen — the stay on the board behind this panel is still drawn hollow, and will be until
 *  it is re-derived. */
function SavedNote({ name, radius }: { name: string; radius: number }) {
  return (
    <div className="np-saved">
      <div className="np-saved-h">Saved — “{name}”, {metres(radius)} across.</div>
      <div className="np-saved-b">
        Stays here from now on will carry the name. The ones already recorded keep the label
        they were minted with until they're re-derived.
      </div>
    </div>
  );
}

export default function NamePlace({ event }: { event: AwareEvent }) {
  const { userId, client } = useAware();
  const place = event.message.place;

  const [q, setQ] = useState("");
  const [hits, setHits] = useState<PlaceCandidate[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState("");
  // The picked candidate, editable before saving — `null` until one is chosen, which is also
  // what keeps the form off screen while you're still looking.
  const [name, setName] = useState("");
  const [categories, setCategories] = useState<string[]>([]);
  const [radius, setRadius] = useState(0);
  const [everyday, setEveryday] = useState(false);
  const [picked, setPicked] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [saved, setSaved] = useState<{ name: string; radius: number } | null>(null);

  const lat = place?.lat, lon = place?.lon;
  // The stay's own scatter is the best guess at how big the place is; clamped server-side to
  // the band the hand-made rows use, and mirrored here so the field opens on the same number.
  const defaultRadius = Math.round(Math.min(Math.max(place?.spread_m ?? 50, 50), 80));

  useEffect(() => { setRadius((r) => (r ? r : defaultRadius)); }, [defaultRadius]);

  // One in-flight search at a time: a superseded query is aborted rather than left to land
  // after the one you actually want, which is the classic autocomplete flicker.
  const inflight = useRef<AbortController | null>(null);
  useEffect(() => {
    const query = q.trim();
    if (query.length < MIN_QUERY || lat === undefined || lon === undefined) {
      setHits([]); setSearching(false); setSearchError("");
      return;
    }
    const timer = setTimeout(() => {
      inflight.current?.abort();
      const ctl = new AbortController();
      inflight.current = ctl;
      setSearching(true); setSearchError("");
      searchPlaces(query, lat, lon, ctl.signal).then(
        (found) => { if (!ctl.signal.aborted) { setHits(found); setSearching(false); } },
        (e: Error) => {
          if (ctl.signal.aborted || e.name === "AbortError") return;
          setHits([]); setSearching(false); setSearchError(e.message);
        },
      );
    }, DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [q, lat, lon]);

  useEffect(() => () => inflight.current?.abort(), []);

  const pick = useCallback((c: PlaceCandidate) => {
    setName(c.name);
    setCategories(c.categories);
    setPicked(true);
    setSaveError("");
  }, []);

  /** Name it exactly as typed, with no categories. The escape hatch for a place OSM has never
   *  heard of — a friend's flat, an office, a car park — which is a normal thing to stop at
   *  and would otherwise be unnameable through a search box. */
  const useTyped = useCallback(() => {
    setName(q.trim());
    setCategories([]);
    setPicked(true);
    setSaveError("");
  }, [q]);

  async function save() {
    if (lat === undefined || lon === undefined) return;
    setSaving(true); setSaveError("");
    try {
      const body = await createPlace(userId, {
        name: name.trim(), lat, lon, radius_m: radius, categories, everyday,
      });
      setSaved({ name: body.name, radius: body.radius_m });
      // So a Places view (and this panel, on the next stay) reads the book including this row.
      client?.invalidate("/api/places");
    } catch (e) {
      setSaveError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  // Geometry is what makes this stay nameable at all: no centroid, nothing to attach a row to.
  // Shouldn't happen (the caller gates on `placeUnknown`, which requires a place) but the panel
  // must not offer a form that can only fail.
  if (lat === undefined || lon === undefined) return null;
  if (saved) return <div className="np">{<SavedNote name={saved.name} radius={saved.radius} />}</div>;

  const PickedIcon = placeIcon(categories);
  const canSave = name.trim().length > 0 && !saving;

  return (
    <div className="np">
      <div className="np-head">
        <span className="np-title">Name this place</span>
        <span className="np-coords" title="the centroid of this stay's own fixes — where the row will be written">
          {lat.toFixed(5)}, {lon.toFixed(5)}
          {place?.spread_m ? ` · ${metres(place.spread_m)} spread` : ""}
        </span>
      </div>

      <input
        className="np-q"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Shop name or address…"
        aria-label="Search for this place by name or address"
        autoComplete="off"
      />

      {searching && <div className="np-note">searching…</div>}
      {searchError && <div className="np-err">{searchError}</div>}

      {/* The list goes away once you've picked. Leaving it up pushed the confirm form below
          the fold of an already-scrolling modal, so clicking a candidate looked like it did
          nothing at all — the one interaction the panel cannot afford to make ambiguous.
          `back to search` brings it back with the query intact. */}
      {!picked && !searching && !searchError && q.trim().length >= MIN_QUERY && (
        <div className="np-hits">
          {hits.map((c) => {
            const Icon = placeIcon(c.categories);
            return (
              <button key={`${c.name}-${c.lat}-${c.lon}`} className="np-hit" onClick={() => pick(c)}>
                <span className="np-hit-icon">{Icon ? <Icon size={15} strokeWidth={2.25} /> : "·"}</span>
                <span className="np-hit-main">
                  <span className="np-hit-name">{c.name}</span>
                  <span className="np-hit-sub">{c.display_name}</span>
                </span>
                <Categories categories={c.categories} />
                <span className="np-hit-d" title="from where your phone actually was">
                  {metres(c.distance_m)}
                </span>
              </button>
            );
          })}
          <button className="np-hit np-hit-typed" onClick={useTyped}>
            <span className="np-hit-icon">+</span>
            <span className="np-hit-main">
              <span className="np-hit-name">Just call it “{q.trim()}”</span>
              <span className="np-hit-sub">
                {hits.length ? "none of these" : "nothing found here"} — save the name as typed
              </span>
            </span>
          </button>
        </div>
      )}

      {picked && (
        <div className="np-form">
          <label className="np-field">
            <span>Name</span>
            <input value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <label className="np-field np-field-r">
            <span>Radius</span>
            <input
              type="number" min={30} max={150} step={5}
              value={radius}
              onChange={(e) => setRadius(Number(e.target.value))}
            />
            <span className="np-unit">m</span>
          </label>
          <label className="np-check" title="the place you live in — kept off the timeline, because home dwell has no natural boundaries (ADR 0007)">
            <input type="checkbox" checked={everyday} onChange={(e) => setEveryday(e.target.checked)} />
            <span>Somewhere I live</span>
          </label>
          <div className="np-preview">
            {PickedIcon ? <PickedIcon size={15} strokeWidth={2.25} /> : null}
            <Categories categories={categories} />
          </div>
          {saveError && <div className="np-err">{saveError}</div>}
          <div className="np-actions">
            <button className="np-cancel" onClick={() => setPicked(false)} disabled={saving}>
              back to search
            </button>
            <button className="np-save" onClick={save} disabled={!canSave}>
              {saving ? "saving…" : "Save place"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
