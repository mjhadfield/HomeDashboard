// Settings: everything the owner can change without touching files. Each tab is a small render function
// returning HTML; forms submit to the validated owner API and the page re-renders from fresh config.
import * as api from './api.js';
import { $, $$, esc, ico, hhmm, ago, toast, isoOf } from './util.js';
import { BUILTIN, chosen, specOf, useTheme, applySpec } from './theme.js';

const TABS = [['links', 'Links', 'globe'], ['projects', 'Projects', 'grid'], ['widgets', 'Widgets', 'cal'], ['appearance', 'Appearance', 'sun'], ['music', 'Music', 'disc'], ['listen', 'Music page', 'wave'], ['news', 'News', 'news'], ['screens', 'Screens & devices', 'server'], ['maintenance', 'Maintenance & logs', 'gear'], ['about', 'About', 'chip']];
const panel = $('#panel');
let cfg = null, live = null, extras = { artists: { artists: [], music_db: false }, gigs: [] };   // config, live widget status, artists + gigs
let maint = { jobs: [], log_counts: {}, backups: [], now: 0 }, logs = { items: [], sources: [], counts: {} };   // schedule table + event log
const logf = { level: 'warning', source: '', q: '' };                                                      // the log's current filter
let tab = TABS.some(t => t[0] === location.hash.slice(1)) ? location.hash.slice(1) : 'links';
let editing = null;                     // {kind:'link'|'project', id} | {kind, id:'new'}
let enrol = null;                       // last generated enrolment link
let musicCfg = null, musicLink = null;   // the Music page's settings (GET /api/owner/music) and the last Spotify login link
let newsExtra = { learning: null, f1: null };   // the News page's learned topics + rules, and its F1 shield's status
const svcTests = {};                    // service id -> {ok, message} from its Test button

// A dashboard card can link straight to "add one of these" (e.g. Next gig's small + button) with ?add=<kind>,
// instead of only landing on the tab and leaving the person to find the button themselves.
const addParam = new URLSearchParams(location.search).get('add');
if (addParam === 'gig') { tab = 'music'; editing = { kind: 'gig', id: 'new' }; }
if (addParam) history.replaceState(null, '', location.pathname + location.hash);   // don't reopen it again on every refresh

// ---- small builders (all dynamic text goes through esc()) -----------------------------------------------
const field = (label, control, hint = '', cls = '') => `<label class="field ${cls}"><span class="field__l">${esc(label)}</span>${control}${hint ? `<span class="field__h">${hint}</span>` : ''}</label>`;
const input = (name, value = '', attrs = '') => `<input class="input" name="${name}" value="${esc(value)}" ${attrs}>`;
const select = (name, options, value) => `<select class="input" name="${name}">${options.map(o => { const [v, l] = Array.isArray(o) ? o : [o, o]; return `<option value="${esc(v)}"${String(v) === String(value) ? ' selected' : ''}>${esc(l)}</option>`; }).join('')}</select>`;
const check = (name, label, on, hint = '') => `<label class="check"><input type="checkbox" name="${name}"${on ? ' checked' : ''}><span>${esc(label)}</span>${hint ? `<small>${hint}</small>` : ''}</label>`;
const card = (title, intro, body, tools = '') => `<section class="card sx__card"><div class="card__in"><div class="card__head"><span class="hud">${esc(title)}</span><span class="card__tools">${tools}</span></div>
  <div class="card__body card__body--flush">${intro ? `<p class="sx__intro">${intro}</p>` : ''}${body}</div></div></section>`;
const act = (name, label, extra = '', cls = '') => `<button type="button" class="btn ${cls}" data-do="${name}" ${extra}>${label}</button>`;
const val = (form, name) => form.elements[name]?.value.trim() ?? '';
const on = (form, name) => !!form.elements[name]?.checked;

/** Pinned items always sort above unpinned ones, so a move only makes sense within the same group. */
const sameGroup = (list, i, d) => !!list[i + d] && list[i + d].pinned === list[i].pinned;

// ---- Links --------------------------------------------------------------------------------------------------
function linkForm(l = {}) {
  return `<form class="form" data-form="link" data-id="${esc(l.id || '')}">
    <div class="form-grid">
      ${field('Name', input('name', l.name, 'required maxlength="60"'))}
      ${field('Subtitle', input('subtitle', l.subtitle, 'maxlength="80" placeholder="e.g. 192.168.0.70:8096"'))}
      ${field('Address', input('url', l.url, 'required type="url" placeholder="https://…"'))}
      ${field('Icon', select('icon', cfg.icons, l.icon || 'globe'))}
      ${field('Status check address (optional)', input('health_url', l.health_url, 'type="url" placeholder="https://…/health"'), '', 'wide')}
    </div>
    <div class="checks">
      ${check('pinned', 'Pin to the top', l.pinned, '')}
      ${check('is_primary', 'Highlight this tile', l.is_primary)}
      ${check('show_guest', 'Show on the guest page', l.show_guest, '')}
      ${check('more', 'Only in the expanded Launchpad', l.tier === 'more')}
      ${check('for_media', 'Use as Media view', l.for_media, '')}
      ${check('for_music', 'Use as Companion view', l.for_music, '')}
      ${check('for_sql', 'Use as SQL DB view', l.for_sql, '')}
      ${check('for_ai', 'Use as Odysseus view', l.for_ai, '')}
    </div>
    <div class="form-actions">${act('cancel', 'Cancel')}<button class="btn btn--solid" type="submit">Save link</button></div></form>`;
}

function linksTab() {
  const rows = cfg.links.map((l, i) => editing?.kind === 'link' && editing.id === l.id
    ? `<li class="item item--edit">${linkForm(l)}</li>`
    : `<li class="item"><span class="item__icon">${ico(l.icon)}</span>
        <div class="item__main"><b>${esc(l.name)}</b>${l.pinned ? '<span class="chip chip--new">Pinned</span>' : ''}${l.is_primary ? '<span class="chip chip--new">Highlighted</span>' : ''}${l.show_guest ? '<span class="chip chip--vinyl">Guest</span>' : ''}${l.tier === 'more' ? '<span class="chip">Expanded only</span>' : ''}${l.for_media ? '<span class="chip chip--vinyl">Media view</span>' : ''}${l.for_music ? '<span class="chip chip--vinyl">Companion view</span>' : ''}${l.for_sql ? '<span class="chip chip--vinyl">SQL DB view</span>' : ''}
          <div class="item__sub">${esc(l.subtitle)}${l.subtitle ? ' · ' : ''}${esc(l.url)}</div></div>
        <div class="item__acts">
          ${act('pin-link', ico('pin'), `data-id="${l.id}" data-to="${!l.pinned}" aria-label="${l.pinned ? 'Unpin' : 'Pin to top'}" title="${l.pinned ? 'Unpin' : 'Pin to top'}"`, `btn--icon${l.pinned ? ' is-pinned' : ''}`)}
          ${act('move-link', ico('up'), `data-i="${i}" data-d="-1" aria-label="Move up" ${sameGroup(cfg.links, i, -1) ? '' : 'disabled'}`, 'btn--icon')}
          ${act('move-link', ico('down'), `data-i="${i}" data-d="1" aria-label="Move down" ${sameGroup(cfg.links, i, 1) ? '' : 'disabled'}`, 'btn--icon')}
          ${act('edit-link', 'Edit', `data-id="${l.id}"`)}${act('delete-link', 'Delete', `data-id="${l.id}" data-name="${esc(l.name)}"`, 'btn--danger')}</div></li>`).join('');
  const adding = editing?.kind === 'link' && editing.id === 'new' ? `<li class="item item--edit">${linkForm()}</li>` : '';
  return card('Links', '',
    `<div class="bar-actions">${act('new-link', `${ico('play')}Add link`, '', 'btn--go')}</div><ul class="items">${adding}${rows}</ul>`);
}

// ---- Projects -------------------------------------------------------------------------------------------------
const envText = env => Object.entries(env || {}).map(([k, v]) => `${k}=${v}`).join('\n');

function projectForm(p = null) {
  const isNew = !p;
  p = p || { kind: 'web', lan_visible: true, enabled: true, env: {}, show_projects: true };
  return `<form class="form" data-form="project" data-slug="${esc(p.slug || '')}">
    <div class="form-grid">
      ${field('Name', input('name', p.name, 'required maxlength="60"'))}
      ${field('Id', input('slug', p.slug, `${isNew ? 'required pattern="[a-z0-9][a-z0-9\\-]*" maxlength="40"' : 'readonly'}`), isNew ? 'Lowercase letters, digits and dashes. Can’t be changed later.' : '')}
      ${field('Description', input('description', p.description, 'maxlength="120"'), '', 'wide')}
      ${field('Type', select('kind', [['web', 'Web app (has a port)'], ['desktop', 'Desktop app (window)']], p.kind))}
      ${field('Port', input('port', p.port ?? '', 'type="number" min="1" max="65535"'), '')}
      ${field('Open at', input('url', p.url, 'placeholder="http://{host}:8642/"'), '<code>{host}</code> = the address you opened Citadel with; <code>{ip}</code> = this PC’s LAN address.', 'wide')}
      ${field('Working directory', input('cwd', p.cwd, 'required placeholder="/home/you/Projects/App"'), p.cwd_exists === false ? '<span style="color:#fda4af">This folder doesn’t exist on this machine.</span>' : '', 'wide')}
      ${field('Start command', `<textarea class="input" name="command" rows="2" required spellcheck="false">${esc(p.command || '')}</textarea>`, 'Not run through a shell: no <code>&&</code>, pipes or <code>$VARS</code>.', 'wide')}
      ${field('Environment (optional)', `<textarea class="input" name="env" rows="2" spellcheck="false" placeholder="KEY=value (one per line)">${esc(envText(p.env))}</textarea>`, '', 'wide')}
      ${field('Launchpad icon', select('icon', cfg.icons, p.icon || (p.kind === 'desktop' ? 'play' : 'server')), '')}
    </div>
    <div class="checks">
      ${check('pinned', 'Pin to the top', p.pinned, '')}
      ${check('show_projects', 'Show in Projects', p.show_projects, '')}
      ${check('show_launchpad', 'Show in Launchpad', p.show_launchpad, '')}
      ${check('local_only', 'Only reachable from this PC', !p.lan_visible, '')}
      ${check('enabled', 'Show on the dashboard', p.enabled, 'Also stops it.')}
      ${check('for_music', 'Use for the Companion page', p.for_music, '')}
      ${check('for_sql', 'Use for the SQL DB page', p.for_sql, '')}
    </div>
    <div class="form-actions">${act('cancel', 'Cancel')}<button class="btn btn--solid" type="submit">Save project</button></div></form>`;
}

function projectsTab() {
  const rows = cfg.projects.map((p, i) => editing?.kind === 'project' && editing.id === p.slug
    ? `<li class="item item--edit">${projectForm(p)}</li>`
    : `<li class="item"><span class="item__icon">${ico(p.kind === 'web' ? 'server' : 'cards')}</span>
        <div class="item__main"><b>${esc(p.name)}</b>${p.pinned ? '<span class="chip chip--new">Pinned</span>' : ''}<span class="chip">${p.kind === 'web' ? `:${esc(p.port ?? '?')}` : 'Desktop'}</span>${p.show_projects ? '' : '<span class="chip chip--warn">Not in Projects</span>'}${p.for_music ? '<span class="chip chip--vinyl">Companion view</span>' : ''}${p.for_sql ? '<span class="chip chip--vinyl">SQL DB view</span>' : ''}${p.lan_visible ? '' : '<span class="chip chip--warn">This PC only</span>'}${p.enabled ? '' : '<span class="chip">Hidden</span>'}${p.cwd_exists ? '' : '<span class="chip chip--warn">Folder missing</span>'}
          <div class="item__sub">${esc(p.command)}</div></div>
        <div class="item__acts">
          ${act('launchpad-project', `${ico('grid')}Launchpad`, `data-id="${esc(p.slug)}" data-to="${!p.show_launchpad}" aria-pressed="${!!p.show_launchpad}" title="${p.show_launchpad ? 'On the Launchpad — click to take it off' : 'Not on the Launchpad — click to add it'}"`, `btn--toggle${p.show_launchpad ? ' is-on' : ''}`)}
          ${act('pin-project', ico('pin'), `data-id="${esc(p.slug)}" data-to="${!p.pinned}" aria-label="${p.pinned ? 'Unpin' : 'Pin to top'}" title="${p.pinned ? 'Unpin' : 'Pin to top'}"`, `btn--icon${p.pinned ? ' is-pinned' : ''}`)}
          ${act('move-project', ico('up'), `data-i="${i}" data-d="-1" aria-label="Move up" ${sameGroup(cfg.projects, i, -1) ? '' : 'disabled'}`, 'btn--icon')}
          ${act('move-project', ico('down'), `data-i="${i}" data-d="1" aria-label="Move down" ${sameGroup(cfg.projects, i, 1) ? '' : 'disabled'}`, 'btn--icon')}
          ${act('edit-project', 'Edit', `data-id="${esc(p.slug)}"`)}${act('delete-project', 'Delete', `data-id="${esc(p.slug)}" data-name="${esc(p.name)}"`, 'btn--danger')}</div></li>`).join('');
  const adding = editing?.kind === 'project' && editing.id === 'new' ? `<li class="item item--edit">${projectForm()}</li>` : '';
  return card('Projects', '',
    `<div class="bar-actions">${act('new-project', `${ico('play')}Add project`, '', 'btn--go')}</div><ul class="items">${adding}${rows}</ul>`);
}

