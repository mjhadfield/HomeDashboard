// The Music page (#listen): playlists Citadel designs and keeps on Spotify, Discover, gig setlists, full-page Releases
// and Vinyl radar, and a player bar that steers Spotify (the desktop app, a phone…). app.js tells this module when the
// page is shown (onView) and hands over each fresh dashboard state (onState) — Releases/Vinyl render from that, exactly
// as Home's cards do. Everything else comes from /api/owner/music, and a 'music' event says when to look again.
//
// Never reuse [data-act] (project start/stop) or [data-date] (the Now card's clock sweep) here — app.js and util.js
// treat those names globally. Row actions on Releases/Vinyl ([data-item-act]) deliberately go through app.js's handler,
// so pin/dismiss/purchase behave exactly as on Home.
import * as api from './api.js';
import { $, $$, esc, ico, safeUrl, toast } from './util.js';
import { musicRowWith, vinylRowWith, preorderRowWith, narrow } from './content-cards.js';

const API = '/api/owner/music';
const root = $('#view-listen');
const TABS = [['playlists', 'Playlists', 'list'], ['discover', 'Discover', 'spark'], ['rediscover', 'Rediscover', 'history'], ['gigs', 'Gigs', 'bolt'], ['releases', 'Releases', 'disc'], ['vinyl', 'Vinyl', 'radar']];
const SRC_LABEL = { history: 'history', shelf: 'shelf', deep: 'deep cut', gig: 'setlist', new: 'new release', similar: 'last.fm', related: 'musicbrainz', tag: 'last.fm tag' };
const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private window: just not remembered */ } },
};

let dash = null;           // the dashboard's own state (releases, vinyl, gigs widgets)
let ov = null;             // GET /api/owner/music
let active = false, started = false;
let tab = TABS.some(t => t[0] === store.get('mu-tab')) ? store.get('mu-tab') : 'playlists';
let cur = null;            // the open playlist, with its tracks
let selected = Number(store.get('mu-pl', 0)) || null;
let disc = { basis: store.get('mu-basis', '12m'), filter: 'all', data: null };
let redis = null;           // GET /rediscover
let gigsData = null, gigSel = null;
const predictions = {};    // artist -> prediction | 'loading' | {error}
const cands = {};          // track pos -> candidate list (unmatched songs)
const rel = { filter: 'all', search: '' }, vin = { mode: 'radar', filter: 'all' };
let bar = ['full', 'compact', 'hidden'].includes(store.get('mu-bar')) ? store.get('mu-bar') : 'full';
let pl = null, plAt = 0, devMenu = null;                     // player state, when it was read, open device menu
let es = null;

