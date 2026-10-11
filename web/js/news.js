// The News page (#news): a Front page (the BBC's Most read top 10 and each section's lead stories), one tab per section
// (its latest stories, merged and ranked), and an Archive of everything still kept. Stories come from /api/owner/news,
// never the dashboard state; app.js tells this module when the page is shown (onView). A 'widget' event for news,
// mostread or f1cal says something new arrived.
//
// 👍/👎 teach the page (Settings → News → What I've learned); 👎 also offers one-tap "Never show" rules. F1 stories the
// spoiler shield holds back arrive without a headline at all — there is nothing here to leak.
//
// Never reuse [data-act] (project start/stop) or [data-date] (the Now card's clock sweep) — app.js and util.js treat
// those names globally. Everything here is [data-nw-*].
import * as api from './api.js';
import { $, $$, esc, ico, safeUrl, toast } from './util.js';

const API = '/api/owner/news';
const root = $('#view-news');
const ICONS = { front: 'news', world: 'globe', uk: 'home', tech: 'chip', science: 'spark', music: 'disc', archive: 'history' };
const VINYL_WORDS = /vinyl|\bLPs?\b|reissue|repress|pressing|variant|colou?red|box ?set/i;
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private window: just not remembered */ } },
};

let active = false, started = false;
let ov = null;                                   // GET /api/owner/news: sections, shield, last sync
let tab = store.get('nw-tab', 'front');
let view = null;                                 // what the current tab last loaded
const arch = { section: '', source: '', q: '', saved: false, liked: false, unread: false, items: [], next: null, sections: [] };
const musicUi = { band: '', vinyl: false, everything: store.get('nw-everything', '') === '1' };   // Music: your artists only, or everything
let downOpen = null;                             // the story whose "Never show" choices are showing after a 👎
let streamDown = false;

