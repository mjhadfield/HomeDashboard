// TV / kiosk view. Read-only: it shows only the panels the owner allowed (server-enforced — disallowed data
// never reaches this page), rotating on a timer, with a reminders banner and a slow pixel-shift against burn-in.
import * as api from './api.js';
import { $, esc, ico, fmtDay, relDays, hhmm, tickClock, isoOf, parseDate } from './util.js';

const stage = $('#stage'), pips = $('#pips'), banner = $('#banner'), foot = $('#foot'), tv = $('#tv');
let state = null, current = 0, rotor = null, ids = '', lastOk = null, online = true;

// ---- panels ---------------------------------------------------------------------------------------------------
const msg = (icon, text, small = '') => `<div class="tv-msg">${ico(icon)}<div>${esc(text)}</div>${small ? `<small>${esc(small)}</small>` : ''}</div>`;

function binRow(item, streams, hour) {
  if (!item) return '';
  const s = streams[item.stream] || { title: item.stream, sub: '', colour: '#6b7280' };
  const due = item.in_days <= 1;
  const chip = item.in_days === 0 ? '<span class="chip chip--today">Collection today</span>' : item.in_days === 1 && hour >= 17 ? '<span class="chip chip--today">Put out tonight</span>' : '';
  return `<div class="card"><div class="card__in"><div class="tv-bin${due ? ' tv-bin--due' : ''}" style="--sw:${esc(s.colour)}"><span class="tv-bin__swatch"></span>
    <div><div class="tv-bin__name">${esc(s.title)} <small>${esc(s.sub)}</small></div>${chip}</div>
    <div class="tv-bin__when">${esc(fmtDay(item.date))}<br>${esc(relDays(item.in_days))}</div></div></div></div>`;
}

function binsPanel(w, hour) {
  if (!w?.configured) return msg('bin', 'Bin days aren’t set up yet', 'Settings → Widgets');
  if (!w.data) return msg('bin', w.error ? 'Couldn’t read the council page' : 'Fetching bin days…');
  return `<div class="big-list"><span class="hud">Next collections</span>${binRow(w.data.next.recycling, w.data.streams, hour)}${binRow(w.data.next.domestic, w.data.streams, hour)}</div>`;
}

/** A TV wants the venue, not the postal address: keep just the first part of a long location. */
const venue = loc => loc.split(',')[0].trim();

function gigsPanel(w) {
  const n = w?.data?.next;
  if (!n) return msg('bolt', 'No gigs booked', 'Add your tickets in Settings → Music');
  const days = n.in_days === 0 ? 'GIG' : String(n.in_days).padStart(2, '0');
  const unit = n.in_days === 0 ? 'tonight' : n.in_days === 1 ? 'day to go' : 'days to go';
  const where = [n.venue, n.city].filter(Boolean).join(', ');
  const more = w.data.upcoming.slice(1, 4).map(g => `<div class="tv-ev"><span class="tv-ev__t">${esc(fmtDay(g.date))}</span><span class="tv-ev__title">${esc(g.artist)}</span></div>`).join('');
  return `<div class="big-list"><span class="hud">Next gig</span><div class="card"><div class="card__in"><div class="tv-gig">
    <div><div class="tv-gig__count">${days}</div></div>
    <div><div class="tv-gig__unit">${unit}</div><div class="tv-gig__name">${esc(n.artist)}</div>
      <div class="tv-gig__sub">${esc(where)} · ${esc(fmtDay(n.date))}${n.doors ? ` · doors ${esc(n.doors)}` : ''}</div></div></div>${more ? `<div class="tv-day"><div class="tv-day__d">Then</div><div>${more}</div></div>` : ''}</div></div></div>`;
}

