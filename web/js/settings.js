// Settings: everything the owner can change without touching files. Each tab is a small render function
// returning HTML; forms submit to the validated owner API and the page re-renders from fresh config.
import * as api from './api.js';
import { $, $$, esc, ico, hhmm, ago, toast, isoOf } from './util.js';

const TABS = [['links', 'Links', 'globe'], ['projects', 'Projects', 'grid'], ['widgets', 'Widgets', 'cal'], ['music', 'Music', 'disc'], ['news', 'News', 'news'], ['screens', 'Screens & devices', 'server'], ['maintenance', 'Maintenance & logs', 'gear'], ['about', 'About', 'chip']];
const panel = $('#panel');
let cfg = null, live = null, extras = { artists: { artists: [], music_db: false }, gigs: [] };   // config, live widget status, artists + gigs
let maint = { jobs: [], log_counts: {}, backups: [], now: 0 }, logs = { items: [], sources: [], counts: {} };   // schedule table + event log
const logf = { level: 'warning', source: '', q: '' };                                                      // the log's current filter
let tab = TABS.some(t => t[0] === location.hash.slice(1)) ? location.hash.slice(1) : 'links';
let editing = null;                     // {kind:'link'|'project', id} | {kind, id:'new'}
let enrol = null;                       // last generated enrolment link

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
      ${field('Status check address (optional)', input('health_url', l.health_url, 'type="url" placeholder="https://…/health"'), 'If set, a status dot shows on the tile — and on the guest page.', 'wide')}
    </div>
    <div class="checks">
      ${check('pinned', 'Pin to the top', l.pinned, 'Pinned tiles always stay first — and always show in the compact Launchpad.')}
      ${check('is_primary', 'Highlight this tile', l.is_primary)}
      ${check('show_guest', 'Show on the guest page', l.show_guest, 'Anyone on your network can see and open it.')}
      ${check('more', 'Only in the expanded Launchpad', l.tier === 'more')}
      ${check('for_media', 'Use as Media view', l.for_media, 'Embedded in the Media page — only one link can be it at a time; picking a new one replaces it.')}
      ${check('for_music', 'Use as Companion view', l.for_music, 'Embedded in the Companion page — only one link can be it at a time; picking a new one replaces it.')}
      ${check('for_sql', 'Use as SQL DB view', l.for_sql, 'Embedded in the SQL DB page — only one link can be it at a time; picking a new one replaces it.')}
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
  return card('Links', 'The tiles on your <b>Launchpad</b>, in order. Mark a link <i>Show on the guest page</i> to share it with visitors — that page shows only those links.',
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
      ${field('Id', input('slug', p.slug, `${isNew ? 'required pattern="[a-z0-9][a-z0-9\\-]*" maxlength="40"' : 'readonly'}`), isNew ? 'Lowercase letters, digits and dashes. Names the systemd unit <code>hub-proj-&lt;id&gt;</code>; can’t be changed later.' : 'Fixed — it names the systemd unit.')}
      ${field('Description', input('description', p.description, 'maxlength="120"'), '', 'wide')}
      ${field('Type', select('kind', [['web', 'Web app (has a port)'], ['desktop', 'Desktop app (window)']], p.kind))}
      ${field('Port', input('port', p.port ?? '', 'type="number" min="1" max="65535"'), 'Checked on this PC to show status. Web apps only.')}
      ${field('Open at', input('url', p.url, 'placeholder="http://{host}:8642/"'), '<code>{host}</code> = the address you opened the dashboard with; <code>{ip}</code> = this PC’s LAN address.', 'wide')}
      ${field('Working directory', input('cwd', p.cwd, 'required placeholder="/home/you/Projects/App"'), p.cwd_exists === false ? '<span style="color:#fda4af">This folder doesn’t exist on this machine.</span>' : '', 'wide')}
      ${field('Start command', `<textarea class="input" name="command" rows="2" required spellcheck="false">${esc(p.command || '')}</textarea>`, 'Run directly, <b>not</b> through a shell — quotes group words, but <code>&&</code>, pipes and <code>$VARS</code> don’t work. Relative program paths (e.g. <code>venv/bin/python</code>) resolve from the working directory.', 'wide')}
      ${field('Environment (optional)', `<textarea class="input" name="env" rows="2" spellcheck="false" placeholder="KEY=value (one per line)">${esc(envText(p.env))}</textarea>`, '', 'wide')}
      ${field('Launchpad icon', select('icon', cfg.icons, p.icon || (p.kind === 'desktop' ? 'play' : 'server')), 'Only matters if "Show in Launchpad" below is on.')}
    </div>
    <div class="checks">
      ${check('pinned', 'Pin to the top', p.pinned, 'Pinned projects stay first in the list — in Projects, and in the Launchpad if shown there too.')}
      ${check('show_projects', 'Show in Projects', p.show_projects, 'Turn off for a standalone app that just gets launched — nothing here to stop. It can still be started from the Launchpad.')}
      ${check('show_launchpad', 'Show in Launchpad', p.show_launchpad, 'A web project gets a plain clickthrough tile; a desktop project’s tile <i>is</i> its start button, since it has no page to link to.')}
      ${check('local_only', 'Only reachable from this PC', !p.lan_visible, 'For apps that listen on 127.0.0.1 only — the link is flagged “this PC only”.')}
      ${check('enabled', 'Show on the dashboard', p.enabled, 'Turning this off also stops it if it is running.')}
    </div>
    <div class="form-actions">${act('cancel', 'Cancel')}<button class="btn btn--solid" type="submit">Save project</button></div></form>`;
}

function projectsTab() {
  const rows = cfg.projects.map((p, i) => editing?.kind === 'project' && editing.id === p.slug
    ? `<li class="item item--edit">${projectForm(p)}</li>`
    : `<li class="item"><span class="item__icon">${ico(p.kind === 'web' ? 'server' : 'cards')}</span>
        <div class="item__main"><b>${esc(p.name)}</b>${p.pinned ? '<span class="chip chip--new">Pinned</span>' : ''}<span class="chip">${p.kind === 'web' ? `:${esc(p.port ?? '?')}` : 'Desktop'}</span>${p.show_projects ? '' : '<span class="chip chip--warn">Not in Projects</span>'}${p.show_launchpad ? '<span class="chip chip--vinyl">Launchpad</span>' : ''}${p.lan_visible ? '' : '<span class="chip chip--warn">This PC only</span>'}${p.enabled ? '' : '<span class="chip">Hidden</span>'}${p.cwd_exists ? '' : '<span class="chip chip--warn">Folder missing</span>'}
          <div class="item__sub">${esc(p.command)}</div></div>
        <div class="item__acts">
          ${act('pin-project', ico('pin'), `data-id="${esc(p.slug)}" data-to="${!p.pinned}" aria-label="${p.pinned ? 'Unpin' : 'Pin to top'}" title="${p.pinned ? 'Unpin' : 'Pin to top'}"`, `btn--icon${p.pinned ? ' is-pinned' : ''}`)}
          ${act('move-project', ico('up'), `data-i="${i}" data-d="-1" aria-label="Move up" ${sameGroup(cfg.projects, i, -1) ? '' : 'disabled'}`, 'btn--icon')}
          ${act('move-project', ico('down'), `data-i="${i}" data-d="1" aria-label="Move down" ${sameGroup(cfg.projects, i, 1) ? '' : 'disabled'}`, 'btn--icon')}
          ${act('edit-project', 'Edit', `data-id="${esc(p.slug)}"`)}${act('delete-project', 'Delete', `data-id="${esc(p.slug)}" data-name="${esc(p.name)}"`, 'btn--danger')}</div></li>`).join('');
  const adding = editing?.kind === 'project' && editing.id === 'new' ? `<li class="item item--edit">${projectForm()}</li>` : '';
  return card('Projects', 'Things the dashboard can start and stop. Each runs as its own supervised systemd unit, so it keeps running if the dashboard restarts. Only owner devices can start, stop or edit them.',
    `<div class="bar-actions">${act('new-project', `${ico('play')}Add project`, '', 'btn--go')}</div><ul class="items">${adding}${rows}</ul>`);
}

// ---- Widgets ----------------------------------------------------------------------------------------------------
function statusLine(w, id = '') {
  if (!w) return '';
  if (w.error) return `<div class="status-line status-line--err"${id ? ` data-status="${id}"` : ''}><span class="dot dot--bad"></span>Last refresh failed: ${esc(w.error)}${w.fetched_at ? ` — showing data from ${esc(hhmm(w.fetched_at))}` : ''}</div>`;
  if (w.fetched_at) return `<div class="status-line"${id ? ` data-status="${id}"` : ''}><span class="dot dot--ok"></span>Synced ${esc(hhmm(w.fetched_at))}</div>`;
  return `<div class="status-line"${id ? ` data-status="${id}"` : ''}><span class="dot"></span>${w.configured ? 'Waiting for the first sync…' : 'Not set up yet'}</div>`;
}

function binsCard() {
  const s = cfg.widgets.bins.settings;
  const hours = Array.from({ length: 24 }, (_, h) => [h, `${String(h).padStart(2, '0')}:00`]);
  return card('Bins — Salford City Council',
    'Reads your collection days from the council website. Your <b>property reference (UPRN)</b> identifies the address — find it at <a href="https://www.findmyaddress.co.uk/search" target="_blank" rel="noopener">findmyaddress.co.uk</a>.',
    `${statusLine(live?.bins, 'bins')}<form class="form" data-form="bins" style="padding:0 18px 12px">
      <div class="form-grid">
        ${field('Property reference (UPRN)', input('uprn', s.uprn, 'inputmode="numeric" pattern="\\d{5,14}" placeholder="e.g. 123456789012"'))}
        ${field('“Put the bin out” reminder from', select('put_out_hour', hours, s.put_out_hour), 'The evening before collection, the alert strip starts showing from this time.')}
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
    'Shows upcoming events. In Google Calendar: <i>Settings → your calendar → Integrate calendar → “Secret address in iCal format”</i>. That address is read-only, but anyone holding it can read the calendar, so it’s stored privately and never shown again. Google can be a few hours slow to refresh these feeds.',
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
  return card('Booked gigs', 'Tickets you have. The <b>Next gig</b> card counts down to the soonest, and you get a reminder in the alert strip the day before.',
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
  return card('Artists you follow', `Releases, vinyl and gigs are found for these artists. <b>Pin</b> to check them first; <b>Mute</b> to hide everything about them. ${list.length} followed, ${muted} muted.`,
    `<form class="bar-actions" data-form="artist-add" style="flex-wrap:wrap">
       <input class="input" name="name" required maxlength="80" placeholder="Follow an artist…" style="max-width:260px"><button class="btn btn--go" type="submit">Follow</button>
       ${act('resync', 'Resync from Music Companion', extras.artists.music_db ? '' : 'disabled title="Music Companion\'s database wasn\'t found"')}
       <input class="input" data-artist-filter placeholder="Filter this list…" style="max-width:220px;margin-left:auto" aria-label="Filter artists"></form>
     <ul class="items items--scroll">${rows || '<li class="item"><span class="field__h">No artists yet — follow one above, or resync from Music Companion.</span></li>'}</ul>`);
}

const typeChecks = (types, selected) => types.map(t => check(`type_${t}`, t, selected.includes(t))).join('');

function releasesCard() {
  const s = cfg.widgets.releases.settings;
  return card('Releases — new albums', 'New studio albums from the artists you follow, from MusicBrainz (no key needed). Singles, live albums, compilations and soundtracks are left out unless you switch them on. MusicBrainz is polite-rate-limited, so a first full pass takes a few minutes.',
    `${statusLine(live?.releases, 'releases')}<form class="form" data-form="releases" style="padding:0 18px 12px"><div class="form-grid">
      ${field('Look back (days)', input('lookback_days', s.lookback_days, 'type="number" min="14" max="730"'), 'How far back to keep recent albums.')}
      ${field('Look ahead (days)', input('horizon_days', s.horizon_days, 'type="number" min="30" max="730"'), 'How far into the future to look for announced albums. MusicBrainz only knows about albums once they are announced.')}
      ${field('Artists per sync', input('batch', s.batch, 'type="number" min="5" max="80"'), 'Checked oldest-first, so everyone is covered over a day.')}
      </div><div class="checks"><span class="field__l">Include</span>${typeChecks(['Album', 'EP', 'Single'], s.types)}
        ${check('include_extras', 'Live albums, compilations and soundtracks', s.include_extras, 'Only applies to the types ticked above.')}</div>
      <div class="form-actions">${act('refresh-widget', 'Sync now', 'data-id="releases"')}<button class="btn btn--solid" type="submit">Save</button></div></form>`);
}

const shopRow = (x = {}) => `<div class="feedcfg shopcfg" data-shop data-id="${esc(x.id || '')}" data-kind="${esc(x.kind || 'shopify')}">
  ${input('s_name', x.name || '', 'placeholder="Name" maxlength="40" aria-label="Store name"')}${input('s_url', x.url || '', 'type="url" placeholder="https://store.example" aria-label="Store address"')}
  ${input('s_coll', x.collection || '', `placeholder="${x.kind === 'juno' ? 'metal,rock,punk-hardcore' : 'pre-orders'}" maxlength="120" aria-label="${x.kind === 'juno' ? 'Juno sections' : 'Pre-order collection'}"`)}
  ${check('s_on', 'On', x.enabled !== false)}${act('check-shop', 'Check')}${act('remove-shop', 'Remove', '', 'btn--danger')}</div>`;

function vinylCard() {
  const s = cfg.widgets.vinyl.settings;
  return card('Vinyl radar — pre-orders', 'Records you can <b>pre-order</b> by artists you follow, from label and record stores. Any <b>Shopify</b> store works: enter its address and the <b>collection</b> that lists its pre-orders (the last part of a link like <code>store.example/collections/<b>pre-orders</b></code>). Use <b>Check</b> to see what a store offers before you rely on it. <b>Juno</b> is read from its pre-order pages by genre (<code>metal</code>, <code>rock</code>, <code>punk-hardcore</code>…) — slowly and politely, as its robots.txt asks, at most once a day. Release dates appear where a store gives one; otherwise you’ll see when it was listed. (Hive sits behind a bot-check, so it can’t be read.)',
    `${statusLine(live?.vinyl, 'vinyl')}<form class="form" data-form="vinyl" style="padding:0 18px 12px">
      <div id="shops"><div class="field__l" style="display:grid;grid-template-columns:minmax(110px,1fr) minmax(200px,2fr) minmax(110px,1fr);gap:10px;padding:0 0 6px"><span>Store</span><span>Address</span><span>Pre-order collection / Juno sections</span></div>${s.shops.map(shopRow).join('')}</div>
      <div class="bar-actions" style="padding:10px 0 4px">${act('add-shop', `${ico('play')}Add store`, '', 'btn--go')}</div>
      <div class="form-actions">${act('refresh-widget', 'Sync now', 'data-id="vinyl"')}<button class="btn btn--solid" type="submit">Save</button></div></form>`);
}

function purchasesCard() {
  return card('Vinyl pre-orders — add a purchase', 'Bought a record somewhere this app doesn’t watch — a shop, a gig, secondhand? Add it by hand; it appears in <b>Confirmed Pre-orders</b> on the Vinyl radar tile alongside anything bought straight from a tracked store. Editing the date later (e.g. once a TBC release is announced) or removing it once it arrives is done from that tile, not here.',
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
  return card('Gig radar — Ticketmaster', 'Upcoming shows near you for artists you follow. Needs a free API key from <a href="https://developer.ticketmaster.com/" target="_blank" rel="noopener">developer.ticketmaster.com</a> (5,000 calls a day). Only events genuinely by the artist are kept, and VIP / premium / hospitality packages are left out (merch packages stay).',
    `${statusLine(live?.radar, 'radar')}<form class="form" data-form="radar" style="padding:0 18px 12px"><div class="form-grid">
      ${field('API key', input('api_key', '', `type="password" autocomplete="off" spellcheck="false" placeholder="${s.has_api_key ? '•••• saved — leave blank to keep' : 'paste your Consumer Key'}"`), '', 'wide')}
      ${field('Your location (latitude, longitude)', input('latlong', s.latlong, 'placeholder="53.4808,-2.2426"'), 'Manchester by default. In Google Maps, right-click your town and click the coordinates to copy them.')}
      ${field('Radius (miles)', input('radius_miles', s.radius_miles, 'type="number" min="5" max="500"'))}
      ${field('Artists per sync', input('batch', s.batch, 'type="number" min="5" max="300"'))}
      </div><div class="form-actions">${act('refresh-widget', 'Sync now', 'data-id="radar"')}<button class="btn btn--solid" type="submit">Save</button></div></form>`);
}

const feedRow = (f = {}) => {
  const custom = (f.include?.length || f.exclude?.length);
  return `<div class="feedcfg" data-feed data-id="${esc(f.id || '')}">
  ${input('f_name', f.name || '', 'placeholder="Name" maxlength="40" aria-label="Feed name"')}${input('f_url', f.url || '', 'type="url" placeholder="https://…/feed" aria-label="Feed address"')}
  ${check('f_on', 'On', f.enabled !== false)}${act('remove-feed', 'Remove', '', 'btn--danger')}
  <details class="feedcfg__filter"${custom ? ' open' : ''}><summary>Filtering${custom ? ' <span class="chip chip--new">custom</span>' : ''}</summary>
    <div class="form-grid">
      ${field('Only show (overrides the global list, this feed only)', `<textarea class="input" name="f_include" rows="3" spellcheck="false" placeholder="Leave empty to use the global list">${esc((f.include || []).join('\n'))}</textarea>`)}
      ${field('Hide (overrides the global list, this feed only)', `<textarea class="input" name="f_exclude" rows="3" spellcheck="false" placeholder="Leave empty to use the global list">${esc((f.exclude || []).join('\n'))}</textarea>`)}
    </div></details></div>`;
};

function newsSettingsCard() {
  const s = cfg.widgets.news.settings;
  const feedErrors = live?.news?.data?.errors || [];
  const errNote = feedErrors.length ? `<div class="status-line status-line--err">⚠ Some feeds couldn't be read last sync: ${esc(feedErrors.join(', '))}</div>` : '';
  return card('Headlines — RSS feeds', 'Any RSS or Atom feed. The defaults lean metal and vinyl. <b>Only show</b> keeps just stories mentioning one of those words; <b>Hide</b> drops stories that mention any. Case doesn’t matter. Each feed can also set its own under <b>Filtering</b>, which replaces (not adds to) the lists below for that feed only.',
    `${statusLine(live?.news, 'news')}${errNote}<form class="form" data-form="news" style="padding:0 18px 12px">
      <div id="feeds">${s.feeds.map(feedRow).join('')}</div>
      <div class="bar-actions" style="padding:10px 0 4px">${act('add-feed', `${ico('play')}Add feed`, '', 'btn--go')}</div>
      <div class="form-grid" style="margin-top:8px">
        ${field('Only show stories mentioning (one per line)', `<textarea class="input" name="include" rows="4" spellcheck="false" placeholder="e.g. vinyl&#10;tour&#10;reissue">${esc(s.include.join('\n'))}</textarea>`, 'Leave empty to show everything. The default for feeds that don\'t set their own.')}
        ${field('Hide stories mentioning (one per line)', `<textarea class="input" name="exclude" rows="4" spellcheck="false" placeholder="e.g. kardashian">${esc(s.exclude.join('\n'))}</textarea>`)}
        ${field('Keep stories for (days)', input('max_age_days', s.max_age_days, 'type="number" min="3" max="180"'))}
      </div><div class="form-actions">${act('refresh-widget', 'Sync now', 'data-id="news"')}<button class="btn btn--solid" type="submit">Save</button></div></form>`);
}

// ---- Screens & devices ------------------------------------------------------------------------------------------
const PANEL_LABELS = { bins: ['Bins', 'The next two collections.'], gigs: ['Next gig', 'Your next booked gig, with a countdown.'], diary: ['Diary', 'Upcoming events — private: anyone who can see the TV will see them.'] };

function screensCard() {
  const s = cfg.screens;
  return card('TV / screens', 'Choose what a paired screen may show. Panels rotate; the calendar is off by default because guests can see a TV.',
    `<form class="form" data-form="screens" style="padding:0 18px 12px">
      <div class="checks"><span class="field__l">Panels</span>${cfg.tv_panels.map(k => check(`panel_${k}`, PANEL_LABELS[k][0], s.panels.includes(k), PANEL_LABELS[k][1])).join('')}
        ${check('show_alerts', 'Show reminders banner (bin night, next event)', s.show_alerts)}</div>
      <div class="form-grid" style="margin-top:14px">${field('Seconds per panel', input('rotate_seconds', s.rotate_seconds, 'type="number" min="5" max="300"'))}</div>
      <div class="form-actions"><a class="btn" href="/tv" target="_blank" rel="noopener">Preview</a><button class="btn btn--solid" type="submit">Save</button></div></form>`);
}

const pairCard = () => card('Pair a screen',
  'On the TV’s browser open <code>' + esc(cfg.public_url) + '/tv</code>. It shows a short code — type it here to approve that screen. Screens can only <i>view</i> what you allow above.',
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
  return card('Devices', 'Everything that has been let in. Revoking a device signs it out immediately.', `<ul class="items">${rows}</ul>`);
}

function enrolCard() {
  const result = enrol ? `<div class="enrol"><div class="enrol__qr">${enrol.qr_svg}</div><div>
      <p class="sx__intro" style="padding:0 0 10px">Open this on the new device (or scan the code). It works <b>once</b>, for 15 minutes.</p>
      <input class="input" readonly value="${esc(enrol.url)}" data-select-all aria-label="Enrolment link"></div></div>` : '';
  return card('Add an owner device', 'Owner devices get everything: the dashboard, start/stop, and these settings. Anyone else on your network sees only the guest page.',
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
    <div class="item__sub sched__msg" title="${esc(j.message)}">${esc(j.message || (j.status === 'not set up' ? 'Add its key or address on its settings page' : ''))}</div>
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
  return card('Schedule', 'Everything that updates by itself. The data syncs run <b>once a day, overnight, one after another</b>, so the services behind them (MusicBrainz, Ticketmaster, the record stores, the council) are never hammered — each artist is only looked up about once a day however often the service restarts. A job that missed its slot (the PC was off) runs when it can. <i>Run now</i> works any time; after a failure a job retries after 5 min, then 15, 30, 60 and at most every 2 hours.',
    `${banner}<div class="sched__head"><span>Job</span><span>Schedule</span><span>Status</span><span></span></div>
     <ul class="items sched__list">${sync.map(jobRow).join('')}</ul>
     <div class="sx__sub hud">Housekeeping</div><ul class="items sched__list">${house.map(jobRow).join('')}</ul>
     <p class="sx__intro">Nightly backups (the last 7) are kept in <code>${esc(cfg.paths.database.replace(/[^/]+$/, ''))}backups/</code> — ${maint.backups.length ? esc(maint.backups.join(', ')) : 'none yet'}. To restore one: stop the service, copy it over <code>hub.sqlite</code>, start the service.</p>`,
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
  return card('Event log', `What the dashboard did and what went wrong: failed syncs, a store or API that changed, unexpected errors. Kept for 30 days. Secrets (API keys, private calendar addresses) are stripped before anything is written. Last 24 hours: <b>${c.error || 0}</b> errors, <b>${c.warning || 0}</b> warnings.`,
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

// ---- About -----------------------------------------------------------------------------------------------------
function aboutTab() {
  const rows = [['Version', cfg.version], ['Host', `${cfg.host} · ${cfg.ip}`], ['Address', cfg.public_url], ['Database', cfg.paths.database], ['Projects folder', cfg.paths.projects],
    ['Links / projects / devices', `${cfg.links.length} / ${cfg.projects.length} / ${cfg.devices.length}`]];
  return card('About', 'Copy <code>data/hub.sqlite</code> (and <code>.env</code>) to move Citadel to another machine — see the README.',
    `<ul class="items">${rows.map(([k, v]) => `<li class="item" style="grid-template-columns:200px 1fr"><span class="field__l">${esc(k)}</span><span class="mono" style="word-break:break-all">${esc(v)}</span></li>`).join('')}</ul>`);
}

// ---- rendering ---------------------------------------------------------------------------------------------------
function render() {
  $('#tabs').innerHTML = TABS.map(([id, label, icon]) => `<button role="tab" data-tab="${id}" aria-selected="${id === tab}"${id === 'maintenance' && needsAttention() ? ' class="has-attn"' : ''}>${ico(icon)}${esc(label)}</button>`).join('');
  panel.innerHTML = { links: linksTab, projects: projectsTab, widgets: () => binsCard() + diaryCard(), music: () => gigsCard() + artistsCard() + releasesCard() + vinylCard() + purchasesCard() + radarCard(), news: newsSettingsCard, screens: () => screensCard() + pairCard() + devicesCard() + enrolCard(), maintenance: maintenanceTab, about: aboutTab }[tab]();
}

async function load() {
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
  const mode = e.target.closest('[data-sched-mode]');
  if (mode) {                                                                    // show the time box for "Daily at", the number box for "Every"
    const f = mode.form, every = mode.value === 'every';
    f.elements.at.hidden = every; f.querySelector('.sched__every').hidden = !every;
    return;
  }
  const src = e.target.closest('[data-log-source]');
  if (src) { logf.source = src.value; run(async () => {}); return; }
  const sel = e.target.closest('[data-artist-status]');
  if (sel) run(() => api.patch(`/api/owner/artists/${sel.dataset.id}`, { status: sel.value }), 'Updated');
});
let logTimer;
panel.addEventListener('input', e => {
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
    case 'new-link': editing = { kind: 'link', id: 'new' }; render(); $('[data-form="link"] input')?.focus(); break;
    case 'edit-link': editing = { kind: 'link', id: Number(d.id) }; render(); break;
    case 'new-project': editing = { kind: 'project', id: 'new' }; render(); $('[data-form="project"] input')?.focus(); break;
    case 'edit-project': editing = { kind: 'project', id: d.id }; render(); break;
    case 'pin-link': run(() => api.post(`/api/owner/links/${d.id}/pin`, { pinned: d.to === 'true' }), d.to === 'true' ? 'Pinned' : 'Unpinned'); break;
    case 'pin-project': run(() => api.post(`/api/owner/projects/${d.id}/pin`, { pinned: d.to === 'true' }), d.to === 'true' ? 'Pinned' : 'Unpinned'); break;
    case 'new-gig': editing = { kind: 'gig', id: 'new' }; render(); $('[data-form="gig"] input')?.focus(); break;
    case 'edit-gig': editing = { kind: 'gig', id: Number(d.id) }; render(); break;
    case 'delete-gig': if (confirm(`Delete the ${d.name} gig?`)) run(() => api.del(`/api/owner/gigs/${d.id}`), 'Gig deleted'); break;
    case 'remove-artist': if (confirm(`Stop following ${d.name}? Their found items will disappear.`)) run(() => api.del(`/api/owner/artists/${d.id}`), 'Removed'); break;
    case 'resync': run(async () => { const r = await api.post('/api/owner/artists/resync'); toast(`Music Companion: ${r.added} new artist${r.added === 1 ? '' : 's'} followed, ${r.updated} refreshed`); }); break;
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
    case 'add-feed': $('#feeds').insertAdjacentHTML('beforeend', feedRow()); $('#feeds [data-feed]:last-child input')?.focus(); break;
    case 'remove-feed': b.closest('[data-feed]').remove(); break;
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
      is_primary: on(f, 'is_primary'), show_guest: on(f, 'show_guest'), tier: on(f, 'more') ? 'more' : 'main', pinned: on(f, 'pinned'), for_media: on(f, 'for_media'), for_music: on(f, 'for_music'), for_sql: on(f, 'for_sql') };
    const id = f.dataset.id;
    run(async () => { await (id ? api.put(`/api/owner/links/${id}`, body) : api.post('/api/owner/links', body)); editing = null; }, 'Link saved');
  } else if (kind === 'project') {
    let env;
    try { env = parseEnv(val(f, 'env')); } catch (err) { toast(err.message); return; }
    const body = { name: val(f, 'name'), description: val(f, 'description'), kind: val(f, 'kind'), url: val(f, 'url') || null,
      port: val(f, 'port') ? Number(val(f, 'port')) : null, cwd: val(f, 'cwd'), command: val(f, 'command'), env, lan_visible: !on(f, 'local_only'), enabled: on(f, 'enabled'), pinned: on(f, 'pinned'),
      show_projects: on(f, 'show_projects'), show_launchpad: on(f, 'show_launchpad'), icon: val(f, 'icon') };
    const slug = f.dataset.slug;
    run(async () => {
      const res = await (slug ? api.put(`/api/owner/projects/${slug}`, body) : api.post('/api/owner/projects', { ...body, slug: val(f, 'slug') }));
      editing = null;
      if (res.warnings?.length) setTimeout(() => toast(res.warnings.join(' ')), 300);
    }, 'Project saved');
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
  } else if (kind === 'news') {
    const lines = name => val(f, name).split('\n').map(l => l.trim()).filter(Boolean);
    const rowLines = (r, name) => (r.querySelector(`[name=${name}]`)?.value || '').split('\n').map(l => l.trim()).filter(Boolean);
    const feeds = $$('[data-feed]', f).map(r => {
      const feed = { id: r.dataset.id || undefined, name: r.querySelector('[name=f_name]').value.trim(), url: r.querySelector('[name=f_url]').value.trim(), enabled: r.querySelector('[name=f_on]').checked };
      const inc = rowLines(r, 'f_include'), exc = rowLines(r, 'f_exclude');
      if (inc.length) feed.include = inc;
      if (exc.length) feed.exclude = exc;
      return feed;
    }).filter(x => x.name || x.url);
    run(() => api.put('/api/owner/widgets/news/settings', { feeds, include: lines('include'), exclude: lines('exclude'), max_age_days: Number(val(f, 'max_age_days')) }), 'Saved — syncing');
  } else if (kind === 'schedule') {
    const every = val(f, 'mode') === 'every';
    const body = every ? { mode: 'every', seconds: Math.round(Number(val(f, 'amount')) * Number(val(f, 'unit'))) } : { mode: 'daily', at: val(f, 'at') };
    run(() => api.put(`/api/owner/maintenance/${f.dataset.id}/schedule`, body), 'Schedule saved');
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
      $$('[data-status]').forEach(el => { const w = live[el.dataset.status]; if (w) el.outerHTML = statusLine(w, el.dataset.status); });
      if (tab === 'maintenance') await paintMaint();
    } catch { /* transient */ }
  }, 500);
}, () => {});

await load();
if (addParam === 'gig') $('[data-form="gig"] input')?.focus();