// ---- Widgets ----------------------------------------------------------------------------------------------------
/** `label` names what synced, for a card with more than one ("Most read: synced 14:05"). */
function statusLine(w, id = '', label = '') {
  if (!w) return '';
  const attrs = `${id ? ` data-status="${id}"` : ''}${label ? ` data-label="${esc(label)}"` : ''}`, pre = label ? `${esc(label)}: ` : '';
  if (w.error) return `<div class="status-line status-line--err"${attrs}><span class="dot dot--bad"></span>${pre}Last refresh failed: ${esc(w.error)}${w.fetched_at ? ` — showing data from ${esc(hhmm(w.fetched_at))}` : ''}</div>`;
  if (w.fetched_at) return `<div class="status-line"${attrs}><span class="dot dot--ok"></span>${pre}${label ? 'synced' : 'Synced'} ${esc(hhmm(w.fetched_at))}</div>`;
  return `<div class="status-line"${attrs}><span class="dot"></span>${pre}${w.configured ? 'Waiting for the first sync…' : 'Not set up yet'}</div>`;
}

// ---- Shortcuts: the row of web links across the top of the dashboard --------------------------------------------
const SC_MODES = [['site', 'Site’s own icon'], ['builtin', 'Citadel icon'], ['image', 'Image address'], ['letter', 'First letter']];
function scPreview(x) {
  const got = (live?.shortcuts?.data?.shortcuts || []).find(s => s.id === x.id);
  const mode = x.icon_mode || 'site';
  if (mode === 'builtin') return ico(x.icon || 'globe');
  if (got?.img && (mode === 'site' || mode === 'image')) return `<img src="${esc(got.img)}" alt="">`;
  return `<span class="sc-row__letter">${esc(((x.label || '').trim() || (x.url || '?').replace(/^https?:\/\/(www\.)?/, '')).charAt(0).toUpperCase() || '?')}</span>`;
}
const scRow = (x = {}) => {
  const mode = x.icon_mode || 'site';
  return `<div class="sc-row" data-sc data-id="${esc(x.id || '')}">
  <span class="sc-row__preview" title="Current icon">${scPreview(x)}</span>
  ${field('Label', input('sc_label', x.label || '', 'maxlength="30" placeholder="Optional"'))}
  ${field('Address', input('sc_url', x.url || '', 'type="url" placeholder="https://…" spellcheck="false"'), '', 'sc-row__url')}
  ${field('Icon', select('sc_mode', SC_MODES, mode))}
  ${field('Citadel icon', select('sc_icon', cfg.icons, x.icon || 'globe'), '', `sc-row__opt${mode === 'builtin' ? '' : ' is-hidden'}`)}
  ${field('Image address', input('sc_image', x.image || '', 'type="url" placeholder="https://…/logo.png" spellcheck="false"'), '', `sc-row__opt${mode === 'image' ? '' : ' is-hidden'}`)}
  <span class="sc-row__tools">${act('sc-up', ico('up'), 'aria-label="Move up" title="Move up"', 'btn--icon')}${act('sc-down', ico('down'), 'aria-label="Move down" title="Move down"', 'btn--icon')}${act('sc-remove', 'Remove', '', 'btn--danger')}</span></div>`;
};

function shortcutsCard() {
  const s = cfg.widgets.shortcuts.settings, failed = live?.shortcuts?.data?.failed || [];
  return card('Shortcuts — the top bar',
    '',
    `${statusLine(live?.shortcuts, 'shortcuts')}${failed.length ? `<div class="status-line"><span class="dot"></span>No icon found for ${esc(failed.join(', '))} — showing its first letter.</div>` : ''}
    <form class="form" data-form="shortcuts" style="padding:0 18px 12px">
      <div class="checks" style="margin-bottom:6px">${check('sc_show', 'Show the shortcut bar', s.show, '')}</div>
      <div id="scs">${s.shortcuts.map(scRow).join('')}</div>
      <div class="bar-actions" style="padding:10px 0 4px">${act('sc-add', `${ico('plus')}Add shortcut`, '', 'btn--go')}${cfg.links.length ? act('sc-import', `${ico('globe')}Copy in from Links`, 'title="Adds a shortcut for each Launchpad link not already here — the links themselves are left alone"') : ''}</div>
      <div class="form-actions">${act('refresh-widget', 'Refresh icons', 'data-id="shortcuts"')}<button class="btn btn--solid" type="submit">Save</button></div></form>`);
}

function binsCard() {
  const s = cfg.widgets.bins.settings;
  const hours = Array.from({ length: 24 }, (_, h) => [h, `${String(h).padStart(2, '0')}:00`]);
  return card('Bins — Salford City Council',
    'Find your property reference (UPRN) at <a href="https://www.findmyaddress.co.uk/search" target="_blank" rel="noopener">findmyaddress.co.uk</a>.',
    `${statusLine(live?.bins, 'bins')}<form class="form" data-form="bins" style="padding:0 18px 12px">
      <div class="form-grid">
        ${field('Property reference (UPRN)', input('uprn', s.uprn, 'inputmode="numeric" pattern="\\d{5,14}" placeholder="e.g. 123456789012"'))}
        ${field('“Put the bin out” reminder from', select('put_out_hour', hours, s.put_out_hour), '')}
      </div>
      <div class="checks"><span class="field__l">Remind me about</span>${Object.entries(cfg.streams).map(([k, t]) => check(`stream_${k}`, t, s.alert_streams.includes(k))).join('')}</div>
      <div class="form-actions">${act('refresh-widget', 'Sync now', 'data-id="bins"')}<button class="btn btn--solid" type="submit">Save</button></div></form>`);
}

const calRow = (c = {}) => `<div class="cal-row" data-cal data-id="${esc(c.id || '')}">
  ${field('Name', input('cal_name', c.name || 'Calendar', 'maxlength="40"'))}
  ${field('Colour', select('cal_colour', cfg.colours, c.colour || 'violet'))}
  ${field('Secret iCal address', input('cal_url', '', `type="password" autocomplete="off" spellcheck="false" placeholder="${c.has_url ? '•••• saved — leave blank to keep' : 'https://calendar.google.com/…/basic.ics'}"`), '', 'wide-m')}
  ${act('remove-cal', 'Remove', '', 'btn--danger')}</div>`;

function diaryCard() {
  const s = cfg.widgets.diary.settings;
  return card('Diary — calendars',
    'Google Calendar → Settings → your calendar → Integrate calendar → <i>Secret address in iCal format</i>.',
    `${statusLine(live?.diary, 'diary')}<form class="form" data-form="diary" style="padding:0 18px 12px">
      <div id="cals">${(s.calendars.length ? s.calendars : [{}]).map(calRow).join('')}</div>
      <div class="bar-actions" style="padding:10px 0 0">${act('add-cal', `${ico('play')}Add calendar`, '', 'btn--go')}</div>
      <div class="form-grid" style="margin-top:14px">${field('Look ahead (days)', input('horizon_days', s.horizon_days, 'type="number" min="7" max="365"'))}</div>
      <div class="form-actions">${act('refresh-widget', 'Sync now', 'data-id="diary"')}<button class="btn btn--solid" type="submit">Save</button></div></form>`);
}

// ---- Music & news ------------------------------------------------------------------------------------------------------
const today = () => { const d = new Date(); return isoOf(d.getFullYear(), d.getMonth(), d.getDate()); };
const SOURCE_LABEL = { 'music-companion': 'Music Companion', manual: 'Added by you' };

function gigForm(g = {}) {
  return `<form class="form" data-form="gig" data-id="${esc(g.id || '')}"><div class="form-grid">
      ${field('Artist', input('artist', g.artist, 'required maxlength="80"'))}
      ${field('Date', input('date', g.date, 'required type="date"'))}
      ${field('Venue', input('venue', g.venue, 'maxlength="80"'))}
      ${field('City', input('city', g.city, 'maxlength="60"'))}
      ${field('Doors (optional)', input('doors', g.doors, 'type="time"'))}
      ${field('Ticket link (optional)', input('ticket_url', g.ticket_url || '', 'type="url" placeholder="https://…"'))}
      ${field('Notes (optional)', input('notes', g.notes, 'maxlength="200" placeholder="e.g. block B, meeting Sam at 5"'), '', 'wide')}
    </div><div class="form-actions">${act('cancel', 'Cancel')}<button class="btn btn--solid" type="submit">Save gig</button></div></form>`;
}

function gigsCard() {
  const rows = extras.gigs.map(g => editing?.kind === 'gig' && editing.id === g.id ? `<li class="item item--edit">${gigForm(g)}</li>`
    : `<li class="item"><span class="item__icon">${ico('bolt')}</span>
        <div class="item__main"><b>${esc(g.artist)}</b>${g.date < today() ? '<span class="chip">Past</span>' : ''}
          <div class="item__sub">${esc(g.date)}${g.venue || g.city ? ` · ${esc([g.venue, g.city].filter(Boolean).join(', '))}` : ''}${g.doors ? ` · doors ${esc(g.doors)}` : ''}</div></div>
        <div class="item__acts">${act('edit-gig', 'Edit', `data-id="${g.id}"`)}${act('delete-gig', 'Delete', `data-id="${g.id}" data-name="${esc(g.artist)}"`, 'btn--danger')}</div></li>`).join('');
  const adding = editing?.kind === 'gig' && editing.id === 'new' ? `<li class="item item--edit">${gigForm()}</li>` : '';
  return card('Booked gigs', '',
    `<div class="bar-actions">${act('new-gig', `${ico('play')}Add gig`, '', 'btn--go')}</div><ul class="items">${adding}${rows || (adding ? '' : '<li class="item"><span class="field__h">No gigs yet.</span></li>')}</ul>`);
}

function artistsCard() {
  const list = extras.artists.artists;
  const muted = list.filter(a => a.status === 'mute').length;
  const opt = (v, l, cur) => `<option value="${v}"${cur === v ? ' selected' : ''}>${l}</option>`;
  const rows = list.map(a => `<li class="item" data-artist-row="${esc(a.name.toLowerCase())}"><span class="item__icon">${ico('disc')}</span>
      <div class="item__main"><b>${esc(a.name)}</b>${a.status === 'pin' ? '<span class="chip chip--new">Pinned</span>' : ''}${a.status === 'mute' ? '<span class="chip chip--warn">Muted</span>' : ''}
        <div class="item__sub">${esc(SOURCE_LABEL[a.source] || a.source)}${a.weight ? ` · score ${Math.round(a.weight)}` : ''}</div></div>
      <div class="item__acts"><select class="input input--sm" data-artist-status data-id="${a.id}" aria-label="Status of ${esc(a.name)}">${opt('follow', 'Follow', a.status)}${opt('pin', 'Pin', a.status)}${opt('mute', 'Mute', a.status)}</select>
        ${act('remove-artist', 'Remove', `data-id="${a.id}" data-name="${esc(a.name)}"`, 'btn--danger')}</div></li>`).join('');
  return card('Artists you follow', `${list.length} followed, ${muted} muted. <b>Pin</b> checks them first; <b>Mute</b> hides them.`,
    `<form class="bar-actions" data-form="artist-add" style="flex-wrap:wrap">
       <input class="input" name="name" required maxlength="80" placeholder="Follow an artist…" style="max-width:260px"><button class="btn btn--go" type="submit">Follow</button>
       ${act('resync', 'Resync from Music Companion', extras.artists.music_db ? '' : 'disabled title="Music Companion\'s database wasn\'t found"')}
       <input class="input" data-artist-filter placeholder="Filter this list…" style="max-width:220px;margin-left:auto" aria-label="Filter artists"></form>
     <ul class="items items--scroll">${rows || '<li class="item"><span class="field__h">No artists yet — follow one above, or resync from Music Companion.</span></li>'}</ul>`);
}

