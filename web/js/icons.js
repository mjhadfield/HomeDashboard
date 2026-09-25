// Inline SVG sprite. Use: <svg class="i"><use href="#i-bin"/></svg>
// (Mockup helper; the real build serves the same sprite from one shared module.)
const paths = {
  logo:   '<path d="M12 2l9 5v10l-9 5-9-5V7z"/><path d="M12 7l4 2.5v5L12 17l-4-2.5v-5z"/>',
  home:   '<path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
  grid:   '<path d="M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z"/>',
  disc:   '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.5"/><path d="M6 12a6 6 0 0 1 6-6"/>',
  bolt:   '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
  news:   '<path d="M4 5h13v14H6a2 2 0 0 1-2-2zM17 9h3v8a2 2 0 0 1-2 2M8 9h6M8 13h6"/>',
  bin:    '<path d="M5 7h14M9 7V4h6v3M7 7l1 13h8l1-13M10 11v6M14 11v6"/>',
  play:   '<path d="M7 4l13 8-13 8z"/>',
  stop:   '<path d="M6 6h12v12H6z"/>',
  ext:    '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  film:   '<path d="M4 5h16v14H4zM8 5v14M16 5v14M4 9h4M4 15h4M16 9h4M16 15h4"/>',
  sun:    '<path d="M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1"/>',
  gear:   '<path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
  pin:    '<path d="M12 17v5M8 3h8l-1 6 3 3H6l3-3z"/>',
  x:      '<path d="M6 6l12 12M18 6L6 18"/>',
  star:   '<path d="M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9z"/>',
  alert:  '<path d="M12 3l10 18H2zM12 10v5M12 18v.5"/>',
  server: '<path d="M4 5h16v6H4zM4 13h16v6H4zM8 8h.01M8 16h.01"/>',
  radar:  '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4.5"/><path d="M12 12l6-6"/>',
  globe:  '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  router: '<path d="M4 14h16v5H4zM8 14l-2-6M16 14l2-6M8 17h.01M12 17h.01"/>',
  chip:   '<path d="M8 8h8v8H8zM4 10h4M4 14h4M16 10h4M16 14h4M10 4v4M14 4v4M10 16v4M14 16v4"/>',
  cards:  '<path d="M6 3h9a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2zM9 8l3 4-3 4-3-4z"/>',
  term:   '<path d="M4 5h16v14H4zM7 10l3 2-3 2M12 15h5"/>',
  cal:    '<path d="M4 6h16v14H4zM4 10h16M8 3v4M16 3v4"/><path d="M8 14h.01M12 14h.01M16 14h.01M8 17h.01M12 17h.01"/>',
  expand: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
  shrink: '<path d="M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5"/>',
  up:     '<path d="M12 19V5M5 12l7-7 7 7"/>',
  down:   '<path d="M12 5v14M5 12l7 7 7-7"/>',
  mute:   '<path d="M11 5L6 9H2v6h4l5 4zM22 9l-6 6M16 9l6 6"/>',
  heart:  '<path d="M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.5A4 4 0 0 1 19 10c0 5.6-7 10-7 10z"/>',
  check:  '<path d="M4 12.5l5 5L20 6"/>',
  plus:   '<path d="M12 5v14M5 12h14"/>',
  bag:    '<path d="M6 8h12l1 12a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2zM9 8V6a3 3 0 0 1 6 0v2"/>',
};

const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
svg.setAttribute('width', '0'); svg.setAttribute('height', '0');
svg.setAttribute('aria-hidden', 'true'); svg.style.position = 'absolute';
svg.innerHTML = Object.entries(paths)
  .map(([k, d]) => `<symbol id="i-${k}" viewBox="0 0 24 24">${d}</symbol>`).join('');
document.body.prepend(svg);
