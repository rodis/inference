/** The lane an event type renders in, and — when it isn't where its derivation depth would
 *  have put it — which way the user moved it. Colours live in styles.css (`.lchip.l1…l4`)
 *  rather than in a JS map, so both themes are the stylesheet's problem; a ladder taller
 *  than the palette reuses the bottom swatch and still shows the true number. */
export default function LevelChip({ level, cap = 4 }: { level: number; cap?: number }) {
  return <span className={`lchip l${Math.min(level, cap)}`}>L{level}</span>;
}

/** `↑ L1` / `↓ L3` — an honest report that this type sits off its depth default. Renders
 *  nothing for a type that's where depth put it, or one with no depth to compare against. */
export function OverrideFlag({ level, def }: { level: number; def: number | null }) {
  if (def == null || level === def) return null;
  const up = level < def;
  return (
    <span className={"ovrflag" + (up ? " up" : " down")}
      title={`depth puts this at L${def} — you moved it ${up ? "up" : "down"}`}>
      {up ? "↑" : "↓"} L{level}
    </span>
  );
}