const typeChecks = (types, selected) => types.map(t => check(`type_${t}`, t, selected.includes(t))).join('');

function releasesCard() {
  const s = cfg.widgets.releases.settings;
  return card('Releases — new albums', '',
    `${statusLine(live?.releases, 'releases')}<form class="form" data-form="releases" style="padding:0 18px 12px"><div class="form-grid">
      ${field('Look back (days)', input('lookback_days', s.lookback_days, 'type="number" min="14" max="730"'), '')}
      ${field('Look ahead (days)', input('horizon_days', s.horizon_days, 'type="number" min="30" max="730"'), '')}
      ${field('Artists per sync', input('batch', s.batch, 'type="number" min="5" max="80"'), '')}
      </div><div class="checks"><span class="field__l">Include</span>${typeChecks(['Album', 'EP', 'Single'], s.types)}
        ${check('include_extras', 'Live albums, compilations and soundtracks', s.include_extras, '')}</div>
      <div class="form-actions">${act('refresh-widget', 'Sync now', 'data-id="releases"')}<button class="btn btn--solid" type="submit">Save</button></div></form>`);
}

const shopRow = (x = {}) => `<div class="feedcfg shopcfg" data-shop data-id="${esc(x.id || '')}" data-kind="${esc(x.kind || 'shopify')}">
  ${input('s_name', x.name || '', 'placeholder="Name" maxlength="40" aria-label="Store name"')}${input('s_url', x.url || '', 'type="url" placeholder="https://store.example" aria-label="Store address"')}
  ${x.kind === 'wix' ? input('s_coll', '', 'placeholder="(whole shop)" disabled aria-label="Wix shops are watched whole"')
    : input('s_coll', x.collection || '', `placeholder="${x.kind === 'juno' ? 'metal,rock,punk-hardcore' : 'pre-orders'}" maxlength="120" aria-label="${x.kind === 'juno' ? 'Juno sections' : 'Pre-order collection'}"`)}
  ${check('s_on', 'On', x.enabled !== false)}${act('check-shop', 'Check')}${act('remove-shop', 'Remove', '', 'btn--danger')}</div>`;

function vinylCard() {
  const s = cfg.widgets.vinyl.settings;
  return card('Vinyl radar — pre-orders', 'Shopify stores: the address plus the pre-order collection (<code>store.example/collections/<b>pre-orders</b></code>). Juno: genre sections, e.g. <code>metal,rock</code>.',
    `${statusLine(live?.vinyl, 'vinyl')}<form class="form" data-form="vinyl" style="padding:0 18px 12px">
      <div id="shops"><div class="field__l" style="display:grid;grid-template-columns:minmax(110px,1fr) minmax(200px,2fr) minmax(110px,1fr);gap:10px;padding:0 0 6px"><span>Store</span><span>Address</span><span>Pre-order collection / Juno sections</span></div>${s.shops.map(shopRow).join('')}</div>
      <div class="bar-actions" style="padding:10px 0 4px">${act('add-shop', `${ico('play')}Add store`, '', 'btn--go')}</div>
      <div class="form-actions">${act('refresh-widget', 'Sync now', 'data-id="vinyl"')}<button class="btn btn--solid" type="submit">Save</button></div></form>`);
}

function purchasesCard() {
  return card('Vinyl pre-orders — add a purchase', 'Records bought elsewhere. They appear under Confirmed Pre-orders on the Vinyl radar tile.',
    `<form class="form" data-form="purchase" style="padding:0 18px 12px"><div class="form-grid">
      ${field('Artist', input('artist', '', 'required maxlength="80"'))}
      ${field('Title', input('title', '', 'required maxlength="200"'))}
      ${field('Variant (optional)', input('variant', '', 'maxlength="120" placeholder="e.g. Ltd. clear vinyl"'))}
      ${field('Release date (optional — leave blank if TBC)', input('date', '', 'type="date"'))}
      ${field('Store/product page link (optional)', input('product_url', '', 'type="url" placeholder="https://…"'), '', 'wide')}
      ${field('Cover image address (optional)', input('image_url', '', 'type="url" placeholder="https://…"'), '', 'wide')}
      </div><div class="form-actions"><button class="btn btn--solid" type="submit">Add purchase</button></div></form>`);
}

function radarCard() {
  const s = cfg.widgets.radar.settings;
  return card('Gig radar — Ticketmaster', 'Needs a free API key from <a href="https://developer.ticketmaster.com/" target="_blank" rel="noopener">developer.ticketmaster.com</a>.',
    `${statusLine(live?.radar, 'radar')}<form class="form" data-form="radar" style="padding:0 18px 12px"><div class="form-grid">
      ${field('API key', input('api_key', '', `type="password" autocomplete="off" spellcheck="false" placeholder="${s.has_api_key ? '•••• saved — leave blank to keep' : 'paste your Consumer Key'}"`), '', 'wide')}
      ${field('Your location (latitude, longitude)', input('latlong', s.latlong, 'placeholder="53.4808,-2.2426"'), 'Google Maps: right-click a place to copy its coordinates.')}
      ${field('Radius (miles)', input('radius_miles', s.radius_miles, 'type="number" min="5" max="500"'))}
      ${field('Artists per sync', input('batch', s.batch, 'type="number" min="5" max="300"'))}
      </div><div class="form-actions">${act('refresh-widget', 'Sync now', 'data-id="radar"')}<button class="btn btn--solid" type="submit">Save</button></div></form>`);
}

// ---- News: front page, F1 spoiler shield, what it's learned, sections and their feeds -------------------------------------
const lines = text => String(text || '').split('\n').map(l => l.trim()).filter(Boolean);
const shortGp = name => String(name || '').replace(/ Grand Prix.*/, ' GP');
const when = epoch => new Date(epoch * 1000).toLocaleString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });

function newsFrontCard() {
  const s = cfg.widgets.news.settings;
  return card('News — front page', 'What the <a href="/#news">News page</a> opens on: the BBC’s <b>Most read</b> top 10 beside each section’s lead stories. Each section’s own tab shows its latest stories, with near-duplicates from different outlets merged into one.',
    `${statusLine(live?.news, 'news', 'Feeds')}${statusLine(live?.mostread, 'mostread', 'Most read')}<form class="form" data-form="news-front" style="padding:0 18px 12px">
      <div class="checks">${check('most_read', 'Show the BBC’s Most read top 10', s.most_read, 'Read from bbc.co.uk once an hour.')}</div>
      <div class="form-grid">
        ${field('Stories per section on the front page', select('front_per_section', [2, 3, 4, 5, 6, 8], s.front_per_section))}
        ${field('A section’s “latest” covers', select('latest_hours', [[12, '12 hours'], [24, '24 hours'], [48, '2 days'], [72, '3 days'], [168, 'a week']], s.latest_hours))}
        ${field('Stories shown per section', select('latest_cap', [10, 15, 20, 30, 40, 50], s.latest_cap), 'The rest are a click away in the Archive.')}
      </div><div class="form-actions">${act('refresh-widget', 'Sync now', 'data-id="news"')}<button class="btn btn--solid" type="submit">Save</button></div></form>`);
}

function newsShieldCard() {
  const s = cfg.widgets.news.settings, f1 = newsExtra.f1;
  if (!f1) return card('F1 spoiler shield', '', '<p class="sx__intro">Couldn’t load the shield’s status.</p>');
  const sh = f1.shield || {};
  const status = !f1.enabled ? '<div class="status-line"><span class="dot"></span>Off — F1 stories show as normal.</div>'
    : sh.locked && sh.reason !== 'race' ? `<div class="status-line status-line--err"><span class="dot dot--warn"></span>On, hiding every F1-looking story: the calendar ${sh.reason === 'stale' ? 'is out of date' : 'hasn’t loaded yet'}.</div>`
    : sh.locked ? `<div class="status-line status-line--err"><span class="dot dot--warn"></span>Locked: ${esc(sh.rounds.map(r => shortGp(r.name)).join(', '))} — F1 stories since ${esc(when(sh.since))} are hidden until you mark it watched.</div>`
    : `<div class="status-line"><span class="dot dot--ok"></span>Ready${sh.next ? ` — locks at ${esc(shortGp(sh.next.name))} ${esc((sh.next.first_label || '').toLowerCase())}, ${esc(when(sh.next.first_session))}` : ''}.</div>`;
  const now = Date.now() / 1000;
  const started = (f1.rounds || []).filter(r => r.first_session && r.first_session <= now);
  const watched = s.f1_watched && s.f1_watched.season === f1.season ? s.f1_watched.round : '';
  const opts = [['', 'Not set — races over a week old count as watched'], [0, 'None this season yet'], ...started.map(r => [r.round, `Round ${r.round} · ${shortGp(r.name)} (${new Date(r.race * 1000).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })})`])];
  return card('F1 spoiler shield', 'From each race weekend’s first session (sprint qualifying on a sprint weekend), F1 stories are hidden everywhere on the News page — Most read and the Archive included — until you say you’ve watched that race. Hidden headlines never even reach the browser. The calendar comes from the free <a href="https://api.jolpi.ca/" target="_blank" rel="noopener">Jolpica F1 API</a>, once a day.',
    `${status}${statusLine(live?.f1cal, 'f1cal', 'Calendar')}<form class="form" data-form="news-f1" style="padding:0 18px 12px" data-season="${esc(f1.season ?? '')}">
      <div class="checks">${check('f1_shield', 'Hide F1 spoilers', f1.enabled)}</div>
      <div class="form-grid">
        ${field('Watched up to', select('f1_watched', opts, watched), 'Or use “I’ve watched it” on the News page.')}
        ${field('Also treat these as F1 (one per line)', `<textarea class="input" name="f1_extra_terms" rows="3" spellcheck="false" placeholder="e.g. Toto Wolff&#10;Silverstone">${esc((s.f1_extra_terms || []).join('\n'))}</textarea>`, 'Drivers, teams and the weekend’s venue are already known.')}
      </div><div class="form-actions">${act('refresh-widget', 'Update calendar', 'data-id="f1cal"')}<button class="btn btn--solid" type="submit">Save</button></div></form>`);
}

function newsLearningCard() {
  const L = newsExtra.learning;
  if (!L) return '';
  const kindName = { p: 'name', c: 'category', w: 'word', s: 'outlet' };
  const max = Math.max(1, ...L.topics.map(t => Math.abs(t.weight)));
  const topic = t => `<li class="lrn"><span class="lrn__label">${esc(t.label)} <small>${kindName[t.kind] || ''}</small></span>
      <span class="lrn__bar"><i class="${t.weight > 0 ? 'up' : 'down'}" style="width:${Math.round(Math.abs(t.weight) / max * 100)}%"></i></span>
      ${act('forget-topic', ico('x'), `data-key="${esc(t.key)}" aria-label="Forget ${esc(t.label)}" title="Forget this"`, 'btn--icon')}</li>`;
  const liked = L.topics.filter(t => t.weight > 0).slice(0, 15), disliked = L.topics.filter(t => t.weight < 0).slice(0, 15);
  const secName = id => cfg.widgets.news.settings.sections.find(x => x.id === id)?.name || id;
  const rules = L.rules.map(r => `<li class="item" style="grid-template-columns:minmax(0,1fr) auto"><div class="item__main"><b>${esc(r.term)}</b>${r.kind === 'source' ? '<span class="chip">outlet</span>' : ''}<span class="chip">${r.section ? esc(secName(r.section)) : 'everywhere'}</span></div>
      <div class="item__acts">${act('delete-rule', 'Remove', `data-id="${r.id}"`, 'btn--danger')}</div></li>`).join('');
  const secOpts = [['', 'Everywhere'], ...cfg.widgets.news.settings.sections.map(x => [x.id, `Only in ${x.name}`])];
  return card('What the News page has learned', 'Every 👍 or 👎 nudges the names, categories and words in that story (and a little, its outlet). Stories you’ve clearly gone off wait under “held back for you” on their section’s tab rather than disappearing. Old votes fade over a couple of months.',
    `<div class="lrn-cols"><div><h4 class="lrn__h">More of</h4>${liked.length ? `<ul class="lrn-list">${liked.map(topic).join('')}</ul>` : '<p class="sx__intro">Nothing yet — 👍 a story you liked.</p>'}</div>
      <div><h4 class="lrn__h">Less of</h4>${disliked.length ? `<ul class="lrn-list">${disliked.map(topic).join('')}</ul>` : '<p class="sx__intro">Nothing yet.</p>'}</div></div>
     <h4 class="lrn__h" style="padding:6px 18px 0">Never show</h4><ul class="items">${rules || '<li class="item"><span class="dim">No rules yet. Add one here, or pick one after a 👎.</span></li>'}</ul>
     <form class="form" data-form="news-rule" style="padding:0 18px 12px"><div class="form-grid">
       ${field('Never show stories about', input('term', '', 'maxlength="60" placeholder="e.g. Love Island" required'))}
       ${field('Where', select('section', secOpts, ''))}
     </div><div class="form-actions">${L.topics.length ? act('reset-learning', 'Forget everything learned', '', 'btn--danger') : ''}<button class="btn btn--solid" type="submit">Add rule</button></div></form>`);
}

