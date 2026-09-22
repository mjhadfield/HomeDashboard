// Owner dashboard bootstrap: load state, render cards, keep them fresh from the event stream.
import * as api from './api.js';
import { cards, ui, selectedSlug } from './cards.js';
import { newsDef, newsUi } from './content-cards.js';
import { Shell } from './shell.js';
import { $, $$, esc, ico, tickClock, tickUptimes, toast } from './util.js';

let state = null;
const shell = new Shell($('#grid'), cards, id => renderCard(id));
const defs = new Map(cards.map(c => [c.id, c]));

// News lives on its own page now, not the dashboard grid — a second Shell, permanently "expanded"
// (pinnedOpen: filters/search visible, all rows shown, no expand button), reusing the same card def and
// render function unchanged. Item-action clicks (pin/dismiss/mute/mark-seen) still work with no new code —
// they're delegated at `document` level below, not scoped to either shell's own grid.
const newsShell = new Shell($('#view-news'), [{ ...newsDef, pinnedOpen: true }], id => renderCard(id));

// The Monitoring tile's data lives here, not on `state` — `state` gets wholesale replaced by every unrelated
// dashboard refresh (any widget's own refresh notifies over SSE and triggers a full re-fetch; the System
// widget alone does this every 30s), which was wiping `state.monitoring` and flashing "Reading sensors…"
// each time, even though the tile's own 1s-fresh data was perfectly fine. Keeping it in its own variable and
// merging it in at render time means it survives every one of those reloads untouched.
let monitoring = null;

function renderCard(id) {
  if (!state) return;
  if (id === 'news') { newsShell.update(id, newsDef.render(state, newsShell.ctx(id))); return; }
  if (id === 'monitoring') { shell.update(id, defs.get(id).render({ ...state, monitoring }, shell.ctx(id))); return; }
  shell.update(id, defs.get(id).render(state, shell.ctx(id)));
  if (id === 'now') tickClock();
}

function renderAlerts() {
  $('#alerts').innerHTML = state.alerts.map(a => `<div class="alert cut">${ico(a.icon)}
    <span><b>${esc(a.title)}</b> — ${esc(a.detail)}</span>
    <span class="chip ${a.level === 'now' ? 'chip--today' : 'chip--new'} alert__when">${esc(a.chip)}</span></div>`).join('');
}

// The Media page's iframe is created once in the HTML and its src is set exactly once, here — never
// reassigned on later state refreshes, so navigating away and back never reloads (and never loses the
// session/playback state of) whatever's embedded there.
const mediaFrame = $('#media-frame');
let mediaSrcSet = false;
function setMediaSrc() {
  if (mediaSrcSet) return;
  const link = state.links.find(l => l.for_media);
  if (!link) return;
  mediaFrame.src = link.url;
  mediaSrcSet = true;
}

// Same "set once" idea as setMediaSrc(), a separate latch so the two iframes never interfere with each other.
const musicFrame = $('#music-frame');
let musicSrcSet = false;
function setMusicSrc() {
  if (musicSrcSet) return;
  const link = state.links.find(l => l.for_music);
  if (!link) return;
  musicFrame.src = link.url;
  musicSrcSet = true;
}

function renderAll() {
  cards.forEach(c => renderCard(c.id));
  renderCard('news');
  renderAlerts();
  setMediaSrc();
  setMusicSrc();
  $('#host-label').textContent = `${state.host} · ${location.hostname}`;
  $('#device-label').textContent = `${state.device.role === 'owner' ? 'Owner' : state.device.role} device`;
}

// The Monitoring tile needs to feel close to real-time — temperatures swing over seconds, not the 30s+ the
// other widgets refresh at — so it's polled directly here, bypassing the state/SSE cycle entirely. Routing it
// through there instead would mean every tick triggers a full dashboard refresh (including two systemctl calls
// for project status) just for one tile. The server-side reading itself is a background loop now too (see
// app/monitor.py) — this fetch just reads its latest cached value, no live sensor read in the request at all.
async function pollMonitoring() {
  if (!state || document.hidden) return;
  try { monitoring = await api.get('/api/owner/monitoring'); } catch { /* keep the last good reading, don't blank the tile over one dropped request */ }
  renderCard('monitoring');
}

