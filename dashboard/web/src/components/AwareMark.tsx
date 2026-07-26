/** The Aware mark: four moments of one day, read left to right, arriving from barely-there to
 *  solid.
 *
 *  It replaced a `Car` glyph, which was a fossil of `car_trip` being the first derivation — any
 *  mark that names a *category* ages the same way, so this one names the act instead. What the app
 *  does is give you awareness of a day that already went by, mostly unnoticed: nothing in it
 *  happened differently because it was recorded, it just became legible afterwards. So the four
 *  dots are the same kind of thing at four strengths — growing in size *and* opacity together, the
 *  faintest barely present. Faint-vs-solid is the same channel the timeline uses for an unnamed
 *  stay's hollow capsule and for altitude, which is why the mark sits in that board without
 *  looking borrowed.
 *
 *  Drawn on lucide's 24×24 grid (the rest of the icon set) but fill-only — no strokes — so it
 *  keeps its weight when scaled down to a 16px favicon, where hairlines would drop out. It paints
 *  in `currentColor`, so the appbar's white-on-coral-gradient tile and a flat accent-ink version
 *  both come free with no variant here.
 *
 *  NOTE: the favicon in `index.html` is this same geometry inlined as a data URI (the FastAPI host
 *  only mounts /assets, so a `public/` file at the root would be swallowed by the SPA fallback).
 *  Change one, change the other. */
export default function AwareMark({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <circle cx="3.4" cy="12" r="1.1" opacity="0.26" />
      <circle cx="8" cy="12" r="1.6" opacity="0.48" />
      <circle cx="13.4" cy="12" r="2.3" opacity="0.74" />
      <circle cx="19.6" cy="12" r="3.2" />
    </svg>
  );
}
