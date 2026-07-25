export type Theme = "system" | "light" | "dark";

const KEY = "aware:theme";
export const THEMES: Theme[] = ["system", "light", "dark"];

export function readTheme(): Theme {
  const v = localStorage.getItem(KEY);
  return v === "light" || v === "dark" ? v : "system";
}

/** "system" removes the attribute so the `prefers-color-scheme` media query decides; an
 *  explicit choice stamps `data-theme`, which styles.css redefines the palette under and
 *  therefore wins over the media query in both directions. */
export function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  if (theme === "system") {
    root.removeAttribute("data-theme");
    localStorage.removeItem(KEY);
  } else {
    root.setAttribute("data-theme", theme);
    localStorage.setItem(KEY, theme);
  }
}

/** Run before React mounts, so an explicit override doesn't flash the other palette. */
export function bootTheme(): void {
  applyTheme(readTheme());
}