const feedStatus = id => {
  const st = live?.news?.data?.feeds?.[id];
  if (!st) return '';
  return st.ok ? `<span class="feedcfg__st">✓ ${st.last24} in the last 24 h · ${st.items} in the feed</span>` : `<span class="feedcfg__st feedcfg__st--err">✗ ${esc(st.error)}</span>`;
};
const feedRow = (f = {}) => `<div class="feedcfg" data-feed data-id="${esc(f.id || '')}">
  ${input('f_name', f.name || '', 'placeholder="Name" maxlength="40" aria-label="Feed name"')}${input('f_url', f.url || '', 'type="url" placeholder="https://…/feed" aria-label="Feed address"')}
  ${check('f_on', 'On', f.enabled !== false)}<span class="feedcfg__acts">${act('test-feed', 'Test')}${act('remove-feed', 'Remove', '', 'btn--danger')}</span>
  <div class="feedcfg__result">${f.id ? feedStatus(f.id) : ''}</div></div>`;

function sectionBlock(sec, i, n) {
  const s = cfg.widgets.news.settings;
  const have = new Set(sec.feeds.map(f => f.url));
  const sugg = (s.suggested[sec.id] || []).filter(f => !have.has(f.url));
  return `<fieldset class="nsec" data-sec data-id="${esc(sec.id || '')}">
    <div class="nsec__head">${input('s_name', sec.name || '', 'maxlength="30" placeholder="Section name" aria-label="Section name" required')}
      ${check('s_on', 'On', sec.enabled !== false)}${check('s_front', 'On the front page', sec.front !== false)}
      <label class="nsec__with"><span>Front-page tile</span><select class="input" name="s_with" aria-label="Front-page tile">${[['', 'Its own'], ...cfg.widgets.news.settings.sections.filter(x => x.id !== sec.id).map(x => [x.id, `Shared with ${x.name}`])].map(([v, l]) => `<option value="${esc(v)}"${(sec.front_with || '') === v ? ' selected' : ''}>${esc(l)}</option>`).join('')}</select></label>${sec.id === 'music' || sec.personal ? check('s_personal', 'Only stories about my artists', !!sec.personal, 'From Music Companion: artists you play, own on vinyl or have seen live. “Everything” is a click away on the page.') : ''}
      <span class="nsec__acts">${act('move-sec', ico('up'), `data-d="-1" aria-label="Move up" ${i === 0 ? 'disabled' : ''}`, 'btn--icon')}${act('move-sec', ico('down'), `data-d="1" aria-label="Move down" ${i === n - 1 ? 'disabled' : ''}`, 'btn--icon')}${act('remove-sec', 'Remove section', '', 'btn--danger')}</span></div>
    <p class="nsec__hint">Feeds, in order of preference — when several carry the same story, the first one keeps it.</p>
    <div data-feeds>${sec.feeds.map(feedRow).join('')}</div>
    <div class="bar-actions" style="padding:10px 0 4px">${sugg.length ? `<select class="input nsec__sugg" data-suggest aria-label="Suggested sources"><option value="">Add a suggested source…</option>${sugg.map(f => `<option value="${esc(f.url)}" data-name="${esc(f.name)}">${esc(f.name)}</option>`).join('')}</select>` : ''}${act('add-feed', `${ico('plus')}Custom feed`, '', 'btn--go')}</div>
    <div class="form-grid" style="margin-top:8px">
      ${field('Hide stories mentioning (one per line)', `<textarea class="input" name="s_exclude" rows="3" spellcheck="false" placeholder="e.g. Strictly">${esc((sec.exclude || []).join('\n'))}</textarea>`)}
      ${field('Only show stories mentioning (one per line)', `<textarea class="input" name="s_include" rows="3" spellcheck="false" placeholder="Empty = everything">${esc((sec.include || []).join('\n'))}</textarea>`)}
      ${field('Keep in the archive for (days)', input('s_keep', sec.keep_days ?? 30, 'type="number" min="3" max="365"'), 'Saved and 👍 stories are kept for good.')}
    </div>
    <details class="nsec__lean"${(sec.favour || []).length || (sec.play_down || []).length ? ' open' : ''}><summary>Leanings — what this section favours and plays down</summary>
      <div class="form-grid" style="margin-top:8px">
        ${field('Favour (one per line)', `<textarea class="input" name="s_favour" rows="5" spellcheck="false" placeholder="e.g. space&#10;physics">${esc((sec.favour || []).join('\n'))}</textarea>`, 'Stories mentioning these move up.')}
        ${field('Play down (one per line)', `<textarea class="input" name="s_play_down" rows="5" spellcheck="false" placeholder="e.g. climate&#10;weather">${esc((sec.play_down || []).join('\n'))}</textarea>`, 'These sink down the list but aren’t hidden. A word, a feed’s own category (e.g. “Climate change”), or part of an address starting with “/” (e.g. /weather/).')}
      </div></details></fieldset>`;
}

function newsSectionsCard() {
  const s = cfg.widgets.news.settings;
  return card('Sections and sources', `Reputable outlets only: tabloid sites (The Sun, the Mail, the Express, the Star, the Mirror, the Daily Record, Metro) are always refused, and their stories are dropped even if another feed links to them.`,
    `<form class="form" data-form="news-sections" style="padding:0 18px 12px"><div id="nsecs">${s.sections.map((x, i) => sectionBlock(x, i, s.sections.length)).join('')}</div>
      <div class="bar-actions" style="padding:12px 0 0">${act('add-sec', `${ico('plus')}Add section`, '', 'btn--go')}</div>
      <div class="form-actions">${act('refresh-widget', 'Sync now', 'data-id="news"')}<button class="btn btn--solid" type="submit">Save sections</button></div></form>`);
}

const newsTab = () => newsFrontCard() + newsShieldCard() + newsLearningCard() + newsSectionsCard();
/** After a move/add/remove, only the first section can't go up and only the last can't go down. */
function renumberSections() {
  const all = $$('#nsecs [data-sec]');
  all.forEach((el, i) => { const [up, down] = $$('[data-do="move-sec"]', el); up.disabled = i === 0; down.disabled = i === all.length - 1; });
}

// ---- Music page: Spotify, the outside services, playlist defaults ----------------------------------------------------------
const svcResult = id => svcTests[id] ? `<div class="status-line${svcTests[id].ok ? '' : ' status-line--err'}"><span class="dot ${svcTests[id].ok ? 'dot--ok' : 'dot--bad'}"></span>${esc(svcTests[id].message)}</div>` : '';
const svcField = (svc, f) => field(f.label, input(`svc_${f.name}`, f.secret ? '' : svc.values[f.name],
  f.secret ? `type="password" autocomplete="off" spellcheck="false" placeholder="${svc.has[f.name] ? '•••• saved — leave blank to keep' : esc(f.placeholder || 'paste it here')}"` : `placeholder="${esc(f.placeholder)}" spellcheck="false"`));

function spotifyCard() {
  const sp = musicCfg.spotify, svc = musicCfg.services.find(x => x.id === 'spotify');
  const until = sp.login_until ? new Date(sp.login_until * 1000).toLocaleDateString('en-GB', { day: 'numeric', month: 'long', year: 'numeric' }) : '';
  const status = !svc.configured ? '<div class="status-line"><span class="dot"></span>Add your Spotify app’s client ID and secret, then Connect.</div>'
    : sp.expired ? '<div class="status-line status-line--err"><span class="dot dot--bad"></span>The login has run out (they last six months) — Connect again.</div>'
    : sp.connected ? `<div class="status-line${sp.days_left <= 14 ? ' status-line--err' : ''}"><span class="dot ${sp.days_left <= 14 ? 'dot--warn' : 'dot--ok'}"></span>Connected${sp.user ? ` as ${esc(sp.user)}` : ''} — login good until ${esc(until)} (${sp.days_left} days)</div>`
    : '<div class="status-line"><span class="dot dot--warn"></span>Not connected yet.</div>';
  const link = musicLink ? `<div class="form-grid" style="margin-top:12px">${field('Open this in a browser on the Citadel PC', `<input class="input" readonly data-select-all value="${esc(musicLink)}">`,
    'Spotify only sends logins back to 127.0.0.1, so it has to finish on this PC. Afterwards every device can use the Music page.', 'wide')}</div>
    <div class="form-actions"><a class="btn btn--go" href="${esc(musicLink)}" target="_blank" rel="noopener">Open the login page</a></div>` : '';
  return card('Spotify', `Uses your own Spotify app (<a href="https://developer.spotify.com/dashboard" target="_blank" rel="noopener">developer dashboard</a>; Premium needed). Add this <b>Redirect URI</b> to the app’s settings:`,
    `<div style="padding:0 18px 10px"><input class="input" readonly data-select-all value="${esc(sp.redirect_uri)}" aria-label="Redirect URI"></div>${status}${svcResult('spotify')}
    <form class="form" data-form="music-spotify" style="padding:0 18px 12px"><div class="form-grid">${svc.fields.map(f => svcField(svc, f)).join('')}</div>
      <div class="checks">${check('svc_enabled', 'Use Spotify', svc.enabled)}</div>
      <div class="form-actions">${sp.connected ? act('svc-test', 'Test', 'data-id="spotify"') + act('spotify-logout', 'Disconnect', '', 'btn--danger') : ''}
        ${svc.configured ? act('spotify-login', sp.connected ? 'Reconnect' : 'Connect', '', 'btn--go') : ''}<button class="btn btn--solid" type="submit">Save</button></div>${link}</form>`);
}

function playlistDefaultsCard() {
  const s = musicCfg.settings;
  return card('Playlists', '', `<form class="form" data-form="music-defaults" style="padding:0 18px 12px"><div class="form-grid">
      ${field('Name prefix on Spotify', input('prefix', s.prefix, 'maxlength="24"'), 'Every playlist Citadel makes is named with this in front.')}
      ${field('Rotating playlists refresh at', input('rotate_at', s.rotate_at, 'type="time"'))}
      ${field('Start the Spotify desktop app with', input('desktop_command', s.desktop_command, 'spellcheck="false"'), 'Used by Open Spotify on the Music page.', 'wide')}
    </div><div class="checks">${check('public', 'New playlists are public', s.public)}</div>
    <div class="form-actions"><button class="btn btn--solid" type="submit">Save</button></div></form>`);
}

function servicesCard() {
  const rows = musicCfg.services.filter(x => x.id !== 'spotify').map(svc => `<div class="svc-row" data-svc="${esc(svc.id)}">
      <div class="svc-row__head"><b>${esc(svc.name)}</b>${svc.always_on ? '' : check('svc_enabled', 'On', svc.enabled)}${svc.link ? `<a class="feed__link" href="${esc(svc.link)}" target="_blank" rel="noopener">${svc.fields.length ? 'get a key' : 'site'}</a>` : ''}
        <span class="dim">${!svc.configured ? (svc.id === 'companion' ? '· database not found' : '· needs setting up') : ''}</span>${act('svc-test', 'Test', `data-id="${esc(svc.id)}"`)}</div>
      <p class="field__h" style="margin:4px 0 8px">${esc(svc.blurb)}${svc.path ? ` <code>${esc(svc.path)}</code>` : ''}</p>
      ${svc.fields.length ? `<div class="form-grid">${svc.fields.map(f => svcField(svc, f)).join('')}</div>` : ''}${svcResult(svc.id)}</div>`).join('');
  return card('Music services', 'Where the Music page gets what Spotify doesn’t give personal apps any more. Adding a service later is one file in <code>app/music/services/</code>.',
    `<form class="form" data-form="music-services" style="padding:0 18px 12px">${rows}<div class="form-actions"><button class="btn btn--solid" type="submit">Save</button></div></form>`);
}