// ---- view router (Home / News / Media / Companion / future sections) --------------------------------------------------
const VIEWS = ['home', 'news', 'media', 'music'];
function route() {
  const v = VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'home';
  $$('.view').forEach(el => el.classList.toggle('is-active', el.dataset.view === v));
  $$('.rail a[data-view], .tabbar a[data-view]').forEach(a => {
    if (a.dataset.view === v) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
}
addEventListener('hashchange', route);
route();

let loading = null;
async function load() {
  if (loading) return loading;                       // coalesce bursts of events into one fetch
  loading = api.get('/api/owner/state').then(s => { state = s; renderAll(); }).catch(e => console.error(e)).finally(() => { loading = null; });
  return loading;
}
let debounce;
const changed = () => { clearTimeout(debounce); debounce = setTimeout(load, 250); };

// ---- start / stop -----------------------------------------------------------------------------------------
document.addEventListener('click', async e => {
  const pin = e.target.closest('[data-pin]');
  if (pin) {                                                                // pin / unpin a link or project
    const { pin: kind, id, to } = pin.dataset;
    try { await api.post(`/api/owner/${kind === 'link' ? 'links' : 'projects'}/${encodeURIComponent(id)}/pin`, { pinned: to === 'true' }); }
    catch (err) { toast(`Couldn't ${to === 'true' ? 'pin' : 'unpin'}: ${err.message}`); }
    await load();
    return;
  }
  const curate = e.target.closest('[data-item-act]');
  if (curate) {                                                             // pin / dismiss / restore an item, or mute its artist
    const { itemAct, id, to, artist } = curate.dataset;
    try {
      if (itemAct === 'mute') {
        await api.post('/api/owner/artists/mute', { name: artist });
        toast(`Muted ${artist} — undo in Settings → Music`);
      } else {
        await api.post(`/api/owner/items/${id}/status`, { status: to });
      }
    } catch (err) { toast(err.message); }
    await load();
    return;
  }
  const seen = e.target.closest('[data-mark-seen]');
  if (seen) {
    try { const r = await api.post(`/api/owner/items/mark-seen/${seen.dataset.markSeen}`); toast(r.cleared ? `Marked ${r.cleared} as seen` : 'Nothing new to clear'); }
    catch (err) { toast(err.message); }
    await load();
    return;
  }
  // A click on an unread headline's own link (target="_blank" — the browser's default navigation already
  // opens it regardless of what happens here): mark it read, distinct from status="seen". Not gated on success
  // blocking anything, since the article's already open in its own tab either way.
  const readLink = e.target.closest('[data-item-read]');
  if (readLink) {
    try { await api.post(`/api/owner/items/${readLink.dataset.itemRead}/read`); } catch (err) { toast(err.message); }
    await load();
    return;
  }
  const vinylToggle = e.target.closest('[data-news-vinyl]');
  if (vinylToggle) {                                                        // News-only "vinyl stories" filter: pure client-side, no server round-trip
    newsUi.vinyl = !newsUi.vinyl;
    renderCard('news');
    return;
  }
  const resetFilters = e.target.closest('[data-news-reset]');
  if (resetFilters) {
    newsUi.source = ''; newsUi.tag = ''; newsUi.vinyl = false;
    renderCard('news');
    return;
  }
  const btn = e.target.closest('[data-act]');
  if (btn) {
    const { act, slug } = btn.dataset;
    ui.pending[slug] = act;
    renderCard('projects');
    renderCard('launchpad');    // a desktop project's Launchpad tile is itself the start button, so it needs the busy state too
    try {
      await api.post(`/api/owner/projects/${encodeURIComponent(slug)}/${act}`);
    } catch (err) {
      toast(`Couldn't ${act} ${slug}: ${err.message}`);
    } finally {
      delete ui.pending[slug];
      await load();
    }
    return;
  }
  const row = e.target.closest('.proj[data-select]');                      // pick a project to view its log (expanded view)
  if (row && $('#projects').classList.contains('is-max')) {
    ui.selected = row.dataset.slug;
    ui.logText = '';
    renderCard('projects');
    pollLogs();
  }
});

document.addEventListener('change', e => {
  const source = e.target.closest('[data-news-source]');
  if (source) { newsUi.source = source.value; renderCard('news'); return; }   // News-only source filter: pure client-side, no server round-trip
  const tagSelect = e.target.closest('[data-news-tag-select]');
  if (tagSelect) { newsUi.tag = tagSelect.value; renderCard('news'); }        // News-only band filter: same pattern
});

// ---- live log for the selected project while the Projects card is expanded ----------------------------------
let logTimer = null;
async function pollLogs() {
  if (!state || !$('#projects').classList.contains('is-max')) return;
  const slug = selectedSlug(state);
  if (!slug) return;
  try {
    const { lines } = await api.get(`/api/owner/projects/${encodeURIComponent(slug)}/logs?n=300`);
    const text = lines.map(l => `${l.t} ${l.msg}`).join('\n');
    if (slug !== selectedSlug(state)) return;                              // selection changed while fetching
    ui.logText = text;
    const pre = $('[data-log]', $('#projects'));
    if (pre) {
      const stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
      pre.textContent = text || 'No output yet — start the project to see its log.';
      if (stick) pre.scrollTop = pre.scrollHeight;
    }
  } catch { /* transient */ }
}
document.addEventListener('card:max', e => {
  if (e.detail.id !== 'projects') return;
  clearInterval(logTimer);
  if (e.detail.on) { ui.logText = ''; pollLogs(); logTimer = setInterval(pollLogs, 3000); }
});

// ---- boot -------------------------------------------------------------------------------------------------
await load();
tickClock();
tickUptimes();
setInterval(() => { tickClock(); tickUptimes(); }, 1000);
pollMonitoring();
setInterval(pollMonitoring, 2000);

const dot = $('#conn-dot');
api.subscribe(changed, ok => { dot.className = `dot ${ok ? 'dot--ok' : 'dot--warn'}`; dot.title = ok ? 'live' : 'reconnecting…'; if (ok) changed(); });
setInterval(load, 60_000);                                                  // safety net if the stream is blocked
document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
