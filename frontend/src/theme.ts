export type Theme = "light" | "dark";
const KEY = "mgf.theme";

export function resolveInitial(): Theme {
  const stored = localStorage.getItem(KEY) as Theme | null;
  if (stored === "light" || stored === "dark") return stored;
  return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

export function applyTheme(t: Theme): void {
  document.documentElement.setAttribute("data-theme", t);
  document.documentElement.style.colorScheme = t;
}

export function persistTheme(t: Theme): void {
  localStorage.setItem(KEY, t);
}
