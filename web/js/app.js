// Owner dashboard bootstrap: load state, render cards, keep them fresh from the event stream.
import * as api from './api.js';
import { cards, ui, selectedSlug } from './cards.js';
import { newsDef, newsUi, vinylUi } from './content-cards.js';
import { Shell } from './shell.js';
import { $, $$, esc, ico, tickClock, tickUptimes, toast } from './util.js';
import { initThemeToggle, toggleTheme, currentTheme } from './theme.js';

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
  if (id === 'vinyl') {
    // Shell's own expand/collapse (#apply) already hides .maxbar (filters + search) on the small tile and
    // un-hides it once expanded — that has to be reasserted here too, not just left to Shell, because
    // Confirmed Pre-orders has no filtering of its own and must stay hidden even when expanded.
    const card = $('#vinyl');
    const maxbar = $('.maxbar', card);
    if (maxbar) maxbar.hidden = vinylUi.mode === 'preorders' || !card.classList.contains('is-max');
  }
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
// Music Companion reads its initial theme off this query param (it has no other way to know Citadel's
// current theme before its own first paint); live theme changes after this point go via postMessage
// instead, in the [data-theme-toggle] click handler below, since the iframe's src is never reassigned.
const musicFrame = $('#music-frame');
const MUSIC_ORIGIN = 'http://192.168.0.66:8642';
let musicSrcSet = false;
function setMusicSrc() {
  if (musicSrcSet) return;
  const link = state.links.find(l => l.for_music);
  if (!link) return;
  const sep = link.url.includes('?') ? '&' : '?';
  musicFrame.src = `${link.url}${sep}theme=${currentTheme()}`;
  musicSrcSet = true;
}

// Same "set once" idea again, for the SQL DB page's iframe (Music DB Explorer).
const sqlFrame = $('#sql-frame');
let sqlSrcSet = false;
function setSqlSrc() {
  if (sqlSrcSet) return;
  const link = state.links.find(l => l.for_sql);
  if (!link) return;
  sqlFrame.src = link.url;
  sqlSrcSet = true;
}

function renderAll() {
  cards.forEach(c => renderCard(c.id));
  renderCard('news');
  renderAlerts();
  setMediaSrc();
  setMusicSrc();
  setSqlSrc();
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

// ---- view router (Home / News / Media / Companion / SQL DB / future sections) --------------------------------------------------
const VIEWS = ['home', 'news', 'media', 'music', 'sql'];
function route() {
  const v = VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'home';
  $$('.view').forEach(el => el.classList.toggle('is-active', el.dataset.view === v));
  $$('.rail a[data-view], .tabbar a[data-view]').forEach(a => {
    if (a.dataset.view === v) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
}
addEventListener('hashchange', route);
route();

const themeBtn = $('[data-theme-toggle]');
if (themeBtn) initThemeToggle(themeBtn);

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
  const themeToggle = e.target.closest('[data-theme-toggle]');
  if (themeToggle) {
    toggleTheme(themeToggle);
    if (musicSrcSet) musicFrame.contentWindow.postMessage({ type: 'citadel-theme', theme: currentTheme() }, MUSIC_ORIGIN);
    return;
  }
  const pin = e.target.closest('[data-pin]');
  if (pin) {                                                                // pin / unpin a link or project
    const { pin: kind, id, to } = pin.dataset;
    try { await api.post(`/api/owner/${kind === 'link' ? 'links' : 'projects'}/${encodeURIComponent(id)}/pin`, { pinned: to === 'true' }); }
    catch (err) { toast(`Couldn't ${to === 'true' ? 'pin' : 'unpin'}: ${err.message}`); }
    await load();
    return;
  }
  const curate = e.target.closest('[data-item-act]');
  if (curate) {                                                             // pin / dismiss / restore an item, mute its artist, or resolve a pre-order
    // "date" is deliberately not read from curate.dataset.date here — tickClock() (see util.js) sweeps every
    // element with a plain [data-date] attribute once a second to redraw the Now card's clock, and would stomp
    // this button's own icon the same way it did before this was renamed to data-release-date.
    const { itemAct, id, to, artist, releaseDate, title } = curate.dataset;
    // Purchased (moves a listing to Confirmed Pre-orders) and both pre-order resolutions are one-way enough to
    // warrant a pause; dismiss/pin/seen stay instant, same as everywhere else in the app.
    const prompts = {
      purchased: `Mark “${title}” as purchased? It’ll move to Confirmed Pre-orders.`,
      received: `Mark “${title}” as received? It’ll come off this list.`,
      remove: `Remove “${title}” from Confirmed Pre-orders? This can’t be undone.`,
    };
    if (prompts[itemAct] && !confirm(prompts[itemAct])) return;
    try {
      if (itemAct === 'mute') {
        await api.post('/api/owner/artists/mute', { name: artist });
        toast(`Muted ${artist} — undo in Settings → Music`);
      } else if (itemAct === 'remove' || itemAct === 'received') {
        await api.del(`/api/owner/items/${id}`);
      } else {
        await api.post(`/api/owner/items/${id}/status`, { status: to });
        // Vinyl radar shows a date it may have borrowed from another store listing the same record (the
        // underlying row's own date can be blank) — freeze that displayed date onto the row now, or Confirmed
        // Pre-orders would show TBC for something that visibly had a release date a second ago.
        if (itemAct === 'purchased' && releaseDate) await api.patch(`/api/owner/items/${id}/date`, { date: releaseDate });
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
  const vinylMode = e.target.closest('[data-vinyl-mode]');
  if (vinylMode) {                                                          // dual-content tile header: Shell never touches this markup again after building it, so the pressed state is set here directly
    vinylUi.mode = vinylMode.dataset.vinylMode;
    $$('[data-vinyl-mode]', vinylMode.closest('.card__head')).forEach(b => b.setAttribute('aria-pressed', b === vinylMode));
    renderCard('vinyl');
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
  if (tagSelect) { newsUi.tag = tagSelect.value; renderCard('news'); return; } // News-only band filter: same pattern
});

// A pre-order's release-date field commits on blur, not on `change` — Chrome fires `change` on a date input
// after every keystroke that makes *some* value parseable (typing just the year's first digit already yields
// a complete, if wrong, "0002-…" date), and reacting to that mid-type triggers load()'s full card re-render,
// which tears down the very input the person is still typing into. Waiting for focusout means the API only
// ever sees what they actually finished typing, and only when it's genuinely different from what was there.
// The "only if unset" guard on focusin matters: a multi-segment date field can re-fire focusin without an
// intervening focusout (seen from Playwright's fill(), plausible from real segment-to-segment interaction too)
// — overwriting dataset.orig there would snapshot the value *after* the edit instead of before it, so the
// focusout comparison would never see a difference and silently drop the change.
document.addEventListener('focusin', e => {
  const el = e.target.closest('[data-preorder-date]');
  if (el && el.dataset.orig === undefined) el.dataset.orig = el.value;
});
document.addEventListener('focusout', e => {
  const el = e.target.closest('[data-preorder-date]');
  if (!el) return;
  if (el.value !== el.dataset.orig) {
    api.patch(`/api/owner/items/${el.dataset.preorderDate}/date`, { date: el.value || null })
      .catch(err => toast(err.message)).finally(load);
  }
  delete el.dataset.orig;
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
