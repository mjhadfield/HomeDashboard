const KEY = 'theme';

export function applyTheme(theme) {
  if (theme === 'light') document.documentElement.dataset.theme = 'light';
  else delete document.documentElement.dataset.theme;
}

export function currentTheme() {
  return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
}

function reflect(btn) {
  const light = currentTheme() === 'light';
  btn.setAttribute('aria-pressed', String(light));
  btn.setAttribute('aria-label', light ? 'Switch to dark theme' : 'Switch to light theme');
}

export function initThemeToggle(btn) { reflect(btn); }

export function toggleTheme(btn) {
  applyTheme(currentTheme() === 'dark' ? 'light' : 'dark');
  try { localStorage.setItem(KEY, currentTheme()); } catch {}
  reflect(btn);
}