function musicHousekeepingCard() {
  return card('Housekeeping', '', `<div class="bar-actions">${act('music-discover', 'Refresh Discover now')}${act('music-forget', 'Forget Spotify matches', '', 'btn--danger')}</div>
    <p class="sx__intro">Songs are matched on Spotify once and remembered; forgetting makes them all look again (picks you made by hand are kept).</p>`);
}

const musicPageTab = () => musicCfg ? spotifyCard() + playlistDefaultsCard() + servicesCard() + musicHousekeepingCard()
  : card('Music page', '<span class="spin"></span>', '');

// ---- Screens & devices ------------------------------------------------------------------------------------------
const PANEL_LABELS = { bins: ['Bins', ''], gigs: ['Next gig', ''], diary: ['Diary', 'Anyone who can see the TV will see your events.'] };

function screensCard() {
  const s = cfg.screens;
  return card('TV / screens', '',
    `<form class="form" data-form="screens" style="padding:0 18px 12px">
      <div class="checks"><span class="field__l">Panels</span>${cfg.tv_panels.map(k => check(`panel_${k}`, PANEL_LABELS[k][0], s.panels.includes(k), PANEL_LABELS[k][1])).join('')}
        ${check('show_alerts', 'Show reminders banner (bin night, next event)', s.show_alerts)}</div>
      <div class="form-grid" style="margin-top:14px">${field('Seconds per panel', input('rotate_seconds', s.rotate_seconds, 'type="number" min="5" max="300"'))}</div>
      <div class="form-actions"><a class="btn" href="/tv" target="_blank" rel="noopener">Preview</a><button class="btn btn--solid" type="submit">Save</button></div></form>`);
}

const pairCard = () => card('Pair a screen',
  'On the TV, open <code>' + esc(cfg.public_url) + '/tv</code> and enter the code it shows.',
  `<form class="form" data-form="pair" style="padding:0 18px 14px"><div class="form-grid">
    ${field('Code shown on the TV', input('code', '', 'required maxlength="8" autocomplete="off" placeholder="ABC-123"'))}
    ${field('Name it', input('name', 'Games room TV', 'maxlength="60"'))}</div>
    <div class="form-actions"><button class="btn btn--solid" type="submit">Pair screen</button></div></form>`);

const roleChip = r => (r === 'owner' ? '<span class="chip chip--new">Owner</span>' : '<span class="chip chip--vinyl">Screen</span>');

function devicesCard() {
  const rows = cfg.devices.map(d => `<li class="item"><span class="item__icon">${ico(d.role === 'owner' ? 'home' : 'server')}</span>
    <div class="item__main"><b>${esc(d.name)}</b>${roleChip(d.role)}${d.current ? '<span class="chip">This device</span>' : ''}
      <div class="item__sub">last seen ${esc(ago(d.last_seen))}${d.user_agent ? ` · ${esc(d.user_agent.slice(0, 70))}` : ''}</div></div>
    <div class="item__acts">${act('rename-device', 'Rename', `data-id="${esc(d.id)}" data-name="${esc(d.name)}"`)}${d.current ? '' : act('revoke-device', 'Revoke', `data-id="${esc(d.id)}" data-name="${esc(d.name)}"`, 'btn--danger')}</div></li>`).join('');
  return card('Devices', '', `<ul class="items">${rows}</ul>`);
}

function enrolCard() {
  const result = enrol ? `<div class="enrol"><div class="enrol__qr">${enrol.qr_svg}</div><div>
      <p class="sx__intro" style="padding:0 0 10px">Open on the new device or scan the code. Works once, for 15 minutes.</p>
      <input class="input" readonly value="${esc(enrol.url)}" data-select-all aria-label="Enrolment link"></div></div>` : '';
  return card('Add an owner device', '',
    `<div class="bar-actions">${act('new-enrol', `${ico('play')}Create a one-time link`, '', 'btn--go')}</div>${result}`);
}

// ---- Maintenance & logs ----------------------------------------------------------------------------------------------
const STATUS = { ok: ['dot--ok', 'Working'], running: ['dot--warn', 'Running now'], failing: ['dot--bad', 'Failing'], stale: ['dot--bad', 'Hasn’t synced for a while'],
  waiting: ['dot', 'Waiting for its first run'], 'not set up': ['dot', 'Not set up'] };
const dayStart = d => new Date(d.getFullYear(), d.getMonth(), d.getDate());
/** "04:00", "yesterday 04:00", "tomorrow 04:00", or "Mon 28 Sep 04:00". */
function whenLabel(epoch) {
  if (!epoch) return '—';
  const d = new Date(epoch * 1000), days = Math.round((dayStart(d) - dayStart(new Date())) / 86400000), hm = hhmm(epoch);
  return days === 0 ? `today ${hm}` : days === 1 ? `tomorrow ${hm}` : days === -1 ? `yesterday ${hm}` : `${d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' })} ${hm}`;
}
const dur = s => (s == null ? '' : s < 1 ? '<1 s' : s < 90 ? `${Math.round(s)} s` : `${Math.round(s / 60)} min`);
const minLabel = s => (s >= 3600 ? `${s / 3600} h` : s >= 120 ? `${Math.round(s / 60)} min` : `${s} s`);

/** The cells of a schedule row that change as syncs happen; the editor next to them is never re-rendered under your hands. */
function jobStatusHtml(j) {
  const [dot, label] = STATUS[j.status] || ['dot', j.status];
  const next = j.next_run == null ? '—' : j.next_run <= maint.now ? 'due now' : whenLabel(j.next_run);
  const last = j.last_run ? `${whenLabel(j.last_run)}${j.seconds != null ? ` · ${dur(j.seconds)}` : ''}` : 'never';
  const fail = j.streak ? ` <span class="chip chip--warn">${j.streak} failed${j.streak === 1 ? '' : ' in a row'}</span>` : '';
  return `<span class="dot ${dot}" title="${esc(label)}"></span><div><span class="sched__state">${esc(label)}</span>${fail}
    <div class="item__sub sched__msg" title="${esc(j.message)}">${esc(j.message || (j.status === 'not set up' ? (j.hint || 'Add its key or address on its settings page') : ''))}</div>
    <div class="item__sub">last ${esc(last)} · next ${esc(next)}</div></div>`;
}

function scheduleEditor(j) {
  const sc = j.schedule, every = sc.mode === 'every';
  const unitS = !every ? 3600 : sc.seconds % 3600 === 0 ? 3600 : sc.seconds % 60 === 0 ? 60 : 1, amount = every ? sc.seconds / unitS : 1;     // the biggest unit that divides evenly
  return `<form class="sched__edit" data-form="schedule" data-id="${esc(j.id)}" aria-label="Schedule for ${esc(j.title)}">
    <select class="input input--sm" name="mode" data-sched-mode aria-label="How often">${[['daily', 'Daily at'], ['every', 'Every']].map(([v, l]) => `<option value="${v}"${sc.mode === v ? ' selected' : ''}>${l}</option>`).join('')}</select>
    <input class="input input--sm" type="time" name="at" value="${esc(sc.mode === 'daily' ? sc.at : (j.default_text.match(/\d\d:\d\d/) || ['04:00'])[0])}" ${every ? 'hidden' : ''} aria-label="Time of day">
    <span class="sched__every" ${every ? '' : 'hidden'}><input class="input input--sm" type="number" name="amount" min="1" max="10080" value="${amount}" aria-label="Interval"><select class="input input--sm" name="unit" aria-label="Unit">${[[1, 'sec'], [60, 'min'], [3600, 'hours']].map(([v, l]) => `<option value="${v}"${unitS === v ? ' selected' : ''}>${l}</option>`).join('')}</select></span>
    <button class="btn" type="submit">Save</button></form>`;
}

function jobRow(j) {
  const kind = j.local ? '<span class="chip">local</span>' : '';
  return `<li class="sched" data-job="${esc(j.id)}">
    <div class="sched__name"><b>${esc(j.title)}</b>${kind}<div class="item__sub">${esc(j.schedule_text)}${j.schedule_text === j.default_text ? '' : ` · default ${esc(j.default_text)}`}${j.min_interval >= 3600 ? ` · at most every ${esc(minLabel(j.min_interval))}` : ''}</div></div>
    ${scheduleEditor(j)}
    <div class="sched__status" data-job-status="${esc(j.id)}">${jobStatusHtml(j)}</div>
    <div class="item__acts">${act('run-job', 'Run now', `data-id="${esc(j.id)}" title="Run ${esc(j.title)} now"`)}${j.schedule_text === j.default_text ? '' : act('sched-default', 'Default', `data-id="${esc(j.id)}" title="Back to ${esc(j.default_text)}"`)}</div></li>`;
}

function scheduleCard() {
  const sync = maint.jobs.filter(j => j.kind === 'sync'), house = maint.jobs.filter(j => j.kind === 'maintenance');
  const bad = maint.jobs.filter(j => ['failing', 'stale'].includes(j.status));
  const banner = bad.length ? `<div class="status-line status-line--err"><span class="dot dot--bad"></span>${bad.map(j => `<b>${esc(j.title)}</b>`).join(', ')} ${bad.length === 1 ? 'needs' : 'need'} attention — see the message beneath ${bad.length === 1 ? 'it' : 'each'}, and the log below.</div>` : '';
  return card('Schedule', '',
    `${banner}<div class="sched__head"><span>Job</span><span>Schedule</span><span>Status</span><span></span></div>
     <ul class="items sched__list">${sync.map(jobRow).join('')}</ul>
     <div class="sx__sub hud">Housekeeping</div><ul class="items sched__list">${house.map(jobRow).join('')}</ul>
     <p class="sx__intro">Backups (last 7): <code>${esc(cfg.paths.database.replace(/[^/]+$/, ''))}backups/</code> — ${maint.backups.length ? esc(maint.backups.join(', ')) : 'none yet'}. To restore: stop the service, copy one over <code>hub.sqlite</code>, start it.</p>`,
    `<button class="btn" type="button" data-do="maint-refresh">Refresh</button>`);
}

const LEVEL_CHIP = { info: '<span class="chip">info</span>', warning: '<span class="chip chip--warn">warning</span>', error: '<span class="chip chip--bad">error</span>' };
function logRowHtml(e) {
  const d = new Date(e.ts * 1000);
  const when = `${d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })} ${hhmm(e.ts)}`;
  return `<li class="logrow logrow--${esc(e.level)}"><span class="logrow__t mono">${esc(when)}</span>${LEVEL_CHIP[e.level] || ''}<span class="logrow__s mono">${esc(e.source)}</span>
    <div class="logrow__m">${esc(e.message)}${e.detail ? `<details><summary>details</summary><pre>${esc(e.detail)}</pre></details>` : ''}</div></li>`;
}
function logListHtml() {
  if (!logs.items.length) return `<li class="item"><span class="field__h">${logf.level === 'warning' && !logf.q && !logf.source ? 'No warnings or errors — all quiet.' : 'Nothing matches that filter.'}</span></li>`;
  return logs.items.map(logRowHtml).join('') + (logs.items.length >= 200 ? '<li class="item"><span class="field__h">Showing the newest 200 — narrow the filter to see older entries.</span></li>' : '');
}

function logCard() {
  const c = logs.counts || {};
  const seg = [['warning', 'Problems'], ['all', 'Everything'], ['error', 'Errors only']].map(([v, l]) => `<button type="button" class="btn${logf.level === v ? ' is-on' : ''}" data-do="log-level" data-level="${v}">${l}</button>`).join('');
  return card('Event log', `Last 24 hours: <b>${c.error || 0}</b> errors, <b>${c.warning || 0}</b> warnings.`,
    `<div class="bar-actions logbar">${seg}
       <select class="input input--sm" data-log-source aria-label="Source"><option value="">All sources</option>${logs.sources.map(x => `<option value="${esc(x)}"${logf.source === x ? ' selected' : ''}>${esc(x)}</option>`).join('')}</select>
       <input class="input input--sm" data-log-q placeholder="Search…" value="${esc(logf.q)}" style="max-width:200px" aria-label="Search the log">
       ${act('maint-refresh', 'Refresh')}${act('log-clear', 'Clear log', '', 'btn--danger')}</div>
     <ul class="logrows" id="loglist">${logListHtml()}</ul>`);
}