// ---- helpers ----------------------------------------------------------------------------------------------------------
const hash = s => [...String(s)].reduce((h, c) => (h * 31 + c.charCodeAt(0)) | 0, 7);
const ART = ['', 'art--b', 'art--c', 'art--d'];
const caa = mbid => (mbid ? `https://coverartarchive.org/release-group/${encodeURIComponent(mbid)}/front-250` : '');
function art(name, img, cls = '') {
  const n = String(name || '?');
  return `<span class="art ${ART[Math.abs(hash(n)) % 4]} ${cls}">${esc(n.replace(/^the\s+/i, '')[0] || '?')}${img ? `<img src="${esc(img)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer">` : ''}</span>`;
}
root.addEventListener('error', e => { if (e.target.tagName === 'IMG' && e.target.closest('.art')) e.target.remove(); }, true);
const mmss = ms => { const s = Math.max(0, Math.floor(ms / 1000)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
const ord = n => `${n}${n % 100 >= 11 && n % 100 <= 13 ? 'th' : ['th', 'st', 'nd', 'rd'][n % 10] || 'th'}`;
const plays = n => `${Number(n).toLocaleString()} play${n === 1 ? '' : 's'}`;
const dmy = iso => { const [y, m, d] = String(iso || '').split('-'); return d ? `${Number(d)} ${MON[Number(m) - 1]} ${y}` : iso || ''; };
const whenRel = epoch => {
  if (!epoch) return '';
  const s = Date.now() / 1000 - epoch;
  if (s < 0) { const d = new Date(epoch * 1000); return `${DAYS[(d.getDay() + 6) % 7]} ${d.getDate()} ${MON[d.getMonth()]}, ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`; }
  return s < 90 ? 'just now' : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
};
const spotifyReady = () => !!(ov?.spotify?.connected && ov.spotify.enabled);
const ing = id => ov?.ingredients.find(i => i.id === id);
const scVar = src => ({ similar: 'new', related: 'new', tag: 'new' }[src] || src);

async function call(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(typeof okMsg === 'function' ? okMsg(r) : okMsg); return r; } catch (e) { toast(e.message); return null; }
}
function busy(btn, on) { if (btn) btn.classList.toggle('is-busy', on); }

// ---- lifecycle ----------------------------------------------------------------------------------------------------------
export function onState(s) {
  dash = s;
  if (active && (tab === 'releases' || tab === 'vinyl')) renderPanel();
}

export function onView(view) {
  const was = active;
  active = view === 'listen';
  if (active && !started) { started = true; boot(); }
  else if (active && !was) { loadOverview(); pollPlayer(); }
}

async function boot() {
  root.innerHTML = `<div class="mu" data-bar="${bar}"><div class="mu__top"><nav class="mtabs" aria-label="Music sections">${TABS.map(([id, label, icon]) =>
    `<button type="button" data-mu-tab="${id}" aria-pressed="${id === tab}">${ico(icon)}${label}</button>`).join('')}</nav><span class="mu-status" id="mu-status"></span></div>
    <div id="mu-banner"></div><div class="mu__panel" id="mu-panel"><div class="mu-empty"><span class="spin"></span></div></div>
    <footer class="mu-bar" id="mu-bar" aria-label="Now playing"></footer></div>`;
  await loadOverview();
  if (selected && !ov.playlists.some(p => p.id === selected)) selected = null;
  selected ??= ov.playlists[0]?.id ?? null;
  if (selected) await loadPlaylist(selected, false);
  renderPanel();
  pollPlayer();
  setInterval(() => { if (active && !document.hidden) pollPlayer(); }, 3000);
  setInterval(tickProgress, 1000);
  document.addEventListener('visibilitychange', () => { if (active && !document.hidden) pollPlayer(); });
  es = new EventSource('/api/owner/events');
  es.addEventListener('music', e => { let d = {}; try { d = JSON.parse(e.data); } catch { /* fine */ } onMusicEvent(d.what); });
}

let evTimer;
function onMusicEvent(what) {
  clearTimeout(evTimer);
  evTimer = setTimeout(async () => {
    if (what === 'discover') { if (tab === 'discover') await loadDiscover(); return; }
    await loadOverview();
    if (what === 'playlists' && cur && !editingNow()) { await loadPlaylist(cur.id, false); if (tab === 'playlists') renderPreview(); }
    renderTop();
    if (tab === 'playlists') renderStrip();
  }, 300);
}
const editingNow = () => !!root.querySelector('.pl-design :focus');

async function loadOverview() {
  try { ov = await api.get(API); } catch (e) { toast(e.message); return; }
  renderTop();
}

async function loadPlaylist(id, render = true) {
  try { cur = await api.get(`${API}/playlists/${id}`); selected = id; store.set('mu-pl', id); } catch { cur = null; }
  if (render && tab === 'playlists') renderPanel();
}

// ---- header ------------------------------------------------------------------------------------------------------------
function renderTop() {
  if (!ov) return;
  const s = ov.spotify;
  let status;
  if (!s.configured) status = `<a href="/settings#listen"><span class="dot dot--warn"></span> Spotify isn’t set up</a>`;
  else if (s.expired || !s.connected) status = `<a href="/settings#listen"><span class="dot dot--warn"></span> <span class="warn">${s.expired ? 'Spotify login ran out — reconnect' : 'Connect Spotify'}</span></a>`;
  else status = `<span class="dot dot--ok"></span>Spotify${s.user ? ` · ${esc(s.user)}` : ''}${s.days_left != null ? `<span class="dim${s.days_left <= 14 ? ' warn' : ''}"> · login good for ${s.days_left} days</span>` : ''}`;
  const mini = bar === 'hidden' && pl?.active
    ? `<button type="button" class="mu-mini" data-mu-bar="full" title="Show the player"><span class="eq${pl.playing ? ' is-on' : ''}"><i></i><i></i><i></i></span><span>${esc(pl.track.name)} · ${esc(pl.track.artists.join(', '))}</span></button>`
    : bar === 'hidden' ? `<button type="button" class="mu-mini" data-mu-bar="full" title="Show the player"><span class="eq"><i></i><i></i><i></i></span><span>Player</span></button>` : '';
  $('#mu-status').innerHTML = mini + status;
  $('#mu-banner').innerHTML = !ov.library.available
    ? `<div class="mu__banner">${ico('alert')}<span>Music Companion’s database wasn’t found, so playlists from your own listening can’t be built.</span><a class="btn" href="/settings#listen">Settings</a></div>`
    : !s.connected ? `<div class="mu__banner">${ico('wave')}<span>Connect Spotify to save playlists to your account and control playback. Designing and previewing works already.</span><a class="btn btn--go" href="/settings#listen">Set up Spotify</a></div>` : '';
}

// ---- panels -------------------------------------------------------------------------------------------------------------
function renderPanel() {
  if (!ov) return;
  $$('[data-mu-tab]', root).forEach(b => b.setAttribute('aria-pressed', String(b.dataset.muTab === tab)));
  const panel = $('#mu-panel');
  if (tab === 'playlists') panel.innerHTML = playlistsHtml();
  else if (tab === 'discover') { panel.innerHTML = discoverHtml(); if (!disc.data) loadDiscover(); }
  else if (tab === 'rediscover') { panel.innerHTML = rediscoverHtml(); if (!redis) loadRediscover(); }
  else if (tab === 'gigs') { panel.innerHTML = gigsHtml(); if (!gigsData) loadGigs(); }
  else if (tab === 'releases') { panel.innerHTML = releasesHtml(); shelfBadges(panel); }
  else { panel.innerHTML = vinylHtml(); shelfBadges(panel); }
}

/** "3 on your shelf" under a release or pre-order by an artist whose records you own. */
function shelfBadges(panel) {
  const owned = ov?.library?.owned || {};
  const key = s => s.toLowerCase().replace(/&/g, 'and').replace(/^the\s+/, '').replace(/[^\p{L}\p{N}]+/gu, '');
  $$('[data-mu-listen]', panel).forEach(b => {
    const n = owned[key(b.dataset.artist)];
    const main = b.closest('li')?.querySelector('.feed__main');
    if (n && main) main.insertAdjacentHTML('beforeend', `<div class="feed__flag"><span class="chip owned" title="Records by them on your shelf">${ico('disc')}${n} on your shelf</span></div>`);
  });
}

// ---- Playlists ------------------------------------------------------------------------------------------------------------
function tileHtml(p) {
  const covers = [...p.covers.map(m => caa(m)), '', '', '', ''].slice(0, 4);
  const sched = p.mode === 'rot'
    ? `<span class="mode mode--rot">${ico('refresh')}${p.schedule.until ? `Daily until ${dmy(p.schedule.until)}` : `${p.schedule.every}${p.schedule.every === 'weekly' ? ` · ${DAYS[p.schedule.day]}` : p.schedule.every === 'monthly' ? ` · ${ord(p.schedule.day)}` : ''} ${p.schedule.at}`}</span>`
    : `<span class="mode">${ico('lock')}Fixed${p.built_at ? ` · built ${whenRel(p.built_at)}` : ''}</span>`;
  const state = p.last_error ? '<span class="warn">⚠ couldn’t save</span>' : p.saved_at ? `on Spotify` : p.count ? 'not saved yet' : 'empty';
  return `<button type="button" class="pl-tile cut" data-mu-pl="${p.id}" aria-pressed="${p.id === selected}">
    <span class="collage">${covers.map((c, i) => art(p.name.slice(i), c)).join('')}</span>
    <span class="pl-tile__main"><span class="pl-tile__name"><small>${esc(ov.settings.prefix)}</small>${esc(p.name)}</span>${sched}
      <span class="pl-tile__sub">${p.count} tracks · ${state}${p.public ? '' : ' · private'}</span></span></button>`;
}
function renderStrip() { const s = $('.pl-strip', root); if (s) s.innerHTML = stripInner(); }
const stripInner = () => ov.playlists.map(tileHtml).join('') + `<button type="button" class="pl-tile pl-tile--new cut" data-mu-new>${ico('plus')}New playlist</button>`;

function playlistsHtml() {
  if (!ov.playlists.length) {
    return `<div class="pl-strip">${stripInner()}</div><section class="card mcard"><div class="card__in"><div class="mu-empty">${ico('list')}<b>No playlists yet</b>
      Start from a template — your forgotten favourites, deep cuts, your records, a setlist — and shape it from there.<br>
      <button type="button" class="btn btn--solid" data-mu-new>${ico('plus')}New playlist</button></div></div></section>`;
  }
  return `<div class="pl-strip">${stripInner()}</div>${cur ? `<div class="pl-editor">${designHtml(cur)}<div id="mu-preview">${previewHtml(cur)}</div></div>` : ''}`;
}

const NOT_PLAYED = [[0, 'any time'], [30, 'a month'], [90, '3 months'], [180, '6 months'], [365, 'a year'], [730, '2 years'], [1825, '5 years']];
const WITHIN = [[0, 'any time'], [30, 'last month'], [90, 'last 3 months'], [365, 'last year']];
const opt = (pairs, v) => pairs.map(([val, label]) => `<option value="${esc(val)}"${String(val) === String(v) ? ' selected' : ''}>${esc(label)}</option>`).join('');

function paramsHtml(i, k) {
  const p = i.params, f = (name, label, ctl, cls = '') => `<label class="${cls}">${esc(label)}${ctl}</label>`;
  const num = (name, v, min, max) => `<input class="fld" type="number" data-mu-param="${name}" value="${esc(v)}" min="${min}" max="${max}" style="width:90px">`;
  const sel = (name, pairs, v) => `<select class="fld" data-mu-param="${name}">${opt(pairs, v)}</select>`;
  const txt = (name, v, ph = '', w = '200px') => `<input class="fld" data-mu-param="${name}" value="${esc(v)}" placeholder="${esc(ph)}" maxlength="80" style="width:${w}">`;
  switch (i.kind) {
    case 'history': return f('', 'At least (plays)', num('min_plays', p.min_plays, 1, 5000)) + f('', 'Not played in', sel('not_played_days', NOT_PLAYED, p.not_played_days)) + f('', 'Played in', sel('played_within_days', WITHIN, p.played_within_days));
    case 'capsule': return f('', 'From', `<input class="fld" type="date" data-mu-param="start" value="${esc(p.start)}">`) + f('', 'To', `<input class="fld" type="date" data-mu-param="end" value="${esc(p.end)}">`) + f('', 'Called', txt('label', p.label, 'e.g. summer 2019', '160px'));
    case 'shelf': return f('', 'Which tracks', sel('mode', [['mixed', 'most played + never streamed'], ['most', 'your most played'], ['never', 'never streamed']], p.mode));
    case 'deep': return f('', 'From your top', num('top_n', p.top_n, 1, 100)) + f('', 'Artists of', sel('days', [[365, 'the last year'], [0, 'all time']], p.days));
    case 'artists': return f('', 'Artists (comma separated)', txt('names', p.names.join(', '), 'Gojira, Mastodon', '100%'), 'wide') + f('', 'Their', sel('mode', [['most', 'most played'], ['least', 'least played']], p.mode));
    case 'genre': return f('', 'Genre', `<select class="fld" data-mu-param="genre">${opt([...new Set([p.genre, ...ov.library.genres])].map(g => [g, g]), p.genre)}</select>`);
    case 'gig': return f('', 'From', sel('kind', [['predict', 'a predicted setlist'], ['show', 'a show you were at'], ['setlistfm', 'a setlist.fm setlist']], p.kind)) +
      (p.kind === 'predict' ? f('', 'Artist', txt('artist', p.artist, 'Amon Amarth')) : p.kind === 'show' ? f('', 'Show (from the Gigs tab)', num('show_id', p.show_id, 0, 1e9)) : f('', 'Setlist id', txt('id', p.id, 'e.g. 434cc7e7', '120px')));
    case 'releases': return f('', 'Out in the last', sel('days', [[30, '30 days'], [60, '60 days'], [90, '90 days'], [180, '6 months']], p.days)) + f('', 'Songs per album', num('per_album', p.per_album, 1, 20));
    case 'similar': case 'related': return f('', 'Seeded by your top', num('seeds', p.seeds, 3, 100)) + f('', 'Songs per artist', num('per_artist', p.per_artist, 1, 5));
    case 'tag': return f('', 'Last.fm tag', txt('tag', p.tag, 'sludge', '160px'));
    default: return '';
  }
}

function designHtml(p) {
  const s = p.schedule || {}, r = p.rules;
  const famLabel = r.fam >= 90 ? 'all familiar' : r.fam <= 10 ? 'all new to you' : `${r.fam}% familiar`;
  const ings = p.ings.map((i, k) => {
    const def = ing(i.kind) || { name: i.kind, icon: 'list', src: 'history', ready: true };
    return `<div class="ing${def.ready ? '' : ' ing--off'}" data-mu-ing="${k}" style="--sc: var(--src-${scVar(def.src)})"><span class="ing__sw"></span>
      <div><div class="ing__name">${ico(def.icon)}${esc(def.name)}${def.ready ? '' : ` <span class="chip chip--warn">${esc(def.why)}</span>`}</div><div class="ing__sum" data-mu-sum="${k}">${esc(p.summaries?.[k] || '')}</div></div>
      <label class="ing__share"><input type="range" min="5" max="100" step="5" value="${i.share}" data-mu-share="${k}" aria-label="${esc(def.name)} share"><span>${i.share}%</span></label>
      <button class="btn btn--icon" type="button" data-mu-ingx="${k}" title="Remove">${ico('x')}</button>
      <details${i.kind === 'artists' && !i.params.names.length ? ' open' : ''}><summary>Settings</summary><div class="ing__params">${paramsHtml(i, k)}</div></details></div>`;
  }).join('') || '<p class="note-line">No ingredients yet — add one.</p>';
  const saved = p.saved_at ? `Saved to Spotify ${whenRel(p.saved_at)}${p.spotify_url ? ` · <a href="${esc(safeUrl(p.spotify_url))}" target="_blank" rel="noopener">open in Spotify</a>` : ''}` : 'Not on Spotify yet';
  const next = p.mode === 'rot' ? (p.next_run ? `<br>next refresh ${whenRel(p.next_run)}` : p.spotify_id ? '' : '<br>rotates once it’s saved') : '';
  return `<section class="card mcard pl-design"><div class="card__in">
    <div class="mh"><span class="hud">Design</span><span class="meta" id="mu-saving"></span><span class="tools"><button class="btn btn--icon btn--stop" type="button" data-mu-delete title="Delete this playlist">${ico('x')}</button></span></div>
    <div class="mbody">
      <label class="lbl" for="mu-name">Name</label>
      <div class="namebox"><span>${esc(ov.settings.prefix)}</span><input id="mu-name" data-mu-field="name" value="${esc(p.name)}" maxlength="80"></div>
      <div class="erow">
        <div><span class="lbl">Type</span><span class="seg" role="group"><button type="button" data-mu-mode="fixed" aria-pressed="${p.mode === 'fixed'}">Fixed</button><button type="button" data-mu-mode="rot" aria-pressed="${p.mode === 'rot'}">Rotating</button></span></div>
        ${p.mode === 'rot' ? `<div><span class="lbl">Refresh</span><span class="sched">
          <select class="fld" data-mu-sched="every">${opt([['daily', 'Daily'], ['weekly', 'Weekly'], ['monthly', 'Monthly']], s.every)}</select>
          ${s.every === 'weekly' ? `<select class="fld" data-mu-sched="day">${opt(DAYS.map((d, i) => [i, d]), s.day)}</select>` : ''}
          ${s.every === 'monthly' ? `<select class="fld" data-mu-sched="day">${opt(Array.from({ length: 28 }, (_, i) => [i + 1, `the ${ord(i + 1)}`]), s.day)}</select>` : ''}
          <input class="fld" type="time" data-mu-sched="at" value="${esc(s.at || ov.settings.rotate_at)}">
          ${s.until ? `<span class="chip chip--new" title="After the show it switches to the real setlist">until ${esc(dmy(s.until))}</span>` : ''}</span></div>` : ''}
        <div><span class="lbl">On Spotify</span><button type="button" class="pub" role="switch" aria-checked="${p.public}" data-mu-public><span class="sw"></span>${p.public ? 'Public' : 'Private'}</button></div>
      </div>
      <p class="note-line">${p.mode === 'fixed' ? 'Fixed: built once, then yours to edit — lock, remove. It only changes when you rebuild it.'
        : 'Rotating: rebuilt on schedule into the <b>same</b> Spotify playlist, so its link and followers stay. Locked tracks stay put; songs you remove stay out.'}</p>
      <div class="esec"><div class="esec__h"><span class="lbl">Ingredients <span class="dim">· share of the playlist</span></span>
        <span class="addmenu"><button class="btn btn--go" type="button" data-mu-add aria-expanded="false">${ico('plus')}Add</button></span></div>${ings}</div>
      <div class="esec"><span class="lbl" style="margin-bottom:12px">Rules</span><div class="rules">
        <label><span class="lbl">Length</span><select class="fld" data-mu-rule="len">${opt([10, 15, 20, 25, 30, 40, 50, 75, 100].map(n => [n, `${n} tracks`]), r.len)}</select></label>
        <label><span class="lbl">Max per artist</span><select class="fld" data-mu-rule="per">${opt([[1, '1'], [2, '2'], [3, '3'], [4, '4'], [5, '5'], [0, 'no limit']], r.per)}</select></label>
        <div class="wide"><span class="lbl">Familiar ↔ new <span class="dim" id="mu-famlbl">· ${famLabel}</span></span>
          <input type="range" min="0" max="100" step="5" value="${r.fam}" data-mu-rule="fam" aria-label="Familiar versus new">
          <div class="slider-ends"><span>Songs you know</span><span>Never heard</span></div></div>
        <label><span class="lbl">Skip if played in the last</span><select class="fld" data-mu-rule="skip_days">${opt([[0, 'don’t skip'], [7, '7 days'], [14, '14 days'], [30, '30 days'], [90, '90 days']], r.skip_days)}</select></label>
        <label><span class="lbl">No repeats for</span><select class="fld" data-mu-rule="norep"${p.mode === 'fixed' ? ' disabled' : ''}>${opt([[0, 'off'], [2, '2 rotations'], [3, '3 rotations'], [4, '4 rotations'], [8, '8 rotations'], [12, '12 rotations']], r.norep)}</select></label>
        <label class="wide"><span class="lbl">Order</span><select class="fld" data-mu-rule="order">${opt([['spread', 'Shuffle, artists spread out'], ['shuffle', 'Plain shuffle'], ['plays', 'Most played first'], ['chrono', 'Oldest plays first'], ['setlist', 'As picked (setlist order)']], r.order)}</select></label>
      </div></div>
      ${p.last_error ? `<p class="err-line">⚠ Last save failed: ${esc(p.last_error)}</p>` : ''}
      <div class="efoot">
        <button class="btn" type="button" data-mu-build>${ico('refresh')}Rebuild</button>
        <button class="btn btn--solid" type="button" data-mu-save${spotifyReady() ? '' : ' disabled title="Connect Spotify first (Settings → Music page)"'}>${ico('save')}Save to Spotify</button>
        <span class="note">${saved}${next}</span>
      </div>
    </div></div></section>`;
}

function trackHtml(t, i) {
  const missing = spotifyReady() && !t.uri;
  return `<div class="trk${t.locked ? ' is-locked' : ''}${missing ? ' trk--miss' : ''}" data-i="${i}">
    <span class="trk__n">${missing ? '?' : i + 1}</span>${art(t.artist, caa(t.mbid))}
    <div style="min-width:0"><div class="trk__t">${esc(t.title)} <span>· ${esc(t.artist)}</span></div><div class="why">${esc(missing ? 'not found on Spotify — find it, or leave it out' : t.why)}</div></div>
    <span class="src src--${esc(t.src)}">${esc(SRC_LABEL[t.src] || t.src)}</span>
    <span class="trk__acts">
      ${missing ? `<button class="btn" type="button" data-mu-find="${i}">Find…</button>` : `<button class="btn btn--icon" type="button" data-mu-playtrk="${i}" title="Play from here">${ico('play')}</button>`}
      <button class="btn btn--icon${t.locked ? ' is-on' : ''}" type="button" data-mu-trk="${t.locked ? 'unlock' : 'lock'}" data-pos="${i}" title="${t.locked ? 'Unlock' : 'Lock — keep it through rebuilds'}">${ico('lock')}</button>
      <button class="btn btn--icon btn--stop" type="button" data-mu-trk="remove" data-pos="${i}" title="Remove — and leave it out of this playlist from now on">${ico('x')}</button></span></div>
    ${cands[i] ? `<div class="cands">${cands[i] === 'loading' ? '<span class="spin"></span>' : cands[i].length ? cands[i].map((c, j) => `<button type="button" class="cand" data-mu-pick="${i}:${j}">${ico('check')}${esc(c.name)} · ${esc(c.artist)}<span class="mono">${esc(c.album)}${c.year ? ` · ${esc(c.year)}` : ''}</span></button>`).join('') : '<span class="note-line">Nothing close on Spotify.</span>'}
      <button type="button" class="cand" data-mu-trk="remove" data-pos="${i}">${ico('x')}Leave it out</button></div>` : ''}`;
}

function previewHtml(p) {
  const missing = spotifyReady() ? p.tracks.filter(t => !t.uri).length : 0;
  const body = p.tracks.length ? p.tracks.map(trackHtml).join('')
    : `<div class="mu-empty">${ico('list')}<b>Nothing in it yet</b>${p.ings.length ? 'Press Rebuild to pick some songs.' : 'Add an ingredient, then Rebuild.'}</div>`;
  return `<section class="card mcard"><div class="card__in">
    <div class="mh"><span class="hud">Preview</span><span class="meta">${p.tracks.length} tracks${missing ? ` · ${missing} not on Spotify` : ''}${p.built_at ? ` · built ${whenRel(p.built_at)}` : ''}</span>
      <span class="tools">${p.tracks.length ? `<button class="btn" type="button" data-mu-playpl${spotifyReady() ? '' : ' disabled'}>${ico('play')}Play</button>` : ''}</span></div>
    ${missing ? `<div class="missbox">${missing} song${missing === 1 ? '' : 's'} not matched on Spotify — they’re left out when saving. Use Find… to pick the right one.</div>` : ''}
    <div class="tlist">${body}</div></div></section>`;
}
function renderPreview() { const el = $('#mu-preview', root); if (el && cur) el.innerHTML = previewHtml(cur); }

// Design edits save themselves (debounced); only the bits that changed are redrawn, so typing is never interrupted.
let saveTimer;
function designChanged(redraw = false) {
  clearTimeout(saveTimer);
  const el = $('#mu-saving', root); if (el) el.textContent = 'saving…';
  if (redraw) renderPanel();
  saveTimer = setTimeout(async () => {
    const body = { name: cur.name, mode: cur.mode, schedule: cur.schedule, public: cur.public, ings: cur.ings, rules: cur.rules };
    try {
      const saved = await api.put(`${API}/playlists/${cur.id}`, body);
      Object.assign(cur, { ...saved, tracks: cur.tracks });
      saved.summaries.forEach((s, k) => { const el2 = $(`[data-mu-sum="${k}"]`, root); if (el2) el2.textContent = s; });
      const note = $('#mu-saving', root); if (note) note.textContent = 'saved';
      const p = ov.playlists.find(x => x.id === cur.id); if (p) Object.assign(p, { name: saved.name, mode: saved.mode, schedule: saved.schedule, public: saved.public });
      renderStrip();
    } catch (e) { toast(e.message); const note = $('#mu-saving', root); if (note) note.textContent = 'not saved'; }
  }, 600);
}

function newPlaylistDialog() {
  const s = document.createElement('div'); s.className = 'scrim mu-scope';
  s.innerHTML = `<div class="card mu-modal" role="dialog" aria-label="New playlist"><div class="card__in">
    <div class="mh"><span class="hud">New playlist · start from</span><span class="tools"><button class="btn btn--icon" type="button" data-close aria-label="Close">${ico('x')}</button></span></div>
    <div class="tgrid">${ov.templates.map(t => `<button type="button" class="tpl" data-tpl="${esc(t.id)}"><b>${esc(t.id === 'blank' ? 'Blank' : t.name)}</b><span class="dim">${esc(t.blurb)}</span>
      <span class="srcs">${t.kinds.map(k => { const d = ing(k); return d ? `<span class="src src--${scVar(d.src)}">${esc(d.name)}</span>` : ''; }).join('')}</span></button>`).join('')}</div></div></div>`;
  document.body.append(s);
  const close = () => { s.remove(); document.removeEventListener('keydown', esc_); };
  const esc_ = e => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', esc_);
  s.addEventListener('click', async e => {
    if (e.target === s || e.target.closest('[data-close]')) { close(); return; }
    const b = e.target.closest('[data-tpl]'); if (!b) return;
    close();
    const made = await call(() => api.post(`${API}/playlists`, { template: b.dataset.tpl }));
    if (!made) return;
    await loadOverview(); cur = made; selected = made.id; store.set('mu-pl', made.id); renderPanel();
    if (made.ings.length) build(null, `Drafted ${ov.settings.prefix}${made.name} — nothing is on Spotify until you save it`);
  });
}

async function build(btn, msg) {
  busy(btn, true);
  const r = await call(() => api.post(`${API}/playlists/${cur.id}/build`));
  busy(btn, false);
  if (!r) return;
  cur = r.playlist; Object.keys(cands).forEach(k => delete cands[k]);
  renderPreview(); await loadOverview(); renderStrip();
  toast(r.errors.length ? `Built ${r.tracks} tracks — ${r.errors.join('; ')}` : msg || `Built ${r.tracks} tracks${r.missing && spotifyReady() ? `, ${r.missing} not on Spotify` : ''} — nothing changes on Spotify until you save`);
}

// ---- Discover --------------------------------------------------------------------------------------------------------------
async function loadDiscover() {
  try { disc.data = await api.get(`${API}/discover?basis=${encodeURIComponent(disc.basis)}`); } catch (e) { toast(e.message); return; }
  if (tab === 'discover') $('#mu-panel').innerHTML = discoverHtml();
}

function discoverHtml() {
  const d = disc.data;
  const bases = d?.bases || { '12m': 'your last 12 months' };
  const head = `<div class="dtool">${[['all', 'All'], ['never', 'Never played'], ['barely', 'Barely tried']].map(([v, l]) => `<button class="fchip" type="button" data-mu-dfilter="${v}" aria-pressed="${disc.filter === v}">${l}</button>`).join('')}
    <span class="grow"><span class="meta">${d?.running ? '<span class="spin"></span> finding artists — a minute or two' : d?.result ? `worked out ${whenRel(d.result.built_at)}` : ''}</span>
      <select class="fld" data-mu-basis aria-label="Based on">${opt(Object.entries(bases).map(([k, v]) => [k, `Based on: ${v}`]), disc.basis)}</select>
      <button class="btn" type="button" data-mu-drefresh${d?.running ? ' disabled' : ''}>${ico('refresh')}Refresh</button></span></div>`;
  if (!d) return head + '<div class="mu-empty"><span class="spin"></span></div>';
  const errs = d.result?.errors?.length ? `<p class="err-line">${esc(d.result.errors.join(' · '))}</p>` : '';
  const list = (d.result?.artists || []).filter(a => disc.filter === 'all' || (disc.filter === 'never' ? !a.plays : a.plays > 0));
  const following = new Set(d.following);
  const norm = s => s.toLowerCase().replace(/&/g, 'and').replace(/^the\s+/, '').replace(/[^\p{L}\p{N}]+/gu, '');
  const songs = a => (a.tracks || []).map(t => (typeof t === 'string' ? { title: t, uri: '' } : t));      // older results carried titles only
  const cards = list.length ? `<div class="dgrid">${list.map(a => {
    const fol = following.has(norm(a.artist)), tracks = songs(a);
    return `<article class="card dc" data-mu-artist="${esc(a.artist)}"><div class="card__in">
      <div class="dc__top">${art(a.artist, '')}<div><div class="dc__name">${esc(a.artist)}</div>
        <div class="match"><span class="match__bar"><i style="width:${a.match}%"></i></span>${a.match}% match · ${a.plays ? plays(a.plays) : 'never played'}</div></div></div>
      <div class="reasons">${a.reasons.map(r => `<div class="reason"><span class="src src--${r.src}">${{ lfm: 'last.fm', mb: 'musicbrainz', you: 'your data' }[r.src] || r.src}</span><span>${esc(r.text)}</span></div>`).join('')}</div>
      ${tracks.length ? `<div><span class="lbl">Tracks to try</span><div class="try">${tracks.map(t => `<button type="button" data-mu-try="${esc(t.title)}" data-uri="${esc(t.uri)}">${ico('play')}${esc(t.title)}</button>`).join('')}</div></div>` : ''}
      <div class="dc__acts">
        <button class="btn" type="button" data-mu-playartist data-uris="${esc(tracks.map(t => t.uri).filter(Boolean).join(' '))}">${ico('play')}Play</button>
        <button class="btn${fol ? ' is-on' : ''}" type="button" data-mu-follow="${fol ? 'undo' : 'follow'}" title="Releases, Vinyl radar and Gig radar start watching them">${ico(fol ? 'check' : 'userplus')}${fol ? 'Following' : 'Follow'}</button>
        <button class="btn btn--icon btn--stop" type="button" data-mu-nope title="Not for me — don’t suggest them again">${ico('x')}</button></div></div></article>`;
  }).join('')}</div>`
    : `<div class="mu-empty">${ico('spark')}<b>${d.running ? 'Finding artists…' : d.result ? 'Nothing to suggest' : 'Not worked out yet'}</b>${d.running ? 'This takes a minute or two the first time.' : d.result ? 'Try a different basis, or Refresh.' : ''}</div>`;
  return head + errs + cards;
}

// ---- Rediscover: artists you played far more than you do now ------------------------------------------------------------
async function loadRediscover() {
  try { redis = await api.get(`${API}/rediscover`); } catch (e) { toast(e.message); return; }
  if (tab === 'rediscover') $('#mu-panel').innerHTML = rediscoverHtml();
}

function rediscoverHtml() {
  if (!redis) return '<div class="mu-empty"><span class="spin"></span></div>';
  if (!redis.artists.length) {
    return `<div class="mu-empty">${ico('history')}<b>${redis.available ? 'Nobody to rediscover' : 'Music Companion’s database wasn’t found'}</b>${redis.available ? 'Everyone you used to play a lot, you still do.' : ''}</div>`;
  }
  return `<div class="dhead" style="margin-top:0"><h2>Rediscover</h2><p>artists you played far more than you do now — a mix is their most played from back then, plus songs of theirs you never got to</p></div>
    <div class="redis">${redis.artists.map(r => {
    const max = Math.max(...r.years.map(y => y.n));
    return `<div class="rd cut"><div class="rd__name">${esc(r.artist)}</div>
      <button class="btn btn--go" type="button" data-mu-remix="${esc(r.artist)}">${ico('refresh')}Rediscover mix</button>
      <div class="spark">${r.years.map(y => `<i class="${y.n === max ? 'pk' : ''}" style="height:${Math.max(3, y.n / max * 100)}%" title="${y.y}: ${y.n} plays"></i>`).join('')}</div>
      <div class="spark-yrs">${r.years.map(y => `<span>${String(y.y).slice(2)}</span>`).join('')}</div>
      <div class="rd__sub">${r.total.toLocaleString()} plays · peaked ${r.peak} (${r.peak_n.toLocaleString()}) · ${r.recent} in the last 12 months${r.owned ? ` · ${r.owned} on your shelf` : ''}</div></div>`;
  }).join('')}</div>`;
}

// ---- Gigs -------------------------------------------------------------------------------------------------------------------
async function loadGigs() {
  try { gigsData = await api.get(`${API}/gigs`); } catch (e) { toast(e.message); return; }
  gigSel ??= gigsData.booked[0]?.id ?? null;
  const g = gigsData.booked.find(x => x.id === gigSel);
  if (g) predict(g.artist);
  if (tab === 'gigs') $('#mu-panel').innerHTML = gigsHtml();
}

async function predict(artist, force = false) {
  if (!force && predictions[artist]) return;
  if (!gigsData?.setlistfm) return;
  predictions[artist] = 'loading';
  try { predictions[artist] = await api.get(`${API}/gigs/predict?artist=${encodeURIComponent(artist)}`); }
  catch (e) { predictions[artist] = { error: e.message }; }
  if (tab === 'gigs') $('#mu-panel').innerHTML = gigsHtml();
}

const datebox = iso => { const [y, m, d] = iso.split('-'); return `<span class="datebox"><b>${Number(d)}</b><span>${MON[Number(m) - 1]}${Number(y) !== new Date().getFullYear() ? ` ${y.slice(2)}` : ''}</span></span>`; };

function gigsHtml() {
  if (!gigsData) return '<div class="mu-empty"><span class="spin"></span></div>';
  const g = gigsData.booked.find(x => x.id === gigSel);
  let hero;
  if (!g) {
    hero = `<section class="card mcard"><div class="card__in"><div class="mu-empty">${ico('bolt')}<b>No gigs booked</b>Add your tickets in Settings → Music and they’ll show here with a predicted setlist.<br><a class="btn btn--go" href="/settings?add=gig#music">Add a gig</a></div></div></section>`;
  } else {
    const p = predictions[g.artist];
    const made = gigsData.made[`gig:${g.id}`];
    let list;
    if (!gigsData.setlistfm) list = `<p class="note-line">Predicted setlists need a Setlist.fm key — <a href="/settings#listen">Settings → Music page</a>.</p>`;
    else if (!p || p === 'loading') list = '<div class="mu-empty"><span class="spin"></span></div>';
    else if (p.error) list = `<p class="err-line">${esc(p.error)}</p>`;
    else if (!p.songs.length) list = `<p class="note-line">Setlist.fm has no recent setlists for ${esc(g.artist)} yet.</p>`;
    else {
      list = `<p class="note-line">Predicted from their last <b>${p.shows} shows</b> on Setlist.fm${p.tour ? ` (${esc(p.tour)})` : ''}${p.latest ? `, most recently ${esc(p.latest.venue)}${p.latest.city ? `, ${esc(p.latest.city)}` : ''} on ${esc(dmy(p.latest.date))}` : ''}.
        A pre-gig playlist refreshes daily as new setlists appear, then switches to what they actually played.</p>` +
        p.songs.map((s, i) => `<div class="sl"><span class="sl__n">${i + 1}</span>
          <span class="sl__t"><span>${esc(s.title)}</span>${s.tag ? `<span class="chip">${esc(s.tag)}</span>` : ''}${s.cover_of ? `<span class="chip" title="Cover">${esc(s.cover_of)}</span>` : ''}</span>
          <span class="pct"><span class="pct__bar"><i style="width:${s.pct}%"></i></span>${s.pct}%</span>
          <span class="yp${s.plays ? '' : ' yp--zero'}" title="Your plays">${s.plays ? plays(s.plays) : 'never'}</span>
          <span class="yp">${s.plays < 5 ? '<span class="chip chip--warn">brush up</span>' : ''}</span>
          <button class="btn btn--icon" type="button" data-mu-trysong="${esc(s.title)}" data-artist="${esc(s.cover_of || g.artist)}" title="Play">${ico('play')}</button></div>`).join('');
    }
    hero = `<section class="card mcard"><div class="card__in"><div class="mh"><span class="hud">${g.id === gigsData.booked[0]?.id ? 'Next gig' : 'Booked'}</span><span class="meta">${esc(dmy(g.date))}</span>
      <span class="tools">${gigsData.setlistfm ? `<button class="btn btn--icon" type="button" data-mu-repredict title="Look again">${ico('refresh')}</button>` : ''}</span></div>
      <div class="mbody"><div class="ghero"><span class="hero__count">${g.in_days === 0 ? 'GIG' : g.in_days}</span><span class="hero__unit">${g.in_days === 0 ? 'tonight' : g.in_days === 1 ? 'day' : 'days'}</span>
        <div class="ghero__who"><b>${esc(g.artist)}</b><span>${esc([g.venue, g.city].filter(Boolean).join(', '))}${g.doors ? ` · doors ${esc(g.doors)}` : ''}</span></div>
        <span class="btns">${made ? `<button class="btn btn--go" type="button" data-mu-openpl="${made}">${ico('list')}Open pre-gig playlist</button>`
          : `<button class="btn btn--solid" type="button" data-mu-pregig="${g.id}"${gigsData.setlistfm ? '' : ' disabled'}>${ico('save')}Pre-gig playlist</button>`}</span></div>${list}</div></div></section>`;
  }
  const booked = gigsData.booked.length > 1 || (gigsData.booked.length && !g) ? `<section class="card mcard"><div class="card__in"><div class="mh"><span class="hud">Booked</span></div><div class="mbody glist">
    ${gigsData.booked.map(o => `<div class="row${o.id === gigSel ? ' is-sel' : ''}">${datebox(o.date)}<div class="row__main"><div class="row__t">${esc(o.artist)}</div><div class="row__s">${esc([o.venue, o.city].filter(Boolean).join(', '))} · ${o.in_days} days</div></div>
      ${o.id === gigSel ? '' : `<button class="btn" type="button" data-mu-gig="${o.id}">${ico('spark')}Setlist</button>`}</div>`).join('')}</div></div></section>` : '';
  const url = `<section class="card mcard"><div class="card__in"><div class="mh"><span class="hud">From a setlist.fm link</span></div><div class="mbody">
    <form class="urlrow" data-mu-urlform><input class="fld" name="url" type="url" required placeholder="https://www.setlist.fm/setlist/…" aria-label="setlist.fm link"><button class="btn btn--go" type="submit"${gigsData.setlistfm ? '' : ' disabled'}>${ico('link')}Make playlist</button></form>
    <p class="note-line">A fixed playlist in the show’s running order; covers are found under the original artist.</p></div></div></section>`;
  const shows = gigsData.shows.length ? `<section class="card mcard"><div class="card__in"><div class="mh"><span class="hud">Shows you’ve been to</span><span class="meta">${gigsData.shows.length} most recent</span></div><div class="mbody glist">
    ${gigsData.shows.map(s => { const made = gigsData.made[`show:${s.id}`]; return `<div class="row">${datebox(s.date)}<div class="row__main"><div class="row__t">${esc(s.artist)}</div><div class="row__s">${esc([s.venue, s.city].filter(Boolean).join(', '))}${s.tour ? ` · ${esc(s.tour)}` : ''} · ${s.songs} songs</div></div>
      ${made ? `<button class="btn btn--icon is-on" type="button" data-mu-openpl="${made}" title="Open its playlist">${ico('list')}</button>` : `<button class="btn btn--icon" type="button" data-mu-show="${s.id}" title="Make a playlist of this show"${s.songs ? '' : ' disabled'}>${ico('save')}</button>`}</div>`; }).join('')}</div></div></section>` : '';
  return `<div class="ggrid">${hero}<div class="gcol">${booked}${url}${shows}</div></div>`;
}

// ---- Releases / Vinyl: Home's own rows, with room, plus Listen ---------------------------------------------------------------
const listenBtn = (it, kind, label) => `<button class="btn" type="button" data-mu-listen="${kind}" data-artist="${esc(it.artist || '')}" data-title="${esc(it.title)}" title="${kind === 'album' ? 'Play the album on Spotify' : 'Not out yet — plays their best-known songs'}">${ico('play')}<span class="bl">${label}</span></button>`;
const isOut = it => !it.date || it.date <= (dash?.now || '').slice(0, 10);

function chips(list, current, attr) {
  return list.map(([v, l]) => `<button class="fchip" type="button" ${attr}="${v}" aria-pressed="${current === v}">${l}</button>`).join('');
}

function releasesHtml() {
  const w = dash?.widgets?.releases, d = w?.data;
  if (!d) return `<div class="mu-empty">${ico('disc')}<b>${w?.error ? 'Couldn’t sync releases' : 'Syncing…'}</b>${esc(w?.error || '')}</div>`;
  const today = dash.now.slice(0, 10);
  let list = narrow(d, { filter: rel.filter === 'out' ? 'all' : rel.filter, search: rel.search }, today);
  if (rel.filter === 'out') list = list.filter(i => (i.date || '') <= today);
  const rows = list.map((it, k) => musicRowWith(it, k, false, listenBtn(it, isOut(it) ? 'album' : 'artist', isOut(it) ? 'Listen' : 'Hear them'))).join('');
  return `<section class="card mcard mu-full" id="mu-releases"><div class="card__in"><div class="mh"><span class="hud">Releases</span><span class="meta">${d.total} albums · ${d.following} artists followed</span></div>
    <div class="maxbar">${chips([['all', 'All'], ['new', 'New'], ['pinned', 'Pinned'], ['upcoming', 'Upcoming'], ['out', 'Out now'], ['dismissed', 'Dismissed']], rel.filter, 'data-mu-rfilter')}
      <button class="fchip fchip--action" type="button" data-mark-seen="release">Mark all seen</button><input class="fld mu-search" type="search" placeholder="Filter…" value="${esc(rel.search)}" data-mu-rsearch aria-label="Filter"></div>
    <ul class="rows">${rows || `<li class="mu-empty">Nothing matches.</li>`}</ul>
    <div class="fullnote">The same releases as Home’s card — pins and dismissals are shared. Listen plays the album on Spotify; before release day it plays the artist’s best-known songs.</div></div></section>`;
}

function vinylHtml() {
  const w = dash?.widgets?.vinyl, d = w?.data;
  if (!d) return `<div class="mu-empty">${ico('radar')}<b>${w?.error ? 'Couldn’t sync the vinyl radar' : 'Syncing…'}</b>${esc(w?.error || '')}</div>`;
  const today = dash.now.slice(0, 10);
  let body, bar2 = '';
  if (vin.mode === 'pre') {
    const list = d.preorders || [];
    body = list.length ? list.map(it => preorderRowWith(it, listenBtn(it, isOut(it) ? 'album' : 'artist', 'Listen'))).join('') : '<li class="mu-empty">No confirmed pre-orders yet.</li>';
  } else {
    const list = narrow(d, { filter: vin.filter, search: '' }, today, { dated: i => !!i.date });
    body = list.map((it, k) => vinylRowWith(it, k, false, listenBtn(it, isOut(it) ? 'album' : 'artist', 'Listen first'))).join('') || '<li class="mu-empty">Nothing matches.</li>';
    bar2 = chips([['all', 'All'], ['new', 'New'], ['pinned', 'Pinned'], ['dated', 'Has date'], ['dismissed', 'Dismissed']], vin.filter, 'data-mu-vfilter') + '<button class="fchip fchip--action" type="button" data-mark-seen="vinyl">Mark all seen</button>';
  }
  return `<section class="card mcard mu-full" id="mu-vinyl"><div class="card__in"><div class="mh"><span class="hud">${vin.mode === 'pre' ? 'Confirmed pre-orders' : 'Vinyl radar'}</span><span class="meta">${vin.mode === 'pre' ? `${(d.preorders || []).length} confirmed` : `${d.total} pre-orders · ${d.stores} stores`}</span></div>
    <div class="maxbar"><span class="seg" role="group"><button type="button" data-mu-vmode="radar" aria-pressed="${vin.mode === 'radar'}">Vinyl radar</button><button type="button" data-mu-vmode="pre" aria-pressed="${vin.mode === 'pre'}">Confirmed pre-orders</button></span>${bar2}</div>
    <ul class="rows">${body}</ul>
    <div class="fullnote">The same records as Home’s Vinyl radar — purchases, pins and dismissals are shared. “Listen first” plays the album, or before release day the artist’s best-known songs.</div></div></section>`;
}

// ---- the player bar --------------------------------------------------------------------------------------------------------
async function pollPlayer() {
  if (!started) return;
  try { pl = await api.get(`${API}/player`); plAt = Date.now(); } catch { /* keep the last reading */ }
  renderBar();
  if (bar === 'hidden') renderTop();
}

function tickProgress() {
  if (!active || !pl?.active || !pl.playing) return;
  const pos = Math.min(pl.track.duration, pl.progress + (Date.now() - plAt));
  const pct = pl.track.duration ? pos / pl.track.duration * 100 : 0;
  const fill = $('#mu-bar .prog__bar i'); if (fill) fill.style.width = `${pct}%`;
  const t = $('#mu-bar [data-mu-pos]'); if (t) t.textContent = mmss(pos);
  $('#mu-bar')?.style.setProperty('--p', `${pct}%`);
}

function sizer() {
  return `<span class="sizer">${bar === 'full' ? `<button class="btn btn--icon" type="button" data-mu-bar="compact" title="Make the player smaller">${ico('minus')}</button>`
    : `<button class="btn btn--icon" type="button" data-mu-bar="full" title="Full player">${ico('expand')}</button>`}
    <button class="btn btn--icon" type="button" data-mu-bar="hidden" title="Hide the player (it stays in the header)">${ico('x')}</button></span>`;
}

function renderBar() {
  const el = $('#mu-bar'); if (!el) return;
  $('.mu', root).dataset.bar = bar;
  el.classList.remove('mu-bar--msg');
  if (!pl) { el.innerHTML = ''; return; }
  if (!pl.ready || !pl.active) {
    el.classList.add('mu-bar--msg');
    const msg = !pl.ready ? `<span><b>${esc(pl.why || 'Spotify isn’t connected')}.</b> Set it up in Settings → Music page.</span>`
      : pl.error ? `<span><b>Spotify:</b> ${esc(pl.error)}</span>` : '<span><b>Nothing playing.</b> Start something here, or open Spotify on this PC.</span>';
    el.innerHTML = `<div class="off">${ico('desk')}${msg}</div><div class="acts">${!pl.ready ? '<a class="btn btn--go" href="/settings#listen">Settings</a>'
      : `<span class="dev"><button class="btn dev__btn" type="button" data-mu-devices>${ico('phone')}<span>Devices</span></button>${devMenuHtml()}</span><button class="btn btn--solid" type="button" data-mu-open>${ico('play')}Open Spotify</button>`}${sizer()}</div>`;
    return;
  }
  const t = pl.track, pos = Math.min(t.duration, pl.progress + (pl.playing ? Date.now() - plAt : 0));
  el.style.setProperty('--p', `${t.duration ? pos / t.duration * 100 : 0}%`);
  el.innerHTML = `<div class="np">${art(t.artists[0] || t.name, t.image)}<div class="np__main"><div class="np__t">${t.url ? `<a href="${esc(safeUrl(t.url))}" target="_blank" rel="noopener">${esc(t.name)}</a>` : esc(t.name)}</div>
      <div class="np__a">${esc(t.artists.join(', '))}${t.album ? ` · ${esc(t.album)}` : ''}</div>${pl.fact ? `<div class="np__fact">${esc(pl.fact)}</div>` : ''}</div>
      <button class="btn btn--icon${pl.liked ? ' is-on' : ''}" type="button" data-mu-cmd="like" data-value="${!pl.liked}" title="${pl.liked ? 'Remove from' : 'Save to'} your library">${ico('heart')}</button></div>
    <div class="ctl"><div class="ctl__b">
      <button class="btn btn--icon${pl.shuffle ? ' is-on' : ''}" type="button" data-mu-cmd="shuffle" data-value="${!pl.shuffle}" title="Shuffle">${ico('shuffle')}</button>
      <button class="btn btn--icon" type="button" data-mu-cmd="previous" title="Previous">${ico('prev')}</button>
      <button class="btn btn--icon btn--play" type="button" data-mu-cmd="${pl.playing ? 'pause' : 'play'}" title="${pl.playing ? 'Pause' : 'Play'}">${ico(pl.playing ? 'pause' : 'play')}</button>
      <button class="btn btn--icon" type="button" data-mu-cmd="next" title="Next">${ico('next')}</button>
      <button class="btn btn--icon${pl.repeat !== 'off' ? ' is-on' : ''}" type="button" data-mu-cmd="repeat" data-value="${{ off: 'context', context: 'track', track: 'off' }[pl.repeat] || 'off'}" title="Repeat: ${esc(pl.repeat)}">${ico('repeat')}</button></div>
      <div class="prog"><span data-mu-pos>${mmss(pos)}</span><span class="prog__bar" data-mu-seek><i style="width:${t.duration ? pos / t.duration * 100 : 0}%"></i></span><span>${mmss(t.duration)}</span></div></div>
    <div class="side"><span class="dev"><button class="btn dev__btn is-live" type="button" data-mu-devices title="Playing on ${esc(pl.device.name)}">${ico(pl.device.type === 'Smartphone' ? 'phone' : 'desk')}<span>${esc(pl.device.name)}</span></button>${devMenuHtml()}</span>
      ${pl.device.volume != null ? `${ico('vol')}<input class="vol" type="range" min="0" max="100" value="${pl.device.volume}" data-mu-vol aria-label="Volume">` : ''}${sizer()}</div>`;
}

function devMenuHtml() {
  if (!devMenu) return '';
  if (devMenu === 'loading') return '<div class="dev__menu"><span class="spin"></span></div>';
  return `<div class="dev__menu">${devMenu.map(d => `<button type="button" data-mu-transfer="${esc(d.id)}"${d.active ? ' aria-current="true"' : ''}>${ico(d.type === 'Smartphone' ? 'phone' : 'desk')}<span>${esc(d.name)}<small>${esc(d.type)}${d.active ? ' · playing here' : ''}</small></span></button>`).join('') || '<small style="padding:8px">No devices — open Spotify somewhere.</small>'}
    <hr><button type="button" data-mu-open>${ico('play')}<span>Open Spotify on this PC<small>starts the desktop app</small></span></button></div>`;
}

async function command(cmd, value) {
  try { await api.post(`${API}/player/${cmd}`, { value }); } catch (e) { toast(e.message); }
  setTimeout(pollPlayer, 350);
}

async function playWhat(body, label) {
  const r = await call(() => api.post(`${API}/play`, body));
  if (r) { toast(`Playing ${r.what || label}`); setTimeout(pollPlayer, 600); }
}

// ---- events ---------------------------------------------------------------------------------------------------------------
root.addEventListener('click', async e => {
  const b = e.target.closest('button, [data-mu-seek]');
  if (!b) { if (!e.target.closest('.addmenu, .dev')) closeMenus(); return; }
  const d = b.dataset;
  if (!b.closest('.addmenu')) $$('.addmenu__list', root).forEach(m => m.remove());
  if (!b.closest('.dev') && devMenu) { devMenu = null; renderBar(); }

  if (d.muTab) { tab = d.muTab; store.set('mu-tab', tab); renderPanel(); $('#mu-panel').scrollTop = 0; return; }
  // playlists
  if (d.muPl) { await loadPlaylist(Number(d.muPl)); return; }
  if ('muNew' in d) { newPlaylistDialog(); return; }
  if (d.muMode) { cur.mode = d.muMode; if (cur.mode === 'rot' && !cur.schedule.every) cur.schedule = { every: 'weekly', day: 0, at: ov.settings.rotate_at }; designChanged(true); return; }
  if ('muPublic' in d) { cur.public = !cur.public; b.setAttribute('aria-checked', String(cur.public)); b.lastChild.textContent = cur.public ? 'Public' : 'Private'; designChanged(); return; }
  if (d.muIngx) { cur.ings.splice(Number(d.muIngx), 1); cur.summaries?.splice(Number(d.muIngx), 1); designChanged(true); return; }
  if ('muAdd' in d) {
    const box = b.parentElement, open = $('.addmenu__list', box);
    if (open) { open.remove(); return; }
    box.insertAdjacentHTML('beforeend', `<div class="addmenu__list">${ov.ingredients.map(i => `<button type="button" data-mu-addi="${i.id}" style="--sc: var(--src-${scVar(i.src)})"${i.ready ? '' : ` disabled title="${esc(i.why)}"`}>${ico(i.icon)}${esc(i.name)}<small>${esc(i.ready ? i.blurb : `${i.blurb} — ${i.why}`)}</small></button>`).join('')}</div>`);
    return;
  }
  if (d.muAddi) { const def = ing(d.muAddi); cur.ings.push({ kind: def.id, share: 30, params: structuredClone(def.defaults) }); cur.summaries = [...(cur.summaries || []), '']; designChanged(true); toast(`Added ${def.name} — Rebuild to hear it`); return; }
  if ('muBuild' in d) { clearTimeout(saveTimer); await flushDesign(); build(b); return; }
  if ('muSave' in d) {
    await flushDesign(); busy(b, true);
    const r = await call(() => api.post(`${API}/playlists/${cur.id}/save`));
    busy(b, false);
    if (r) { cur = r.playlist; await loadOverview(); renderPanel(); toast(`Saved ${ov.settings.prefix}${cur.name} to Spotify — ${r.tracks} tracks${r.skipped ? `, ${r.skipped} not found there` : ''}`); }
    return;
  }
  if ('muDelete' in d) {
    if (!confirm(`Delete “${cur.name}” from Citadel?${cur.spotify_id ? ' The playlist stays on your Spotify account — delete it there if you don’t want it.' : ''}`)) return;
    if (await call(() => api.del(`${API}/playlists/${cur.id}`), 'Deleted')) { cur = null; selected = null; await loadOverview(); if (ov.playlists[0]) await loadPlaylist(ov.playlists[0].id, false); renderPanel(); }
    return;
  }
  if (d.muTrk) {
    const r = await call(() => api.post(`${API}/playlists/${cur.id}/tracks`, { op: d.muTrk, pos: Number(d.pos) }));
    if (r) { cur = r; delete cands[d.pos]; renderPreview(); }
    return;
  }
  if (d.muFind) {
    const i = Number(d.muFind), t = cur.tracks[i];
    if (cands[i]) { delete cands[i]; renderPreview(); return; }
    cands[i] = 'loading'; renderPreview();
    const r = await call(() => api.get(`${API}/candidates?artist=${encodeURIComponent(t.artist)}&title=${encodeURIComponent(t.title)}`));
    cands[i] = r?.items || []; renderPreview();
    return;
  }
  if (d.muPick) {
    const [i, j] = d.muPick.split(':').map(Number), c = cands[i][j];
    const r = await call(() => api.post(`${API}/playlists/${cur.id}/match`, { pos: i, uri: c.uri, name: c.name, artist: c.artist, album: c.album }), 'Matched — remembered for every playlist');
    if (r) { cur = r; delete cands[i]; renderPreview(); }
    return;
  }
  if (d.muPlaytrk) { playWhat({ kind: 'playlist', playlist_id: cur.id, start: Number(d.muPlaytrk) }, cur.name); return; }
  if ('muPlaypl' in d) { playWhat({ kind: 'playlist', playlist_id: cur.id }, cur.name); return; }
  if (d.muOpenpl) { tab = 'playlists'; store.set('mu-tab', tab); await loadPlaylist(Number(d.muOpenpl), false); renderPanel(); return; }
  // discover
  if (d.muDfilter) { disc.filter = d.muDfilter; $('#mu-panel').innerHTML = discoverHtml(); return; }
  if ('muDrefresh' in d) { await call(() => api.post(`${API}/discover/refresh?basis=${encodeURIComponent(disc.basis)}`)); await loadDiscover(); return; }
  const artistCard = b.closest('[data-mu-artist]');
  if (d.muTry && artistCard) { playWhat(d.uri ? { kind: 'uris', uris: [d.uri] } : { kind: 'track', artist: artistCard.dataset.muArtist, title: d.muTry }, d.muTry); return; }
  if ('muPlayartist' in d) { playWhat(d.uris ? { kind: 'uris', uris: d.uris.split(' ') } : { kind: 'artist', artist: artistCard.dataset.muArtist }, artistCard.dataset.muArtist); return; }
  if (d.muFollow) {
    const artist = artistCard.dataset.muArtist;
    if (await call(() => api.post(`${API}/discover/feedback`, { artist, verdict: d.muFollow === 'undo' ? null : 'follow' }), d.muFollow === 'undo' ? `Stopped following ${artist} here — unfollow fully in Settings → Music` : `Following ${artist} — Releases, Vinyl radar and Gig radar will watch them`)) loadDiscover();
    return;
  }
  if ('muNope' in d) {
    const artist = artistCard.dataset.muArtist;
    artistCard.classList.add('is-gone');
    await call(() => api.post(`${API}/discover/feedback`, { artist, verdict: 'nope' }), `Got it — ${artist} won’t be suggested again`);
    return;
  }
  if (d.muRemix) {
    busy(b, true);
    const made = await call(() => api.post(`${API}/discover/rediscover`, { artist: d.muRemix }));
    busy(b, false);
    if (made) { await loadOverview(); cur = made; selected = made.id; store.set('mu-pl', made.id); tab = 'playlists'; store.set('mu-tab', tab); renderPanel(); toast(`Drafted ${ov.settings.prefix}${made.name} — save it to put it on Spotify`); }
    return;
  }
  // gigs
  if (d.muGig) { gigSel = Number(d.muGig); const g = gigsData.booked.find(x => x.id === gigSel); $('#mu-panel').innerHTML = gigsHtml(); predict(g.artist); return; }
  if ('muRepredict' in d) { const g = gigsData.booked.find(x => x.id === gigSel); predict(g.artist, true); return; }
  if (d.muPregig) {
    busy(b, true);
    const made = await call(() => api.post(`${API}/gigs/pregig`, { gig_id: Number(d.muPregig) }));
    busy(b, false);
    if (made) { gigsData = null; await loadOverview(); cur = made; selected = made.id; store.set('mu-pl', made.id); tab = 'playlists'; store.set('mu-tab', tab); renderPanel(); toast(`${ov.settings.prefix}${made.name} — refreshes daily until the show. Save it to put it on Spotify.`); }
    return;
  }
  if (d.muShow) { setlistPlaylist(b, { show_id: Number(d.muShow) }); return; }
  if (d.muTrysong) { playWhat({ kind: 'track', artist: d.artist, title: d.muTrysong }); return; }
  // releases / vinyl
  if (d.muRfilter) { rel.filter = d.muRfilter; renderPanel(); return; }
  if (d.muVfilter) { vin.filter = d.muVfilter; renderPanel(); return; }
  if (d.muVmode) { vin.mode = d.muVmode; renderPanel(); return; }
  if (d.muListen) { playWhat({ kind: d.muListen, artist: d.artist, title: d.title }, d.title); return; }
  // player
  if (d.muBar) { bar = d.muBar; store.set('mu-bar', bar); renderBar(); renderTop(); return; }
  if (d.muCmd) { command(d.muCmd, d.value === undefined ? null : d.value === 'true' ? true : d.value === 'false' ? false : d.value); return; }
  if ('muDevices' in d) {
    if (devMenu) { devMenu = null; renderBar(); return; }
    devMenu = 'loading'; renderBar();
    const r = await call(() => api.get(`${API}/player/devices`));
    devMenu = r ? r.devices : null; renderBar();
    return;
  }
  if (d.muTransfer) { devMenu = null; command('transfer', d.muTransfer); return; }
  if ('muOpen' in d) {
    devMenu = null; busy(b, true); toast('Starting Spotify…');
    const r = await call(() => api.post(`${API}/player-open`));
    busy(b, false);
    if (r) toast(`Spotify is ready on ${r.device}`);
    pollPlayer();
    return;
  }
  if ('muSeek' in d && pl?.active) {
    const r = b.getBoundingClientRect();
    command('seek', Math.round((e.clientX - r.left) / r.width * pl.track.duration));
  }
});

function closeMenus() { $$('.addmenu__list', root).forEach(m => m.remove()); if (devMenu) { devMenu = null; renderBar(); } }

async function flushDesign() {
  if (!saveTimer) return;
  clearTimeout(saveTimer); saveTimer = null;
  try { const saved = await api.put(`${API}/playlists/${cur.id}`, { name: cur.name, mode: cur.mode, schedule: cur.schedule, public: cur.public, ings: cur.ings, rules: cur.rules }); Object.assign(cur, { ...saved, tracks: cur.tracks }); }
  catch (e) { toast(e.message); }
}

async function setlistPlaylist(btn, body) {
  busy(btn, true);
  const r = await call(() => api.post(`${API}/gigs/setlist-playlist`, body));
  busy(btn, false);
  if (!r) return;
  gigsData = null; await loadOverview();
  cur = r.playlist; selected = cur.id; store.set('mu-pl', cur.id); tab = 'playlists'; store.set('mu-tab', tab); renderPanel();
  toast(r.saved ? `Saved ${cur.spotify_name} to Spotify — ${r.saved.tracks} tracks` : `Made ${cur.name}${r.errors.length ? ` — ${r.errors.join('; ')}` : ''}`);
}

root.addEventListener('submit', e => {
  const f = e.target.closest('[data-mu-urlform]');
  if (!f) return;
  e.preventDefault();
  setlistPlaylist(f.querySelector('button'), { url: f.elements.url.value.trim() });
});

let volTimer;
root.addEventListener('input', e => {
  const el = e.target, d = el.dataset;
  if (d.muField === 'name') { cur.name = el.value; designChanged(); return; }
  if (d.muShare !== undefined) { cur.ings[Number(d.muShare)].share = Number(el.value); el.nextElementSibling.textContent = `${el.value}%`; designChanged(); return; }
  if (d.muRule === 'fam') { cur.rules.fam = Number(el.value); $('#mu-famlbl').textContent = `· ${cur.rules.fam >= 90 ? 'all familiar' : cur.rules.fam <= 10 ? 'all new to you' : `${cur.rules.fam}% familiar`}`; designChanged(); return; }
  if (d.muVol !== undefined) { clearTimeout(volTimer); volTimer = setTimeout(() => command('volume', Number(el.value)), 250); return; }
  if (d.muRsearch !== undefined) { rel.search = el.value.trim().toLowerCase(); clearTimeout(volTimer); volTimer = setTimeout(() => { renderPanel(); const box = $('[data-mu-rsearch]', root); box?.focus(); box?.setSelectionRange(box.value.length, box.value.length); }, 250); }
});

root.addEventListener('change', e => {
  const el = e.target, d = el.dataset;
  if (d.muRule && d.muRule !== 'fam') { cur.rules[d.muRule] = d.muRule === 'order' ? el.value : Number(el.value); designChanged(); return; }
  if (d.muSched) { cur.schedule = { ...cur.schedule, [d.muSched]: d.muSched === 'at' ? el.value : d.muSched === 'every' ? el.value : Number(el.value) };
    if (d.muSched === 'every') cur.schedule.day = el.value === 'monthly' ? 1 : 0;
    designChanged(d.muSched === 'every'); return; }
  if (d.muParam) {
    const k = Number(el.closest('[data-mu-ing]').dataset.muIng), p = cur.ings[k].params;
    p[d.muParam] = d.muParam === 'names' ? el.value.split(',').map(s => s.trim()).filter(Boolean) : el.type === 'number' ? Number(el.value) : ['not_played_days', 'played_within_days', 'days'].includes(d.muParam) ? Number(el.value) : el.value;
    designChanged(d.muParam === 'kind');
    return;
  }
  if (el.matches('[data-mu-basis]')) { disc.basis = el.value; store.set('mu-basis', disc.basis); disc.data = null; $('#mu-panel').innerHTML = discoverHtml(); loadDiscover(); }
});

document.addEventListener('keydown', e => { if (e.key === 'Escape' && active) closeMenus(); });