function diaryPanel(w, todayIso) {
  if (!w?.configured) return msg('cal', 'No calendar connected', 'Settings → Widgets');
  if (!w.data) return msg('cal', w.error ? 'Couldn’t read the calendar' : 'Loading…');
  const d = parseDate(todayIso); d.setDate(d.getDate() + 1);
  const tomorrow = isoOf(d.getFullYear(), d.getMonth(), d.getDate());
  const groups = new Map();
  let n = 0;
  for (const e of w.data.events) {
    if (n >= 7 || (!groups.has(e.date) && groups.size >= 4)) continue;
    if (!groups.has(e.date)) groups.set(e.date, []);
    groups.get(e.date).push(e); n++;
  }
  if (!groups.size) return msg('cal', 'Nothing planned', 'Enjoy the quiet.');
  const rows = [...groups].map(([date, evs]) => `<div class="tv-day"><div class="tv-day__d">${date === todayIso ? 'Today' : date === tomorrow ? 'Tomorrow' : esc(fmtDay(date))}</div><div>${
    evs.map(e => `<div class="tv-ev"><span class="tv-ev__t">${e.all_day ? 'All day' : esc(e.time)}</span><span class="tv-ev__title">${esc(e.title)}</span>${e.location ? `<small>${esc(venue(e.location))}</small>` : ''}</div>`).join('')}</div></div>`).join('');
  return `<div class="big-list"><span class="hud">Coming up</span><div class="card"><div class="card__in">${rows}</div></div></div>`;
}

// ---- render ---------------------------------------------------------------------------------------------------
function panels(s) {
  const hour = new Date(s.now).getHours(), today = s.now.slice(0, 10);
  const build = { bins: () => binsPanel(s.widgets.bins, hour), diary: () => diaryPanel(s.widgets.diary, today), gigs: () => gigsPanel(s.widgets.gigs) };
  const list = s.tv.panels.map(id => ({ id, html: build[id]() }));
  return list.length ? list : [{ id: 'empty', html: msg('grid', 'Nothing to show yet', 'Choose panels in Settings → Screens') }];
}

function show(i) {
  current = i;
  [...stage.children].forEach((el, k) => el.toggleAttribute('data-on', k === i));
  [...pips.children].forEach((el, k) => el.toggleAttribute('data-on', k === i));
}

function render() {
  const list = panels(state);
  const key = list.map(p => p.id).join(',');
  if (key !== ids) {                                            // set of panels changed: rebuild
    ids = key;
    stage.innerHTML = list.map(p => `<div class="panel">${p.html}</div>`).join('');
    pips.innerHTML = list.length > 1 ? list.map(() => '<i></i>').join('') : '';
    current = Math.min(current, list.length - 1);
    show(current);
  } else {
    list.forEach((p, k) => { const el = stage.children[k]; if (el.innerHTML !== p.html) el.innerHTML = p.html; });
  }
  banner.innerHTML = state.alerts.map(a => `<div class="tv__alert cut">${ico(a.icon)}<span><b>${esc(a.title)}</b> — ${esc(a.detail)}</span>
    <span class="chip ${a.level === 'now' ? 'chip--today' : 'chip--new'}">${esc(a.chip)}</span></div>`).join('');

  clearInterval(rotor);
  if (list.length > 1) rotor = setInterval(() => show((current + 1) % list.length), state.tv.rotate_seconds * 1000);
  tickClock();
  renderFoot();
}

function renderFoot() {
  foot.innerHTML = online ? `<span class="dot dot--ok"></span>Live${lastOk ? ` · updated ${esc(hhmm(lastOk))}` : ''}` : '<span class="dot dot--warn"></span>Reconnecting…';
}

// ---- data -------------------------------------------------------------------------------------------------------
let loading = null;
function load() {
  if (loading) return loading;
  loading = api.get('/api/kiosk/state')
    .then(s => { state = s; lastOk = Date.now() / 1000; online = true; render(); })
    .catch(() => { online = false; renderFoot(); })
    .finally(() => { loading = null; });
  return loading;
}
let debounce;
const changed = () => { clearTimeout(debounce); debounce = setTimeout(load, 300); };

await load();
tickClock();
setInterval(tickClock, 1000);
api.subscribe(changed, ok => { online = ok; renderFoot(); if (ok) changed(); }, '/api/kiosk/events');
setInterval(load, 60_000);                                                    // safety net
setInterval(() => { tv.style.transform = `translate(${Math.round(Math.random() * 8 - 4)}px, ${Math.round(Math.random() * 8 - 4)}px)`; }, 60_000);