const maintenanceTab = () => scheduleCard() + logCard();
const needsAttention = () => maint.jobs.some(j => ['failing', 'stale'].includes(j.status));
const logUrl = () => `/api/owner/logs?level=${logf.level}&limit=200${logf.source ? `&source=${encodeURIComponent(logf.source)}` : ''}${logf.q ? `&q=${encodeURIComponent(logf.q)}` : ''}`;
async function loadMaint() { [maint, logs] = await Promise.all([api.get('/api/owner/maintenance'), api.get(logUrl())]); }
/** Re-read and repaint just the changing parts (status cells + the log list) — never the schedule editors. */
async function paintMaint() {
  await loadMaint();
  maint.jobs.forEach(j => { const el = $(`[data-job-status="${CSS.escape(j.id)}"]`); if (el) el.innerHTML = jobStatusHtml(j); });
  const list = $('#loglist'); if (list) list.innerHTML = logListHtml();
  const tabBtn = $('[data-tab="maintenance"]'); if (tabBtn) tabBtn.classList.toggle('has-attn', needsAttention());
}

// ---- Appearance: themes and background pictures ---------------------------------------------------------------------
const FIT_LABELS = { cover: 'Fill the screen', contain: 'Show all of it', tile: 'Tile', center: 'Centre, actual size' };
const BUILTIN_LOOK = { dark: { panel: '#0e0b14', accent: '#8b5cf6' }, light: { panel: '#f8f0e2', accent: '#b5541f' } };

function themePreview(t) {
  const look = t.accent ? t : { ...t, ...BUILTIN_LOOK[t.id] };
  const bg = t.background?.file ? `<img src="/backgrounds/${esc(t.background.file)}" alt="">` : '';
  return `<span class="tp" style="--tp-panel:${esc(look.panel)};--tp-accent:${esc(look.accent)};--tp-base:${look.base === 'light' ? '#f2e8d5' : '#07060a'}">${bg}
    <span class="tp__card"><span class="tp__tick"></span><span class="tp__line"></span><span class="tp__line tp__line--short"></span></span></span>`;
}

function themeForm(t) {
  const base = t.base || 'dark', bg = t.background || {};
  const panel = t.panel || cfg.appearance.base_panel[base], accent = t.accent || cfg.appearance.base_accent[base];
  const pct = (n, v, min, max, step) => `<span class="range"><input type="range" name="${n}" min="${min}" max="${max}" step="${step}" value="${v}"><output>${v}%</output></span>`;
  const ownPanel = t.panel && t.panel !== cfg.appearance.base_panel[base];       // a chosen panel colour isn't reset by switching Dark/Light
  return `<form class="form" data-form="theme" data-id="${esc(t.id || '')}"${ownPanel ? ' data-panel-touched="1"' : ''}>
    <div class="form-grid">
      ${field('Name', input('name', t.name || '', 'required maxlength="40" placeholder="e.g. Midnight blue"'))}
      ${field('Start from', select('base', [['dark', 'Dark — light text on dark panels'], ['light', 'Light — dark text on light panels']], base), '')}
      ${field('Accent colour', `<span class="colour"><input type="color" name="accent" value="${esc(accent)}"><code>${esc(accent)}</code></span>`, '')}
      ${field('Panel colour', `<span class="colour"><input type="color" name="panel" value="${esc(panel)}"><code>${esc(panel)}</code>${act('panel-reset', 'Reset', 'title="Back to the starting theme’s panel colour"', 'btn--icon-text')}</span>`, '')}
      ${field('Panel see-through', pct('panel_opacity', t.panel_opacity ?? 100, 30, 100, 5), '')}
    </div>
    <div class="field__l" style="margin-top:18px">Background picture</div>
    <div class="bgpick">
      <span class="bgpick__thumb">${bg.file ? `<img src="/backgrounds/${esc(bg.file)}" alt="">` : '<span>None</span>'}</span>
      <label class="btn btn--go">${ico('plus')}Choose a picture…<input type="file" accept="image/jpeg,image/png,image/webp,image/gif" data-bg-file hidden></label>
      ${act('bg-remove', 'Remove', bg.file ? '' : 'hidden')}
      <input type="hidden" name="bg_file" value="${esc(bg.file || '')}">
      <span class="field__h">JPEG, PNG, WebP or GIF, up to ${cfg.appearance.max_background_mb} MB.</span>
    </div>
    <div class="form-grid" style="margin-top:12px">
      ${field('Fit', select('fit', cfg.appearance.fits.map(f => [f, FIT_LABELS[f]]), bg.fit || 'cover'))}
      ${field('Dim the picture', pct('dim', bg.dim ?? 40, 0, 90, 5), '')}
    </div>
    <div class="checks">${check('glow', 'Accent glow in the corners', bg.glow !== false)}</div>
    <p class="field__h" style="margin-top:6px">Previews live; nothing is kept until you save.</p>
    <div class="form-actions">${act('cancel', 'Cancel')}<button class="btn btn--solid" type="submit">${t.id ? 'Save theme' : 'Create theme'}</button></div></form>`;
}

/** The theme the editor form describes right now (for the live preview and for saving). */
function readTheme(f) {
  return { name: val(f, 'name'), base: f.elements.base.value, accent: f.elements.accent.value, panel: f.elements.panel.value,
    panel_opacity: Number(f.elements.panel_opacity.value),
    background: { file: f.elements.bg_file.value, fit: f.elements.fit.value, dim: Number(f.elements.dim.value), glow: f.elements.glow.checked } };
}
function previewTheme(f) {
  applySpec(specOf(readTheme(f)));
  $$('.range output', f).forEach(o => { o.textContent = `${o.previousElementSibling.value}%`; });
  $$('.colour', f).forEach(c => { c.querySelector('code').textContent = c.querySelector('input').value; });
}

function appearanceTab() {
  const themes = cfg.appearance.themes, cur = chosen();
  const tiles = [...BUILTIN, ...themes].map(t => `<button type="button" class="themepick${t.id === cur ? ' is-current' : ''}" data-do="use-theme" data-id="${esc(t.id)}" aria-pressed="${t.id === cur}">
      ${themePreview(t)}<span class="themepick__name">${esc(t.name)}${t.id === cur ? `<small>${ico('check')}On this device</small>` : ''}</span></button>`).join('');
  const rows = themes.map(t => editing?.kind === 'theme' && editing.id === t.id ? `<li class="item item--edit">${themeForm(t)}</li>`
    : `<li class="item"><span class="item__icon item__icon--theme">${themePreview(t)}</span>
        <div class="item__main"><b>${esc(t.name)}</b>${t.id === cur ? '<span class="chip chip--new">This device</span>' : ''}<span class="chip">from ${t.base === 'light' ? 'Light' : 'Dark'}</span>${t.background.file ? '<span class="chip">Background</span>' : ''}
          <div class="item__sub">accent ${esc(t.accent)} · panels ${esc(t.panel)}${t.panel_opacity < 100 ? ` at ${t.panel_opacity}%` : ''}</div></div>
        <div class="item__acts">${act('edit-theme', 'Edit', `data-id="${esc(t.id)}"`)}${act('copy-theme', 'Duplicate', `data-id="${esc(t.id)}"`)}${act('delete-theme', 'Delete', `data-id="${esc(t.id)}" data-name="${esc(t.name)}"`, 'btn--danger')}</div></li>`).join('');
  const adding = editing?.kind === 'theme' && editing.id === 'new' ? `<li class="item item--edit">${themeForm(editing.seed || {})}</li>` : '';
  return card('Theme on this device',
    '',
    `<div class="themepicks">${tiles}</div>`)
    + card('Custom themes',
    '',
    `<div class="bar-actions">${act('new-theme', `${ico('plus')}New theme`, '', 'btn--go')}</div><ul class="items">${adding}${rows}</ul>
     ${themes.length || adding ? '' : '<p class="sx__intro" style="padding-top:0">No custom themes yet.</p>'}`);
}

// ---- About -----------------------------------------------------------------------------------------------------
function aboutTab() {
  const rows = [['Version', cfg.version], ['Host', `${cfg.host} · ${cfg.ip}`], ['Address', cfg.public_url], ['Database', cfg.paths.database], ['Projects folder', cfg.paths.projects],
    ['Links / projects / devices', `${cfg.links.length} / ${cfg.projects.length} / ${cfg.devices.length}`]];
  return card('About', 'To move Citadel, copy <code>data/hub.sqlite</code> and <code>.env</code>.',
    `<ul class="items">${rows.map(([k, v]) => `<li class="item" style="grid-template-columns:200px 1fr"><span class="field__l">${esc(k)}</span><span class="mono" style="word-break:break-all">${esc(v)}</span></li>`).join('')}</ul>`);
}

// ---- rendering ---------------------------------------------------------------------------------------------------
function render() {
  if (editing?.kind !== 'theme') useTheme(chosen(), cfg.appearance.themes);     // ends any live preview; picks up edits made elsewhere
  $('#tabs').innerHTML = TABS.map(([id, label, icon]) => `<button role="tab" data-tab="${id}" aria-selected="${id === tab}"${id === 'maintenance' && needsAttention() ? ' class="has-attn"' : ''}>${ico(icon)}${esc(label)}</button>`).join('');
  panel.innerHTML = { links: linksTab, projects: projectsTab, widgets: () => shortcutsCard() + binsCard() + diaryCard(), appearance: appearanceTab, music: () => gigsCard() + artistsCard() + releasesCard() + vinylCard() + purchasesCard() + radarCard(), listen: musicPageTab, news: newsTab, screens: () => screensCard() + pairCard() + devicesCard() + enrolCard(), maintenance: maintenanceTab, about: aboutTab }[tab]();
}

async function load() {
  musicCfg = await api.get('/api/owner/music').catch(() => null);
  [newsExtra.learning, newsExtra.f1] = await Promise.all([api.get('/api/owner/news/learning').catch(() => null), api.get('/api/owner/news/f1').catch(() => null)]);
  [cfg, live, extras.artists, extras.gigs] = await Promise.all([api.get('/api/owner/config'), api.get('/api/owner/state').then(s => s.widgets), api.get('/api/owner/artists'), api.get('/api/owner/gigs').then(r => r.gigs), loadMaint()]).then(r => r.slice(0, 4));
  render();
}

async function run(fn, ok) {
  try { await fn(); if (ok) toast(ok); await load(); } catch (e) { toast(e.message); }
}

// ---- events ---------------------------------------------------------------------------------------------------------
$('#tabs').addEventListener('click', e => {
  const b = e.target.closest('[data-tab]');
  if (b) { tab = b.dataset.tab; editing = null; history.replaceState(null, '', `#${tab}`); render(); }
});

panel.addEventListener('change', e => {
  const file = e.target.closest('[data-bg-file]');
  if (file?.files?.[0]) {
    const f = file.closest('form'), pic = file.files[0];
    file.value = '';
    if (pic.size > cfg.appearance.max_background_mb * 1_000_000) { toast(`Pictures up to ${cfg.appearance.max_background_mb} MB, please`); return; }
    toast('Uploading…');
    fetch('/api/owner/appearance/backgrounds', { method: 'POST', headers: { 'X-Hub': '1', 'Content-Type': pic.type || 'application/octet-stream' }, body: pic })
      .then(async r => { const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.detail || `Upload failed (${r.status})`); return j; })
      .then(({ file: name }) => {
        f.elements.bg_file.value = name;
        $('.bgpick__thumb', f).innerHTML = `<img src="/backgrounds/${esc(name)}" alt="">`;
        f.querySelector('[data-do="bg-remove"]').hidden = false;
        previewTheme(f); toast('Picture added — save the theme to keep it');
      }).catch(err => toast(err.message));
    return;
  }
  const tf = e.target.closest('[data-form="theme"]');
  if (tf) { previewTheme(tf); return; }
  const scMode = e.target.closest('[name=sc_mode]');
  if (scMode) {
    const row = scMode.closest('[data-sc]');
    row.querySelector('[name=sc_icon]').closest('.field').classList.toggle('is-hidden', scMode.value !== 'builtin');
    row.querySelector('[name=sc_image]').closest('.field').classList.toggle('is-hidden', scMode.value !== 'image');
    return;
  }
  const mode = e.target.closest('[data-sched-mode]');
  if (mode) {                                                                    // show the time box for "Daily at", the number box for "Every"
    const f = mode.form, every = mode.value === 'every';
    f.elements.at.hidden = every; f.querySelector('.sched__every').hidden = !every;
    return;
  }
  const sugg = e.target.closest('[data-suggest]');
  if (sugg) {                                                                    // a suggested source becomes a feed row, ready to save
    const opt = sugg.selectedOptions[0];
    if (opt?.value) { $('[data-feeds]', sugg.closest('[data-sec]')).insertAdjacentHTML('beforeend', feedRow({ name: opt.dataset.name, url: opt.value })); opt.remove(); }
    sugg.value = '';
    return;
  }
  const src = e.target.closest('[data-log-source]');
  if (src) { logf.source = src.value; run(async () => {}); return; }
  const sel = e.target.closest('[data-artist-status]');
  if (sel) run(() => api.patch(`/api/owner/artists/${sel.dataset.id}`, { status: sel.value }), 'Updated');
});
let logTimer;
panel.addEventListener('input', e => {
  const tf = e.target.closest('[data-form="theme"]');
  if (tf && !e.target.matches('[data-bg-file]')) {
    if (e.target.name === 'base' && !tf.dataset.panelTouched) tf.elements.panel.value = cfg.appearance.base_panel[e.target.value];
    if (e.target.name === 'panel') tf.dataset.panelTouched = '1';
    previewTheme(tf);
    return;
  }
  const lq = e.target.closest('[data-log-q]');
  if (lq) { clearTimeout(logTimer); logTimer = setTimeout(() => { logf.q = lq.value.trim(); paintMaint().catch(err => toast(err.message)); }, 300); return; }
  const box = e.target.closest('[data-artist-filter]');
  if (!box) return;
  const q = box.value.trim().toLowerCase();
  $$('[data-artist-row]').forEach(r => { r.hidden = !!q && !r.dataset.artistRow.includes(q); });
});

