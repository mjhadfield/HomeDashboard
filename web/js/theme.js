// Themes: the built-in Dark and Light, plus custom themes (Settings → Appearance). Custom themes are stored on the
// server; which theme a device uses is that device's choice, kept in localStorage along with a ready-to-apply copy
// of its colours (theme-init.js applies that copy before first paint, so a custom theme never flashes dark first).
//
// A custom theme starts from Dark or Light (text colours and everything else) and overrides the accent, the panel
// colour and how see-through the panels are, and can add a background picture behind everything.

const KEY = 'theme';               // 'dark' | 'light' | 'c:<custom theme id>'
const SPEC_KEY = 'theme-spec';     // the chosen custom theme's computed CSS, for theme-init.js
const VAR_NAMES = ['--accent', '--accent-hi', '--accent-soft', '--accent-deep', '--accent-wash', '--edge-strong', '--surface', '--surface-2', '--surface-3',
  '--bg-image', '--bg-dim', '--bg-size', '--bg-repeat', '--bg-pos'];

export const BUILTIN = [{ id: 'dark', name: 'Dark', base: 'dark' }, { id: 'light', name: 'Light', base: 'light' }];

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch { /* private window: this device just won't remember */ } },
};

const mix = (a, pct, b) => `color-mix(in oklab, ${a} ${pct}%, ${b})`;
const see = (c, pct) => (pct >= 100 ? c : `color-mix(in srgb, ${c} ${pct}%, transparent)`);
const FIT = { cover: ['cover', 'no-repeat', 'center'], contain: ['contain', 'no-repeat', 'center'], tile: ['auto', 'repeat', 'top left'], center: ['auto', 'no-repeat', 'center'] };

/** A custom theme -> { base, vars, bg, glow } ready to apply. */
export function specOf(t) {
  const dark = t.base !== 'light', a = t.accent, p = t.panel, o = t.panel_opacity ?? 100;
  const vars = {
    '--accent': a,
    '--accent-hi': mix(a, 82, '#fff'),
    '--accent-soft': dark ? mix(a, 55, '#fff') : mix(a, 62, '#000'),        // text-safe: lighter on dark, darker on light
    '--accent-deep': mix(a, 52, '#000'),
    '--accent-wash': `color-mix(in srgb, ${a} 10%, transparent)`,
    '--edge-strong': `color-mix(in srgb, ${a} 45%, transparent)`,
    '--surface': see(p, o),
    '--surface-2': see(mix(p, 93, dark ? '#fff' : '#000'), o),
    '--surface-3': see(mix(p, 86, dark ? '#fff' : '#000'), o),
  };
  const bg = t.background || {};
  if (bg.file) {
    const [size, repeat, pos] = FIT[bg.fit] || FIT.cover;
    Object.assign(vars, { '--bg-image': `url("/backgrounds/${bg.file}")`, '--bg-dim': `${bg.dim ?? 40}%`, '--bg-size': size, '--bg-repeat': repeat, '--bg-pos': pos });
  }
  return { base: dark ? 'dark' : 'light', vars, bg: !!bg.file, glow: bg.glow !== false };
}

/** Put a spec (or null for a built-in) on the page. Exported for the editor's live preview. */
export function applySpec(spec, base = spec?.base || 'dark') {
  const root = document.documentElement;
  if (base === 'light') root.dataset.theme = 'light'; else delete root.dataset.theme;
  VAR_NAMES.forEach(n => root.style.removeProperty(n));
  root.classList.remove('has-bg', 'no-glow');
  if (!spec) return;
  Object.entries(spec.vars).forEach(([k, v]) => root.style.setProperty(k, v));
  root.classList.toggle('has-bg', spec.bg);
  root.classList.toggle('no-glow', !spec.glow);
}

/** This device's chosen theme id ('dark', 'light' or a custom theme's id). */
export function chosen() {
  const v = store.get(KEY);
  return v === 'light' ? 'light' : v?.startsWith('c:') ? v.slice(2) : 'dark';
}

/** Use a theme on this device. `themes` is the list of custom themes (from the server). */
export function useTheme(id, themes = []) {
  const t = themes.find(x => x.id === id);
  if (t) {
    const spec = specOf(t);
    store.set(KEY, `c:${id}`); store.set(SPEC_KEY, JSON.stringify(spec));
    applySpec(spec);
  } else {
    const base = id === 'light' ? 'light' : 'dark';
    store.set(KEY, base); store.set(SPEC_KEY, null);
    applySpec(null, base);
  }
  document.dispatchEvent(new CustomEvent('theme:change', { detail: { base: currentTheme() } }));
}

/** After the server's list arrives: re-apply this device's theme (it may have been edited elsewhere, or deleted). */
export function syncThemes(themes = []) {
  const id = chosen();
  if (id === 'dark' || id === 'light') return;
  const t = themes.find(x => x.id === id);
  const fresh = t ? JSON.stringify(specOf(t)) : null;
  if (fresh !== store.get(SPEC_KEY) || !t) useTheme(t ? id : 'dark', themes);
}

/** What an embedded page (Music Companion, Music DB Explorer) needs to look the same: the base mode, a custom
 * theme's colours (they use the same variable names), and whether Citadel is drawing a background picture behind
 * them — if so they make their own page backdrop see-through and the picture shows through the frame. */
export function currentLook() {
  let spec = null;
  if (chosen() !== 'dark' && chosen() !== 'light') { try { spec = JSON.parse(store.get(SPEC_KEY) || 'null'); } catch { /* none */ } }
  const vars = spec ? Object.fromEntries(Object.entries(spec.vars).filter(([k]) => !k.startsWith('--bg-'))) : null;
  return { type: 'citadel-theme', theme: currentTheme(), vars, bg: !!spec?.bg, glow: spec ? spec.glow : true };
}

/** 'dark' or 'light' — what a custom theme is built on (for embedded pages that only know those two). */
export function currentTheme() {
  return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
}

// The old one-click toggle, still used where there's no theme menu: flips between the built-in Dark and Light.
export function applyTheme(theme) { useTheme(theme === 'light' ? 'light' : 'dark'); }
export function initThemeToggle(btn) { btn.setAttribute('aria-pressed', String(currentTheme() === 'light')); }
export function toggleTheme(btn) { applyTheme(currentTheme() === 'dark' ? 'light' : 'dark'); initThemeToggle(btn); }