// ---- small helpers ------------------------------------------------------------------------------------------------------
const pad = n => String(n).padStart(2, '0');
const hm = epoch => { const d = new Date(epoch * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
const dayName = epoch => { const d = new Date(epoch * 1000); return `${DAYS[d.getDay()]} ${d.getDate()} ${MON[d.getMonth()]}`; };
function age(epoch) {
  if (!epoch) return '';
  const s = Date.now() / 1000 - epoch;
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  if (s < 7 * 86400) return `${Math.round(s / 86400)}d`;
  return dayName(epoch);
}
const sections = () => (ov?.sections || []);
const tabs = () => [['front', 'Front'], ...sections().map(s => [s.id, s.name]), ['archive', 'Archive']];

async function call(fn) { try { return await fn(); } catch (e) { toast(e.message); return null; } }

// ---- lifecycle ------------------------------------------------------------------------------------------------------------
export function onView(v) {
  const was = active;
  active = v === 'news';
  if (active && !started) { started = true; boot(); }
  else if (active && !was) refresh();
}

async function boot() {
  root.innerHTML = `<div class="nw"><div class="nw__top"><nav class="mtabs" id="nw-tabs" aria-label="News sections"></nav><span class="nw-status"><span id="nw-mini"></span><span id="nw-status"></span></span></div>
    <div id="nw-shield"></div><div class="nw__panel" id="nw-panel"><div class="nw-empty"><span class="spin"></span></div></div></div>`;
  root.addEventListener('click', onClick);
  root.addEventListener('change', onChange);
  root.addEventListener('input', onInput);
  root.addEventListener('error', e => { if (e.target.tagName === 'IMG') e.target.closest('.st__pic')?.remove(); }, true);
  // app.js's stream, shared (api.subscribe) — one per tab, or the browser runs out of connections to Citadel
  api.subscribe(e => { let d = {}; try { d = JSON.parse(e.data); } catch { /* fine */ } if (['news', 'mostread', 'f1cal'].includes(d.id)) changed(); },
    ok => { if (!ok) streamDown = true; else if (streamDown) { streamDown = false; changed(); } }, '/api/owner/events', ['widget']);
  await refresh();
}

let evTimer;
function changed() {
  clearTimeout(evTimer);
  evTimer = setTimeout(() => {
    if (!active || downOpen != null) return;                          // never yank a list out from under an open 👎 choice
    if (tab === 'archive') { loadOverview(); return; }                  // the archive keeps its place; it refreshes when you ask
    refresh();
  }, 600);
}

async function loadOverview() {
  const r = await call(() => api.get(API));
  if (!r) return;
  ov = r;
  if (!tabs().some(t => t[0] === tab)) tab = 'front';
  renderTabs();
  renderShield(ov.shield);
  renderStatus();
}

async function refresh() {
  await loadOverview();
  await loadTab();
}

async function loadTab() {
  const panel = $('#nw-panel');
  if (!panel) return;
  if (tab === 'archive') { arch.items = []; arch.next = null; await loadArchive(); return; }
  const r = await call(() => api.get(tab === 'front' ? `${API}/front` : `${API}/section/${encodeURIComponent(tab)}${musicUi.everything ? '?everything=true' : ''}`));
  if (!r) return;
  view = r;
  renderShield(r.shield);
  renderPanel();
}

// ---- header, tabs, shield banner -------------------------------------------------------------------------------------------
function renderTabs() {
  $('#nw-tabs').innerHTML = tabs().map(([id, label]) =>
    `<button type="button" data-nw-tab="${esc(id)}" aria-pressed="${id === tab}">${ico(ICONS[id] || 'news')}${esc(label)}</button>`).join('');
}

function renderStatus() {
  const s = ov?.synced || {};
  const errs = s.feed_errors?.length ? ` · <span class="warn" title="${esc(s.feed_errors.join(', '))}">${s.feed_errors.length} feed${s.feed_errors.length === 1 ? '' : 's'} failed</span>` : '';
  $('#nw-status').innerHTML = s.at ? `<span class="dot ${s.error ? 'dot--warn' : 'dot--ok'}"></span>Updated ${esc(hm(s.at))}${errs}` : '<span class="dot"></span>Waiting for the first sync…';
}

function renderShield(sh) {
  const el = $('#nw-shield');
  if (!el || !sh) return;
  const mini = $('#nw-mini');
  mini.innerHTML = '';
  if (!sh.on) { el.innerHTML = ''; return; }
  if (!sh.locked) {
    el.innerHTML = '';
    const n = sh.next;
    if (n) mini.innerHTML = `<span class="nw-shield-mini" title="F1 spoiler shield: locks when that weekend's first session starts">${ico('lock')}Shield ready · ${esc(n.name.replace(/ Grand Prix.*/, ' GP'))} ${esc(dayName(n.first_session))}</span>`;
    return;
  }
  if (sh.reason !== 'race') {
    el.innerHTML = `<div class="nw-shield">${ico('lock')}<span><b>Spoiler shield on</b> — the F1 calendar ${sh.reason === 'stale' ? 'is more than two weeks old' : 'hasn’t loaded yet'}, so every F1-looking story is hidden until it’s back.</span></div>`;
    return;
  }
  const rounds = sh.rounds || [];
  const row = r => `<span class="nw-shield__round"><b>${esc(r.name.replace(/ Grand Prix.*/, ' GP'))}</b> <span class="dim">${esc((r.first_label || 'Qualifying').toLowerCase())} started ${esc(dayName(r.first_session))}, ${esc(hm(r.first_session))}</span>
    <button type="button" class="btn btn--go" data-nw-watched="${r.round}" data-season="${esc(sh.season ?? '')}">${ico('check')}${rounds.length > 1 ? `Watched up to here` : 'I’ve watched it'}</button></span>`;
  el.innerHTML = `<div class="nw-shield">${ico('lock')}<span class="nw-shield__text"><b>Spoiler shield on</b> — F1 stories published since then are hidden everywhere on this page.</span>
    <span class="nw-shield__rounds">${rounds.map(row).join('')}</span></div>`;
}

// ---- stories --------------------------------------------------------------------------------------------------------------
function rankChip(it) {
  if (it.most_read) return `<span class="chip chip--new" title="In the BBC’s Most read right now">#${it.most_read} most read</span>`;
  if (it.peak_rank) return `<span class="chip" title="How high it got in the BBC’s Most read">was #${it.peak_rank} most read</span>`;
  return '';
}
const MON_SHORT = iso => { const [y, m, d] = String(iso || '').split('-'); return d ? `${Number(d)} ${MON[Number(m) - 1]}` : iso || ''; };
/** What Citadel's other cards know about the artists in a Music story: a new album, a pre-order, a gig you're going to. */
function linkChips(it) {
  const many = new Set((it.links || []).map(l => l.artist)).size > 1;
  return (it.links || []).map(l => {
    const who = many ? `${l.artist}: ` : '';
    const [icon, text] = l.kind === 'release' ? ['disc', l.out ? `New album out now — ${l.title}` : `New album ${MON_SHORT(l.date)} — ${l.title}`]
      : l.kind === 'vinyl' ? ['radar', l.bought ? `Pre-ordered — ${l.title}` : `Pre-order open — ${l.title}`]
      : ['bolt', `You’re seeing them ${dayName(new Date(`${l.date}T12:00:00`).getTime() / 1000)}`];
    const body = `${ico(icon)}${esc(who + text)}`;
    return l.url ? `<a class="st__link st__link--${l.kind}" href="${esc(safeUrl(l.url))}" target="_blank" rel="noopener noreferrer">${body}</a>` : `<span class="st__link st__link--${l.kind}">${body}</span>`;
  }).join('');
}
const alsoChip = it => (it.also?.length ? `<span class="st__also">also ${it.also.map(a => `<a href="${esc(safeUrl(a.url))}" target="_blank" rel="noopener noreferrer">${esc(a.name)}</a>`).join(', ')}</span>` : '');

function acts(it) {
  return `<div class="st__acts">
    <button type="button" class="btn btn--icon${it.vote === 1 ? ' is-on' : ''}" data-nw-vote="1" data-id="${it.id}" aria-pressed="${it.vote === 1}" aria-label="More like this" title="More like this">${ico('thumbup')}</button>
    <button type="button" class="btn btn--icon${it.vote === -1 ? ' is-on' : ''}" data-nw-vote="-1" data-id="${it.id}" aria-pressed="${it.vote === -1}" aria-label="Less like this" title="Less like this">${ico('thumbdown')}</button>
    <button type="button" class="btn btn--icon${it.saved ? ' is-on' : ''}" data-nw-save="${it.id}" aria-pressed="${it.saved}" aria-label="${it.saved ? 'Saved — click to unsave' : 'Save for later'}" title="${it.saved ? 'Saved' : 'Save for later'}">${ico('star')}</button></div>`;
}

function shieldRow(it, opts = {}) {
  return `<li class="st st--shield">${opts.rank ? `<span class="mr__n">${it.rank}</span>` : ''}<div class="st__main">
    <div class="st__src">${esc(it.source)}<span class="st__age">${esc(age(it.t))}</span></div>
    <div class="st__title st__title--shield">${ico('lock')}F1 story hidden</div></div>
    <div class="st__acts st__acts--on"><button type="button" class="btn" data-nw-reveal="${it.id}">Reveal</button></div></li>`;
}

function downStrip(it) {
  if (downOpen !== it.id) return '';
  const chips = (it.topics || []).map(t => `<button type="button" class="fchip" data-nw-never="${esc(t)}" data-kind="topic">${esc(t)}</button>`).join('');
  return `<div class="st__never">${ico('check')}<span>Fewer like this. Never show:</span>${chips}
    <button type="button" class="fchip" data-nw-never="${esc(it.source)}" data-kind="source">Anything from ${esc(it.source)}</button>
    <button type="button" class="btn btn--icon" data-nw-never-close aria-label="Done" title="Done">${ico('x')}</button></div>`;
}

/** A story row. `lead` adds its picture and summary; `summary` shows the summary on its own. */
function storyRow(it, { lead = false, summary = false, reason = '', rank = false } = {}) {
  if (it.shielded) return shieldRow(it, { rank });
  if (it.muted) return `<li class="st st--muted">${rank ? `<span class="mr__n">${it.rank}</span>` : ''}<div class="st__main"><div class="st__title st__title--shield">Hidden — you said never show “${esc(it.muted)}”</div></div></li>`;
  const pic = lead && it.image ? `<a class="st__pic" href="${esc(safeUrl(it.url))}" target="_blank" rel="noopener noreferrer" data-nw-read="${it.id}" tabindex="-1"><img src="${esc(it.image)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer"></a>` : '';
  const move = rank && it.prev_rank != null && it.prev_rank !== it.rank
    ? `<span class="mr__move ${it.prev_rank > it.rank ? 'up' : 'down'}" title="Was #${it.prev_rank} an hour ago">${it.prev_rank > it.rank ? '▲' : '▼'}${Math.abs(it.prev_rank - it.rank)}</span>`
    : rank && it.fresh ? '<span class="mr__move new" title="New to the list this hour">NEW</span>' : '';
  const bands = (it.artists || []).length || (it.links || []).length ? `<span class="st__bands">${(it.artists || []).map(a => `<span class="chip chip--vinyl">${esc(a)}</span>`).join('')}${linkChips(it)}</span>` : '';
  return `<li class="st${lead ? ' st--lead' : ''}${it.read ? ' is-read' : ''}${it.saved ? ' is-saved' : ''}" data-nw-row="${it.id}"${it.sole ? ' data-nw-keep' : ''}>
    ${rank ? `<span class="mr__n">${it.rank}</span>` : ''}${pic}
    <div class="st__main">
      <div class="st__src">${esc(it.source)}${it.t && !rank ? `<span class="st__age">${esc(age(it.t))}</span>` : ''}${move}${rank ? '' : rankChip(it)}${reason ? `<span class="chip chip--warn">${esc(reason)}</span>` : ''}</div>
      <a class="st__title" href="${esc(safeUrl(it.url))}" target="_blank" rel="noopener noreferrer" data-nw-read="${it.id}">${esc(it.title)}</a>
      ${(lead || summary) && it.summary ? `<p class="st__sum">${esc(it.summary)}</p>` : ''}
      ${alsoChip(it)}${bands}${downStrip(it)}
    </div>${acts(it)}</li>`;
}

// ---- panels --------------------------------------------------------------------------------------------------------------------
// On the fixed big-screen layout the front page is exactly one screen (news.css): each tile keeps as many whole stories
// as fit, never half of one. Smaller screens scroll as normal, so nothing is dropped there.
const FIXED = matchMedia('(min-width: 1800px) and (min-height: 860px)');
function fitFront() {
  if (tab !== 'front') return;
  $$('.nw-front .st-list', root).forEach(list => {
    const rows = $$(':scope > li', list);
    rows.forEach(li => { li.hidden = false; });
    if (!FIXED.matches) return;
    for (let i = rows.length - 1; i > 0 && list.scrollHeight > list.clientHeight + 1; i--) {
      if (!rows[i].hasAttribute('data-nw-keep')) rows[i].hidden = true;          // a section's only story in a shared tile stays
    }
  });
}
let fitTimer;
addEventListener('resize', () => { clearTimeout(fitTimer); fitTimer = setTimeout(fitFront, 150); });

function renderPanel() {
  renderTabs();
  const panel = $('#nw-panel');
  if (tab === 'front') { panel.innerHTML = frontHtml(view); requestAnimationFrame(fitFront); }
  else if (tab === 'archive') panel.innerHTML = archiveHtml();
  else panel.innerHTML = sectionHtml(view);
}

const empty = (icon, title, text) => `<div class="nw-empty">${ico(icon)}<b>${esc(title)}</b>${text ? `<span>${text}</span>` : ''}</div>`;

function frontHtml(d) {
  const mr = d.most_read;
  const mrCard = mr ? `<section class="card nw-mr"><div class="card__in"><div class="card__head"><span class="hud">Most read · BBC</span>
      <span class="meta">${mr.error ? `<span class="warn" title="${esc(mr.error)}">couldn’t refresh · </span>` : ''}${mr.at ? `as of ${esc(hm(mr.at))}` : ''}</span></div>
      ${mr.items.length ? `<ol class="rows st-list mr">${mr.items.map(it => storyRow(it, { rank: true })).join('')}</ol>` : empty('news', 'Not loaded yet', 'The BBC’s list arrives within the hour.')}</div></section>` : '';
  const blocks = d.sections.map(b => `<section class="card nw-block"><div class="card__in"><div class="card__head"><span class="hud">${esc(b.name)}${b.personal ? ' · your artists' : ''}</span>
      <span class="nw-block__tabs">${(b.tabs || [{ id: b.id, name: '', total: b.total }]).length > 1
        ? b.tabs.map(t => `<button type="button" class="btn" data-nw-tab="${esc(t.id)}">${esc(t.name)} ${t.total} ›</button>`).join('')
        : `<button type="button" class="btn" data-nw-tab="${esc(b.id)}">${b.total ? `All ${b.total}` : 'Open'} ›</button>`}</span></div>
      ${b.lead || b.items.length ? `<ul class="rows st-list">${b.lead ? storyRow(b.lead, { lead: true }) : ''}${b.items.map(it => storyRow(it)).join('')}</ul>` : empty(ICONS[b.id] || 'news', 'Nothing new', b.personal ? 'No news about your artists in the last couple of days.' : 'No stories in the last couple of days.')}</div></section>`).join('');
  return `<div class="nw-front${mr ? '' : ' nw-front--solo'}">${mrCard}<div class="nw-blocks">${blocks || empty('news', 'No sections on the front page', 'Choose some in Settings → News.')}</div></div>`;
}

function sectionHtml(d) {
  const isMusic = d.id === 'music';
  let stories = d.stories;
  if ((isMusic || d.personal) && (musicUi.band || musicUi.vinyl)) stories = stories.filter(s => s.shielded || ((!musicUi.band || (s.artists || []).includes(musicUi.band)) && (!musicUi.vinyl || VINYL_WORDS.test(s.title))));
  const seg = d.personal ? `<span class="seg" role="group" aria-label="Which stories"><button type="button" data-nw-everything="0" aria-pressed="${!d.everything}">Your artists</button><button type="button" data-nw-everything="1" aria-pressed="${!!d.everything}">Everything</button></span>` : '';
  const tools = isMusic || d.personal ? `<div class="nw-tools">${seg}<select class="nw-sel" data-nw-band aria-label="Filter by band"><option value="">All ${d.personal && !d.everything ? 'your artists' : 'bands'}</option>${d.artists.map(a => `<option${a === musicUi.band ? ' selected' : ''}>${esc(a)}</option>`).join('')}</select>
      <button type="button" class="fchip" data-nw-vinyl aria-pressed="${musicUi.vinyl}">Vinyl</button></div>` : '';
  const others = d.personal && !d.everything && d.others ? `<p class="nw-note">${d.others} more ${d.others === 1 ? 'story' : 'stories'} about artists you don’t play · <a href="#news" data-nw-everything="1">show everything</a></p>` : '';
  const held = d.hidden.length ? `<details class="nw-held"><summary>${d.hidden.length} held back for you <span class="dim">— stories like ones you gave a 👎</span></summary>
      <ul class="rows st-list">${d.hidden.map(it => storyRow(it, { summary: true, reason: it.reason })).join('')}</ul></details>` : '';
  const muted = d.muted ? `<p class="nw-note">${d.muted} more hidden by your “Never show” rules · <a href="/settings#news">manage</a></p>` : '';
  return `<section class="card nw-sec"><div class="card__in"><div class="card__head"><span class="hud">${esc(d.name)} · latest</span>
      <span class="meta">${stories.length < d.total ? `top ${stories.length} of ${d.total}` : `${stories.length} stories`}</span></div>${tools}
      ${stories.length ? `<ul class="rows st-list">${stories.map(it => storyRow(it, { summary: true })).join('')}</ul>` : empty(ICONS[d.id] || 'news', 'Nothing here', (isMusic || d.personal) && (musicUi.band || musicUi.vinyl) ? 'Nothing matches those filters.' : d.personal && !d.everything ? 'No news about your artists in the latest window.' : 'No stories in the latest window.')}
      ${others}${held}${muted}<div class="nw-foot"><button type="button" class="btn" data-nw-tab="archive" data-nw-archive-section="${esc(d.id)}">${ico('history')}Older ${esc(d.name)} stories</button></div></div></section>`;
}

// ---- archive ---------------------------------------------------------------------------------------------------------------------
async function loadArchive(more = false) {
  const p = new URLSearchParams();
  for (const k of ['section', 'source', 'q']) if (arch[k]) p.set(k, arch[k]);
  for (const k of ['saved', 'liked', 'unread']) if (arch[k]) p.set(k, 'true');
  if (more && arch.next) { p.set('before', arch.next.before); p.set('before_id', arch.next.before_id); }
  const r = await call(() => api.get(`${API}/archive?${p}`));
  if (!r) return;
  arch.items = more ? [...arch.items, ...r.items] : r.items;
  arch.next = r.next;
  arch.sections = r.sections;
  renderShield(r.shield);
  if (more) { const list = $('#nw-arch-list'); if (list) { list.innerHTML = archiveList(); $('#nw-arch-more').innerHTML = moreBtn(); return; } }
  renderPanel();
}

const moreBtn = () => (arch.next ? `<button type="button" class="btn" data-nw-more>${ico('down')}Load older</button>` : arch.items.length ? '<span class="dim">That’s everything kept.</span>' : '');

function archiveList() {
  if (!arch.items.length) return empty('history', 'Nothing found', arch.q ? 'Try different words.' : 'Nothing matches those filters.');
  const groups = [];
  const today = new Date(); today.setHours(0, 0, 0, 0);
  for (const it of arch.items) {
    const d = new Date(it.t * 1000); d.setHours(0, 0, 0, 0);
    const diff = Math.round((today - d) / 86400000);
    const label = diff <= 0 ? 'Today' : diff === 1 ? 'Yesterday' : dayName(it.t);
    if (!groups.length || groups.at(-1).label !== label) groups.push({ label, items: [] });
    groups.at(-1).items.push(it);
  }
  return groups.map(g => `<h3 class="nw-day">${esc(g.label)}</h3><ul class="rows st-list">${g.items.map(it => storyRow(it)).join('')}</ul>`).join('');
}

function archiveHtml() {
  const secs = arch.sections.length ? arch.sections : [...sections(), { id: 'mostread', name: 'Most read', sources: [] }];
  const chosen = secs.find(s => s.id === arch.section);
  const chip = (key, label) => `<button type="button" class="fchip" data-nw-flag="${key}" aria-pressed="${arch[key]}">${label}</button>`;
  return `<section class="card nw-sec"><div class="card__in"><div class="card__head"><span class="hud">Archive</span><span class="meta">everything still kept</span></div>
    <div class="nw-tools nw-tools--arch">
      <div class="nw-chips"><button type="button" class="fchip" data-nw-asec="" aria-pressed="${!arch.section}">All</button>${secs.map(s => `<button type="button" class="fchip" data-nw-asec="${esc(s.id)}" aria-pressed="${arch.section === s.id}">${esc(s.name)}</button>`).join('')}</div>
      <div class="nw-chips">${chosen?.sources?.length ? `<select class="nw-sel" data-nw-asrc aria-label="Source"><option value="">All sources</option>${chosen.sources.map(f => `<option value="${esc(f.id)}"${arch.source === f.id ? ' selected' : ''}>${esc(f.name)}</option>`).join('')}</select>` : ''}
        <input class="nw-search" type="search" data-nw-q placeholder="Search headlines…" value="${esc(arch.q)}" aria-label="Search the archive">
        ${chip('unread', 'Unread')}${chip('saved', 'Saved')}${chip('liked', 'Liked')}</div></div>
    <div id="nw-arch-list">${archiveList()}</div><div class="nw-foot" id="nw-arch-more">${moreBtn()}</div></div></section>`;
}

// ---- finding a story in what's on screen ---------------------------------------------------------------------------------------
function allShown() {
  if (tab === 'archive') return arch.items;
  if (!view) return [];
  if (tab === 'front') return [...(view.most_read?.items || []), ...view.sections.flatMap(b => [b.lead, ...b.items].filter(Boolean))];
  return [...view.stories, ...view.hidden];
}
const findAll = id => allShown().filter(s => s && s.id === id);

// ---- events ------------------------------------------------------------------------------------------------------------------------
async function onClick(e) {
  const t = e.target;
  const tabBtn = t.closest('[data-nw-tab]');
  if (tabBtn) {
    tab = tabBtn.dataset.nwTab;
    store.set('nw-tab', tab);
    downOpen = null;
    if (tabBtn.dataset.nwArchiveSection !== undefined) { arch.section = tabBtn.dataset.nwArchiveSection; arch.source = ''; }
    renderTabs();
    $('#nw-panel').innerHTML = '<div class="nw-empty"><span class="spin"></span></div>';
    await loadTab();
    $('#nw-panel')?.scrollTo?.(0, 0);
    return;
  }
  const readLink = t.closest('[data-nw-read]');
  if (readLink) {                                               // the link opens by itself (new tab); this just remembers it
    const id = Number(readLink.dataset.nwRead);
    findAll(id).forEach(s => { s.read = true; });
    $$(`[data-nw-row="${id}"]`, root).forEach(r => r.classList.add('is-read'));
    api.post(`${API}/items/${id}/read`).catch(() => {});
    return;
  }
  const vote = t.closest('[data-nw-vote]');
  if (vote) {
    const id = Number(vote.dataset.id), want = Number(vote.dataset.nwVote);
    const it = findAll(id)[0];
    const next = it?.vote === want ? 0 : want;
    const r = await call(() => api.put(`${API}/items/${id}/vote`, { vote: next }));
    if (!r) return;
    findAll(id).forEach(s => { s.vote = next; });
    if (next === -1) { downOpen = id; renderPanel(); return; }
    if (next === 1) toast('Got it — more like this');
    if (downOpen === id) downOpen = null;
    if (next === 0 && tab !== 'front' && tab !== 'archive') { await loadTab(); return; }
    renderPanel();
    return;
  }
  const never = t.closest('[data-nw-never]');
  if (never) {
    const term = never.dataset.nwNever, kind = never.dataset.kind;
    const r = await call(() => api.post(`${API}/rules`, { term, kind }));
    if (!r) return;
    toast(kind === 'source' ? `Won’t show anything from ${term} — undo in Settings → News` : `Won’t show stories about “${term}” — undo in Settings → News`);
    downOpen = null;
    await loadTab();
    return;
  }
  if (t.closest('[data-nw-never-close]')) { downOpen = null; await loadTab(); return; }
  const save = t.closest('[data-nw-save]');
  if (save) {
    const id = Number(save.dataset.nwSave), it = findAll(id)[0];
    const on = !it?.saved;
    const r = await call(() => (on ? api.post(`${API}/items/${id}/save`) : api.del(`${API}/items/${id}/save`)));
    if (!r) return;
    findAll(id).forEach(s => { s.saved = on; });
    toast(on ? 'Saved — find it under Archive › Saved' : 'Unsaved');
    renderPanel();
    return;
  }
  const reveal = t.closest('[data-nw-reveal]');
  if (reveal) {
    if (!confirm('This looks like an F1 story and may give away a result. Show it anyway?')) return;
    const id = Number(reveal.dataset.nwReveal);
    const r = await call(() => api.post(`${API}/items/${id}/reveal`));
    if (!r) return;
    await loadTab();
    return;
  }
  const watched = t.closest('[data-nw-watched]');
  if (watched) {
    const body = { round: Number(watched.dataset.nwWatched) };
    if (watched.dataset.season) body.season = Number(watched.dataset.season);
    const r = await call(() => api.post(`${API}/f1/watched`, body));
    if (!r) return;
    toast('Enjoy — F1 stories are back');
    await refresh();
    return;
  }
  const every = t.closest('[data-nw-everything]');
  if (every) {
    e.preventDefault();
    musicUi.everything = every.dataset.nwEverything === '1';
    musicUi.band = '';
    store.set('nw-everything', musicUi.everything ? '1' : '');
    await loadTab();
    return;
  }
  const vinyl = t.closest('[data-nw-vinyl]');
  if (vinyl) { musicUi.vinyl = !musicUi.vinyl; renderPanel(); return; }
  const asec = t.closest('[data-nw-asec]');
  if (asec) { arch.section = asec.dataset.nwAsec; arch.source = ''; await loadArchive(); return; }
  const flag = t.closest('[data-nw-flag]');
  if (flag) { const k = flag.dataset.nwFlag; arch[k] = !arch[k]; await loadArchive(); return; }
  if (t.closest('[data-nw-more]')) { await loadArchive(true); }
}

function onChange(e) {
  const band = e.target.closest('[data-nw-band]');
  if (band) { musicUi.band = band.value; renderPanel(); return; }
  const src = e.target.closest('[data-nw-asrc]');
  if (src) { arch.source = src.value; loadArchive(); }
}

let qTimer;
function onInput(e) {
  const q = e.target.closest('[data-nw-q]');
  if (!q) return;
  clearTimeout(qTimer);
  qTimer = setTimeout(async () => {
    arch.q = q.value.trim();
    const pos = q.selectionStart;
    await loadArchive();
    const box = $('[data-nw-q]', root);                          // the panel was redrawn: keep typing where you were
    if (box) { box.focus(); try { box.setSelectionRange(pos, pos); } catch { /* fine */ } }
  }, 350);
}