panel.addEventListener('focusin', e => { if (e.target.matches('[data-select-all]')) e.target.select(); });

panel.addEventListener('click', e => {
  const b = e.target.closest('[data-do]');
  if (!b) return;
  const d = b.dataset;
  switch (b.dataset.do) {
    case 'cancel': editing = null; render(); break;
    case 'use-theme': useTheme(d.id, cfg.appearance.themes); render(); toast('Theme set for this device'); break;
    case 'new-theme': editing = { kind: 'theme', id: 'new', seed: { base: document.documentElement.dataset.theme === 'light' ? 'light' : 'dark' } }; render(); $('[data-form="theme"] [name=name]')?.focus(); previewTheme($('[data-form="theme"]')); break;
    case 'edit-theme': editing = { kind: 'theme', id: d.id }; render(); previewTheme($('[data-form="theme"]')); break;
    case 'copy-theme': { const t = cfg.appearance.themes.find(x => x.id === d.id); editing = { kind: 'theme', id: 'new', seed: { ...t, id: '', name: `${t.name} copy`.slice(0, 40) } }; render(); previewTheme($('[data-form="theme"]')); break; }
    case 'delete-theme': if (confirm(`Delete the “${d.name}” theme? Any device using it goes back to Dark.`)) run(() => api.del(`/api/owner/appearance/themes/${d.id}`), 'Theme deleted'); break;
    case 'panel-reset': { const f = b.closest('form'); f.elements.panel.value = cfg.appearance.base_panel[f.elements.base.value]; previewTheme(f); break; }
    case 'bg-remove': { const f = b.closest('form'); f.elements.bg_file.value = ''; $('.bgpick__thumb', f).innerHTML = '<span>None</span>'; b.hidden = true; previewTheme(f); break; }
    case 'new-link': editing = { kind: 'link', id: 'new' }; render(); $('[data-form="link"] input')?.focus(); break;
    case 'edit-link': editing = { kind: 'link', id: Number(d.id) }; render(); break;
    case 'new-project': editing = { kind: 'project', id: 'new' }; render(); $('[data-form="project"] input')?.focus(); break;
    case 'edit-project': editing = { kind: 'project', id: d.id }; render(); break;
    case 'pin-link': run(() => api.post(`/api/owner/links/${d.id}/pin`, { pinned: d.to === 'true' }), d.to === 'true' ? 'Pinned' : 'Unpinned'); break;
    case 'launchpad-project': run(() => api.post(`/api/owner/projects/${d.id}/launchpad`, { show: d.to === 'true' }), d.to === 'true' ? 'Added to the Launchpad' : 'Taken off the Launchpad'); break;
    case 'pin-project': run(() => api.post(`/api/owner/projects/${d.id}/pin`, { pinned: d.to === 'true' }), d.to === 'true' ? 'Pinned' : 'Unpinned'); break;
    case 'new-gig': editing = { kind: 'gig', id: 'new' }; render(); $('[data-form="gig"] input')?.focus(); break;
    case 'edit-gig': editing = { kind: 'gig', id: Number(d.id) }; render(); break;
    case 'delete-gig': if (confirm(`Delete the ${d.name} gig?`)) run(() => api.del(`/api/owner/gigs/${d.id}`), 'Gig deleted'); break;
    case 'remove-artist': if (confirm(`Stop following ${d.name}? Their found items will disappear.`)) run(() => api.del(`/api/owner/artists/${d.id}`), 'Removed'); break;
    case 'resync': run(async () => { const r = await api.post('/api/owner/artists/resync'); toast(`Music Companion: ${r.added} new artist${r.added === 1 ? '' : 's'} followed, ${r.updated} refreshed`); }); break;
    case 'spotify-login': run(async () => { const r = await api.post('/api/owner/music/spotify/login'); musicLink = r.url;
      if (['127.0.0.1', 'localhost'].includes(location.hostname)) window.open(r.url, '_blank', 'noopener'); }, ['127.0.0.1', 'localhost'].includes(location.hostname) ? 'Opened Spotify’s login page' : 'Open the link below on the Citadel PC'); break;
    case 'spotify-logout': if (confirm('Disconnect Spotify? Playlists stay on your account; Citadel just stops updating them.')) run(async () => { await api.post('/api/owner/music/spotify/logout'); musicLink = null; }, 'Disconnected'); break;
    case 'svc-test': b.disabled = true; b.textContent = 'Testing…';
      api.post(`/api/owner/music/services/${d.id}/test`).then(r => { svcTests[d.id] = r; render(); }).catch(err => toast(err.message)); break;
    case 'music-forget': if (confirm('Forget every Spotify match? They’ll be looked up again as playlists are built.')) run(async () => { const r = await api.del('/api/owner/music/matches'); toast(`Forgot ${r.forgotten} matches`); }); break;
    case 'music-discover': run(() => api.post('/api/owner/music/discover/refresh'), 'Discover is being worked out — a minute or two'); break;
    case 'add-shop': $('#shops').insertAdjacentHTML('beforeend', shopRow()); $('#shops [data-shop]:last-child input')?.focus(); break;
    case 'remove-shop': b.closest('[data-shop]').remove(); break;
    case 'check-shop': {
      const row = b.closest('[data-shop]');
      const url = row.querySelector('[name=s_url]').value.trim(), collection = row.querySelector('[name=s_coll]').value.trim();
      if (!url) { toast('Enter the store’s address first'); break; }
      b.disabled = true; b.textContent = 'Checking…';
      api.post('/api/owner/shops/check', { url, collection, kind: row.dataset.kind || 'shopify' })
        .then(r => toast(r.ok ? `${r.vinyl_preorders} vinyl pre-orders among ${r.products} products (${r.currency || 'currency unknown'}). e.g. ${r.sample.slice(0, 2).join(' · ')}${r.note ? ` — ${r.note}` : ''}` : r.error))
        .catch(err => toast(err.message))
        .finally(() => { b.disabled = false; b.textContent = 'Check'; });
      break;
    }
    case 'add-feed': { const list = $('[data-feeds]', b.closest('[data-sec]')); list.insertAdjacentHTML('beforeend', feedRow()); $('[data-feed]:last-child input', list)?.focus(); break; }
    case 'remove-feed': b.closest('[data-feed]').remove(); break;
    case 'test-feed': {
      const row = b.closest('[data-feed]'), url = row.querySelector('[name=f_url]').value.trim(), out = $('.feedcfg__result', row);
      if (!url) { toast('Enter the feed’s address first'); break; }
      b.disabled = true; b.textContent = 'Testing…';
      api.post('/api/owner/news/test-feed', { url })
        .then(r => {
          out.innerHTML = r.ok ? `<span class="feedcfg__st">✓ ${esc(r.title || 'A feed')} — ${r.items} stories, ${r.last24} in the last 24 h${r.images ? `, ${r.images} with pictures` : ''}. e.g. “${esc(r.sample[0] || '')}”</span>` : `<span class="feedcfg__st feedcfg__st--err">✗ ${esc(r.error)}</span>`;
          const name = row.querySelector('[name=f_name]');
          if (r.ok && !name.value.trim() && r.title) name.value = r.title.slice(0, 40);
        })
        .catch(err => toast(err.message)).finally(() => { b.disabled = false; b.textContent = 'Test'; });
      break;
    }
    case 'move-sec': {
      const sec = b.closest('[data-sec]'), other = Number(d.d) < 0 ? sec.previousElementSibling : sec.nextElementSibling;
      if (other) { Number(d.d) < 0 ? other.before(sec) : other.after(sec); renumberSections(); }
      break;
    }
    case 'remove-sec': {
      const sec = b.closest('[data-sec]'), name = sec.querySelector('[name=s_name]').value.trim() || 'this section';
      if ($$('[data-sec]').length < 2) { toast('Keep at least one section'); break; }
      if (confirm(`Remove ${name}? Its stories go once you save (anything saved or liked stays in the Archive).`)) { sec.remove(); renumberSections(); }
      break;
    }
    case 'add-sec': {
      $('#nsecs').insertAdjacentHTML('beforeend', sectionBlock({ name: '', enabled: true, front: true, keep_days: 30, feeds: [], include: [], exclude: [] }, 1, 1));
      renumberSections(); $('#nsecs [data-sec]:last-child [name=s_name]')?.focus();
      break;
    }
    case 'forget-topic': run(() => api.del(`/api/owner/news/learning/topics?key=${encodeURIComponent(d.key)}`), 'Forgotten'); break;
    case 'delete-rule': run(() => api.del(`/api/owner/news/rules/${d.id}`), 'Rule removed'); break;
    case 'reset-learning': if (confirm('Forget everything the News page has learned from your 👍 and 👎? Your “Never show” rules stay.')) run(() => api.post('/api/owner/news/learning/reset'), 'Learning reset'); break;
    case 'delete-link': if (confirm(`Delete the “${d.name}” link?`)) run(() => api.del(`/api/owner/links/${d.id}`), 'Link deleted'); break;
    case 'delete-project': if (confirm(`Delete “${d.name}”? It will be stopped first. Your project's files are not touched.`)) run(() => api.del(`/api/owner/projects/${d.id}`), 'Project deleted'); break;
    case 'move-link': case 'move-project': {
      const isLink = b.dataset.do === 'move-link';
      const list = isLink ? cfg.links : cfg.projects;
      const i = Number(d.i), j = i + Number(d.d);
      if (j < 0 || j >= list.length) break;
      [list[i], list[j]] = [list[j], list[i]];
      run(() => api.post(`/api/owner/${isLink ? 'links' : 'projects'}/reorder`, { ids: list.map(x => (isLink ? x.id : x.slug)) }));
      break;
    }
    case 'refresh-widget': run(async () => { await api.post(`/api/owner/widgets/${d.id}/refresh`); await new Promise(r => setTimeout(r, 2500)); }, 'Syncing…'); break;
    case 'run-job': run(async () => { await api.post(`/api/owner/maintenance/${d.id}/run`); await new Promise(r => setTimeout(r, 1500)); }, 'Started'); break;
    case 'sched-default': run(() => api.put(`/api/owner/maintenance/${d.id}/schedule`, { mode: 'default' }), 'Back to the default schedule'); break;
    case 'maint-refresh': paintMaint().then(() => toast('Refreshed')).catch(err => toast(err.message)); break;
    case 'log-level': logf.level = d.level; run(async () => {}); break;
    case 'log-clear': if (confirm('Clear the whole event log?')) run(() => api.del('/api/owner/logs'), 'Log cleared'); break;
    case 'add-cal': $('#cals').insertAdjacentHTML('beforeend', calRow()); break;
    case 'sc-add': $('#scs').insertAdjacentHTML('beforeend', scRow()); $('#scs [data-sc]:last-child [name=sc_label]')?.focus(); break;
    case 'sc-remove': b.closest('[data-sc]').remove(); break;
    case 'sc-up': { const r = b.closest('[data-sc]'); if (r.previousElementSibling) r.previousElementSibling.before(r); break; }
    case 'sc-down': { const r = b.closest('[data-sc]'); if (r.nextElementSibling) r.nextElementSibling.after(r); break; }
    case 'sc-import': {
      const have = new Set($$('[data-sc] [name=sc_url]').map(i => i.value.trim()));
      const fresh = cfg.links.filter(l => !have.has(l.url));
      fresh.forEach(l => $('#scs').insertAdjacentHTML('beforeend', scRow({ label: l.name, url: l.url, icon_mode: 'site' })));
      toast(fresh.length ? `Added ${fresh.length} — press Save to keep them` : 'Every link is already here');
      break;
    }
    case 'remove-cal': b.closest('[data-cal]').remove(); break;
    case 'new-enrol': run(async () => { enrol = await api.post('/api/owner/devices/enrol', { role: 'owner', name: 'New owner device' }); }); break;
    case 'rename-device': { const n = prompt('Name this device', d.name); if (n?.trim()) run(() => api.patch(`/api/owner/devices/${d.id}`, { name: n.trim() }), 'Renamed'); break; }
    case 'revoke-device': if (confirm(`Revoke “${d.name}”? It will be signed out straight away.`)) run(() => api.del(`/api/owner/devices/${d.id}`), 'Device revoked'); break;
  }
});

