"use strict";
// Theme: system (default), light or dark, kept in localStorage. Loaded without
// defer in <head> so data-theme is set before the first paint. With no script
// the CSS light-dark() tokens follow the system on their own.
(() => {
  const KEY = "txboards-theme", MODES = ["system", "light", "dark"];
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  const stored = () => { try { const v = localStorage.getItem(KEY); return MODES.includes(v) ? v : "system"; } catch { return "system"; } };
  let mode = stored();
  const apply = () => {
    const dark = mode === "dark" || (mode === "system" && media.matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    for (const m of document.querySelectorAll('meta[name="theme-color"]')) m.setAttribute("content", dark ? "#1e1d1a" : "#f6f4ee");
    const btn = document.querySelector("button.theme");
    if (btn) {
      const next = MODES[(MODES.indexOf(mode) + 1) % MODES.length];
      btn.dataset.mode = mode;
      btn.querySelector("span").textContent = mode[0].toUpperCase() + mode.slice(1);
      btn.setAttribute("aria-label", `Theme: ${mode}${mode === "system" ? ` (${dark ? "dark" : "light"})` : ""}. Switch to ${next}.`);
    }
  };
  apply();
  media.addEventListener("change", () => { if (mode === "system") apply(); });
  window.addEventListener("storage", e => { if (e.key === KEY) { mode = stored(); apply(); } });
  document.addEventListener("DOMContentLoaded", () => {
    const btn = document.querySelector("button.theme");
    if (!btn) return;
    btn.hidden = false;
    btn.addEventListener("click", () => {
      mode = MODES[(MODES.indexOf(mode) + 1) % MODES.length];
      try { localStorage.setItem(KEY, mode); } catch { /* private mode: the choice lasts for this page only */ }
      // A theme change is instant: hover and press transitions would otherwise fade every control.
      const root = document.documentElement;
      root.classList.add("theme-switching");
      apply();
      requestAnimationFrame(() => requestAnimationFrame(() => root.classList.remove("theme-switching")));
    });
    apply();
  });
})();
