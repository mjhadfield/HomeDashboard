// Small helpers shared by the dashboard modules. Everything that reaches innerHTML goes through esc():
// calendar titles and locations are external content and must never be trusted as markup.

export const $ = (s, r = document) => r.querySelector(s);
export const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const ENT = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
export const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ENT[c]);

export const ico = (name, cls = '') => `<svg class="i ${esc(cls)}"><use href="#i-${esc(name)}"/></svg>`;

/** Only http(s) URLs may become an href (blocks javascript: and friends). */
export const safeUrl = u => (/^https?:\/\//i.test(u || '') ? u : '#');
export const hostUrl = tpl => safeUrl(String(tpl || '').replace('{host}', location.hostname));

const pad = n => String(n).padStart(2, '0');
export const isoOf = (y, m0, d) => `${y}-${pad(m0 + 1)}-${pad(d)}`;
export const parseDate = iso => { const [y, m, d] = iso.split('-').map(Number); return new Date(y, m - 1, d); };
const dayFmt = new Intl.DateTimeFormat('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
export const fmtDay = iso => dayFmt.format(parseDate(iso)).replace(',', '');       // "Thu 24 Sep"
/** "25/09/2026" from YYYY-MM-DD (or "09/2026" / "2026" when a source only knows that much). */
export const fmtDMY = iso => { const [y, m, d] = String(iso || '').split('-'); return d ? `${d}/${m}/${y}` : m ? `${m}/${y}` : y || ''; };
export const relDays = n => (n === 0 ? 'today' : n === 1 ? 'tomorrow' : `in ${n} days`);
export const hhmm = epoch => { const d = new Date(epoch * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };

/** Google Calendar's own "new event" page, filled in for a gig — the Diary card can only read the calendar, so adding
 *  one is a click on Save there. With a time (doors, or Ticketmaster's start) it runs four hours, past midnight if need
 *  be; without one it's an all-day event. */
export function gcalUrl({ artist, venue, city, date, doors, notes, ticket_url }) {
  const stamp = (d, withTime) => `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}` + (withTime ? `T${pad(d.getHours())}${pad(d.getMinutes())}00` : '');
  const day = parseDate(date);
  let dates;
  if (/^\d{2}:\d{2}$/.test(doors || '')) {
    const [h, m] = doors.split(':').map(Number);
    const start = new Date(day.getFullYear(), day.getMonth(), day.getDate(), h, m);
    dates = `${stamp(start, true)}/${stamp(new Date(start.getTime() + 4 * 3600e3), true)}`;
  } else {
    dates = `${stamp(day)}/${stamp(new Date(day.getFullYear(), day.getMonth(), day.getDate() + 1))}`;
  }
  const details = [notes, doors ? `Doors ${doors}` : '', ticket_url ? `Tickets: ${ticket_url}` : ''].filter(Boolean).join('\n');
  const q = new URLSearchParams({ action: 'TEMPLATE', text: `${artist} (gig)`, dates, ctz: 'Europe/London',
    location: [venue, city].filter(Boolean).join(', '), details });
  return `https://calendar.google.com/calendar/render?${q}`;
}

export function bytes(n) {
  const u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let i = 0;
  while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
  return `${n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)} ${u[i]}`;
}

export function duration(seconds) {
  const d = Math.floor(seconds / 86400), h = Math.floor(seconds % 86400 / 3600), m = Math.floor(seconds % 3600 / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}

/** "42s", "3m 12s", "2h 14m", "3d 4h" — a project's uptime: seconds while it's genuinely quick to watch tick,
 *  coarsening as it grows so a service that's been up for weeks still reads cleanly. */
export function uptimeText(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${pad(s % 60)}s`;
  return `${s}s`;
}

/** Live-tick every [data-since] (a running project's uptime) from the epoch seconds it started. */
export function tickUptimes() {
  const now = Date.now() / 1000;
  $$('[data-since]').forEach(el => { el.textContent = uptimeText(now - Number(el.dataset.since)); });
}

/** Live clock: fills every [data-clock]/[data-dow]/[data-dmy] currently in the page. */
export function tickClock() {
  const d = new Date();
  const t = `${pad(d.getHours())}:${pad(d.getMinutes())}<small>${pad(d.getSeconds())}</small>`;
  $$('[data-clock]').forEach(el => { el.innerHTML = t; });
  $$('[data-date]').forEach(el => { el.textContent = d.toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long' }); });
  $$('[data-dow]').forEach(el => { el.textContent = d.toLocaleDateString('en-GB', { weekday: 'long' }); });
  $$('[data-dmy]').forEach(el => { el.textContent = d.toLocaleDateString('en-GB', { day: 'numeric', month: 'long' }); });
}

let toastTimer;
export function toast(message) {
  let el = $('#toast');
  if (!el) { el = document.createElement('div'); el.id = 'toast'; el.className = 'toast'; el.setAttribute('role', 'status'); document.body.append(el); }
  el.textContent = message;
  el.classList.add('is-on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('is-on'), 5000);
}

export function ago(epoch) {
  if (!epoch) return 'never';
  const s = Math.max(0, Date.now() / 1000 - epoch);
  if (s < 90) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