function parseEnv(text) {
  const env = {};
  for (const line of text.split('\n').map(l => l.trim()).filter(Boolean)) {
    const i = line.indexOf('=');
    if (i < 1) throw new Error(`Environment: “${line}” should look like KEY=value`);
    env[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return env;
}

panel.addEventListener('submit', e => {
  e.preventDefault();
  const f = e.target, kind = f.dataset.form;
  if (kind === 'link') {
    const body = { name: val(f, 'name'), subtitle: val(f, 'subtitle'), url: val(f, 'url'), icon: val(f, 'icon'), health_url: val(f, 'health_url') || null,
      is_primary: on(f, 'is_primary'), show_guest: on(f, 'show_guest'), tier: on(f, 'more') ? 'more' : 'main', pinned: on(f, 'pinned'), for_media: on(f, 'for_media'), for_music: on(f, 'for_music'), for_sql: on(f, 'for_sql'), for_ai: on(f, 'for_ai') };
    const id = f.dataset.id;
    run(async () => { await (id ? api.put(`/api/owner/links/${id}`, body) : api.post('/api/owner/links', body)); editing = null; }, 'Link saved');
  } else if (kind === 'project') {
    let env;
    try { env = parseEnv(val(f, 'env')); } catch (err) { toast(err.message); return; }
    const body = { name: val(f, 'name'), description: val(f, 'description'), kind: val(f, 'kind'), url: val(f, 'url') || null,
      port: val(f, 'port') ? Number(val(f, 'port')) : null, cwd: val(f, 'cwd'), command: val(f, 'command'), env, lan_visible: !on(f, 'local_only'), enabled: on(f, 'enabled'), pinned: on(f, 'pinned'),
      show_projects: on(f, 'show_projects'), show_launchpad: on(f, 'show_launchpad'), icon: val(f, 'icon'), for_music: on(f, 'for_music'), for_sql: on(f, 'for_sql') };
    const slug = f.dataset.slug;
    run(async () => {
      const res = await (slug ? api.put(`/api/owner/projects/${slug}`, body) : api.post('/api/owner/projects', { ...body, slug: val(f, 'slug') }));
      editing = null;
      if (res.warnings?.length) setTimeout(() => toast(res.warnings.join(' ')), 300);
    }, 'Project saved');
  } else if (kind === 'theme') {
    const t = readTheme(f), id = f.dataset.id;
    run(async () => {
      const saved = await (id ? api.put(`/api/owner/appearance/themes/${id}`, t) : api.post('/api/owner/appearance/themes', t));
      editing = null;
      if (!id) useTheme(saved.id, [saved]);                // a brand-new theme: use it here straight away
    }, id ? 'Theme saved' : 'Theme created — now in use on this device');
  } else if (kind === 'shortcuts') {
    const shortcuts = $$('[data-sc]', f).map(r => ({
      id: r.dataset.id || undefined, label: r.querySelector('[name=sc_label]').value.trim(), url: r.querySelector('[name=sc_url]').value.trim(),
      icon_mode: r.querySelector('[name=sc_mode]').value, icon: r.querySelector('[name=sc_icon]').value, image: r.querySelector('[name=sc_image]').value.trim(),
    })).filter(x => x.url || x.label);                                        // an untouched "Add shortcut" row is ignored
    run(() => api.put('/api/owner/widgets/shortcuts/settings', { show: on(f, 'sc_show'), shortcuts }), 'Shortcuts saved — fetching icons');
  } else if (kind === 'bins') {
    run(() => api.put('/api/owner/widgets/bins/settings', { uprn: val(f, 'uprn'), put_out_hour: Number(val(f, 'put_out_hour')),
      alert_streams: Object.keys(cfg.streams).filter(k => on(f, `stream_${k}`)) }), 'Bins saved — syncing');
  } else if (kind === 'diary') {
    const calendars = $$('[data-cal]', f)
      .map(r => ({ id: r.dataset.id || undefined, name: r.querySelector('[name=cal_name]').value.trim() || 'Calendar', colour: r.querySelector('[name=cal_colour]').value,
        ...(r.querySelector('[name=cal_url]').value.trim() ? { url: r.querySelector('[name=cal_url]').value.trim() } : {}) }))
      .filter(c => c.id || c.url);                                          // an untouched "Add calendar" row is ignored
    run(() => api.put('/api/owner/widgets/diary/settings', { calendars, horizon_days: Number(val(f, 'horizon_days')) || 60 }), 'Calendars saved — syncing');
  } else if (kind === 'screens') {
    run(() => api.put('/api/owner/screens', { panels: cfg.tv_panels.filter(k => on(f, `panel_${k}`)), rotate_seconds: Number(val(f, 'rotate_seconds')) || 12, show_alerts: on(f, 'show_alerts') }), 'Screen settings saved');
  } else if (kind === 'gig') {
    const body = { artist: val(f, 'artist'), date: val(f, 'date'), venue: val(f, 'venue'), city: val(f, 'city'), doors: val(f, 'doors'), ticket_url: val(f, 'ticket_url') || null, notes: val(f, 'notes') };
    const id = f.dataset.id;
    run(async () => { await (id ? api.put(`/api/owner/gigs/${id}`, body) : api.post('/api/owner/gigs', body)); editing = null; }, 'Gig saved');
  } else if (kind === 'purchase') {
    const body = { artist: val(f, 'artist'), title: val(f, 'title'), variant: val(f, 'variant'), date: val(f, 'date') || null, image_url: val(f, 'image_url') || null, product_url: val(f, 'product_url') || null };
    run(async () => { await api.post('/api/owner/vinyl/preorders', body); }, 'Added — see Confirmed Pre-orders on Vinyl radar');
  } else if (kind === 'artist-add') {
    run(async () => { await api.post('/api/owner/artists', { name: val(f, 'name') }); }, 'Following — checking for new music');
  } else if (kind === 'releases') {
    run(() => api.put('/api/owner/widgets/releases/settings', { lookback_days: Number(val(f, 'lookback_days')), horizon_days: Number(val(f, 'horizon_days')), batch: Number(val(f, 'batch')), types: ['Album', 'EP', 'Single'].filter(t => on(f, `type_${t}`)), include_extras: on(f, 'include_extras') }), 'Saved');
  } else if (kind === 'vinyl') {
    const shops = $$('[data-shop]', f).map(r => ({ id: r.dataset.id || undefined, kind: r.dataset.kind || 'shopify', name: r.querySelector('[name=s_name]').value.trim(), url: r.querySelector('[name=s_url]').value.trim(),
      collection: r.querySelector('[name=s_coll]').value.trim(), enabled: r.querySelector('[name=s_on]').checked })).filter(x => x.name || x.url);
    run(() => api.put('/api/owner/widgets/vinyl/settings', { shops }), 'Saved — syncing');
  } else if (kind === 'radar') {
    run(() => api.put('/api/owner/widgets/radar/settings', { latlong: val(f, 'latlong'), radius_miles: Number(val(f, 'radius_miles')), batch: Number(val(f, 'batch')), api_key: val(f, 'api_key') }), 'Saved — syncing');
  } else if (kind === 'news-front') {
    run(() => api.put('/api/owner/widgets/news/settings', { most_read: on(f, 'most_read'), front_per_section: Number(val(f, 'front_per_section')), latest_hours: Number(val(f, 'latest_hours')), latest_cap: Number(val(f, 'latest_cap')) }), 'Saved');
  } else if (kind === 'news-f1') {
    const round = val(f, 'f1_watched'), season = Number(f.dataset.season);
    run(() => api.put('/api/owner/widgets/news/settings', { f1_shield: on(f, 'f1_shield'), f1_extra_terms: lines(val(f, 'f1_extra_terms')),
      f1_watched: round === '' || !season ? null : { season, round: Number(round) } }), 'Saved');
  } else if (kind === 'news-rule') {
    run(async () => { await api.post('/api/owner/news/rules', { term: val(f, 'term'), section: val(f, 'section') || null }); }, 'Rule added');
  } else if (kind === 'news-sections') {
    const sections = $$('[data-sec]', f).map(el => ({
      id: el.dataset.id || undefined, name: el.querySelector('[name=s_name]').value.trim(), enabled: el.querySelector('[name=s_on]').checked,
      front: el.querySelector('[name=s_front]').checked, keep_days: Number(el.querySelector('[name=s_keep]').value) || 30,
      exclude: lines(el.querySelector('[name=s_exclude]').value), include: lines(el.querySelector('[name=s_include]').value),
      favour: lines(el.querySelector('[name=s_favour]').value), play_down: lines(el.querySelector('[name=s_play_down]').value),
      personal: !!el.querySelector('[name=s_personal]')?.checked, front_with: el.querySelector('[name=s_with]')?.value || null,
      feeds: $$('[data-feed]', el).map(r => ({ id: r.dataset.id || undefined, name: r.querySelector('[name=f_name]').value.trim(), url: r.querySelector('[name=f_url]').value.trim(),
        enabled: r.querySelector('[name=f_on]').checked })).filter(x => x.name || x.url),
    }));
    run(() => api.put('/api/owner/widgets/news/settings', { sections }), 'Saved — syncing');
  } else if (kind === 'schedule') {
    const every = val(f, 'mode') === 'every';
    const body = every ? { mode: 'every', seconds: Math.round(Number(val(f, 'amount')) * Number(val(f, 'unit'))) } : { mode: 'daily', at: val(f, 'at') };
    run(() => api.put(`/api/owner/maintenance/${f.dataset.id}/schedule`, body), 'Schedule saved');
  } else if (kind === 'music-spotify') {
    const svc = { enabled: on(f, 'svc_enabled') };
    musicCfg.services.find(x => x.id === 'spotify').fields.forEach(x => { svc[x.name] = val(f, `svc_${x.name}`); });
    run(() => api.put('/api/owner/music/settings', { services: { spotify: svc } }), 'Spotify settings saved');
  } else if (kind === 'music-defaults') {
    run(() => api.put('/api/owner/music/settings', { prefix: f.elements.prefix.value, public: on(f, 'public'), rotate_at: val(f, 'rotate_at'), desktop_command: val(f, 'desktop_command') }), 'Saved');
  } else if (kind === 'music-services') {
    const services = {};
    $$('[data-svc]', f).forEach(row => {
      const svc = musicCfg.services.find(x => x.id === row.dataset.svc), out = {};
      if (!svc.always_on) out.enabled = !!row.querySelector('[name=svc_enabled]')?.checked;
      svc.fields.forEach(x => { out[x.name] = row.querySelector(`[name=svc_${x.name}]`).value.trim(); });
      services[svc.id] = out;
    });
    run(() => api.put('/api/owner/music/settings', { services }), 'Saved');
  } else if (kind === 'pair') {
    run(async () => { await api.post('/api/owner/pair', { code: val(f, 'code'), name: val(f, 'name') || 'TV' }); }, 'Screen paired');
  }
});

/** When a widget finishes a sync, update its status line in place. Only those lines change — a form being edited is never re-rendered. */
let statusTimer;
api.subscribe(() => {
  clearTimeout(statusTimer);
  statusTimer = setTimeout(async () => {
    try {
      live = (await api.get('/api/owner/state')).widgets;
      $$('[data-status]').forEach(el => { const w = live[el.dataset.status]; if (w) el.outerHTML = statusLine(w, el.dataset.status, el.dataset.label || ''); });
      if (tab === 'maintenance') await paintMaint();
    } catch { /* transient */ }
  }, 500);
}, () => {});

await load();
if (addParam === 'gig') $('[data-form="gig"] input')?.focus();
