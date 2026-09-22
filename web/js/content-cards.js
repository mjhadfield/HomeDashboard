// The five content cards: Next gig, Releases, Vinyl radar, Gig radar, Headlines.
// Every row can be pinned, dismissed, or its artist muted; the expanded view adds filters, search and a
// "Dismissed" list you can restore from. Everything from the network passes through esc()/safeUrl().
import { esc, ico, safeUrl, fmtDay, fmtDMY, relDays, hhmm, parseDate } from './util.js';

const COVERS = ['', 'cover--b', 'cover--c', 'cover--d'];
const initial = s => (String(s || '?').trim()[0] || '?').toUpperCase();
const COMPACT = 4;                                        // rows in a small card by default; the rest are expanded-only
const LONG = 10;                                          // the music cards show more: their body scrolls

const empty = (icon, title, text, extra = '') =>
  `<div class="empty">${ico(icon)}<div class="empty__title">${esc(title)}</div><p>${esc(text)}</p>${extra}</div>`;
const settingsLink = label => `<a class="btn btn--go" href="/settings#music">${esc(label)}</a>`;
const staleChip = w => (w.error ? ' <span class="chip chip--warn">stale</span>' : '');
const newChip = n => (n ? ` <span class="chip chip--new">${n} new</span>` : '');

/** Dates arrive as YYYY-MM-DD, or YYYY-MM / YYYY when a source only knows that much. */
function niceDate(d) {
  if (!d) return '';
  if (/^\d{4}-\d{2}-\d{2}$/.test(d)) return +d.slice(0, 4) === new Date().getFullYear() ? fmtDay(d) : `${fmtDay(d)} ${d.slice(0, 4)}`;   // the year only when it isn't this one
  if (/^\d{4}-\d{2}$/.test(d)) return parseDate(`${d}-01`).toLocaleDateString('en-GB', { month: 'short', year: 'numeric' });
  return d;
}

/** "today", "yesterday", "3d" — headlines only carry a day. */
function dayAge(date, todayIso) {
  const n = Math.round((parseDate(todayIso) - parseDate(date)) / 86400000);
  return n <= 0 ? 'today' : n === 1 ? 'yesterday' : `${n}d`;
}

// ---- row actions ------------------------------------------------------------------------------------------------
function acts(it) {
  if (it.status === 'dismissed') {
    return `<div class="feed__acts feed__acts--wide"><button class="btn" data-item-act="restore" data-id="${it.id}" data-to="seen">Restore</button></div>`;
  }
  const pinned = it.status === 'pinned';
  // Order: seen (only while it's new) · dismiss · mute · pin. The pin sits furthest right and, once pinned, stays lit
  // even when the row isn't hovered.
  const seen = it.status === 'new' ? `<button class="btn btn--icon" data-item-act="seen" data-id="${it.id}" data-to="seen" aria-label="Mark as seen" title="Mark as seen — clears the New highlight">${ico('check')}</button>` : '';
  return `<div class="feed__acts">${seen}
    <button class="btn btn--icon" data-item-act="dismiss" data-id="${it.id}" data-to="dismissed" aria-label="Dismiss" title="Dismiss">${ico('x')}</button>
    ${it.artist ? `<button class="btn btn--icon" data-item-act="mute" data-artist="${esc(it.artist)}" aria-label="Mute ${esc(it.artist)}" title="Mute ${esc(it.artist)}">${ico('mute')}</button>` : ''}
    <button class="btn btn--icon${pinned ? ' is-pinned' : ''}" data-item-act="pin" data-id="${it.id}" data-to="${pinned ? 'seen' : 'pinned'}" aria-label="${pinned ? 'Unpin' : 'Pin'}" aria-pressed="${pinned}" title="${pinned ? 'Unpin' : 'Pin to top'}">${ico('pin')}</button></div>`;
}

const link = (it, text, attrs = '') => `<a class="feed__link" href="${esc(safeUrl(it.url))}" target="_blank" rel="noopener"${attrs}>${esc(text)}</a>`;

function musicRow(it, k, hide) {
  const more = hide;
  const art = thumb(it.meta?.image);
  return `<li class="feed${it.status === 'new' ? ' feed--new' : ''}${it.status === 'pinned' ? ' feed--pinned' : ''}${more ? ' more' : ''}"${more ? ' hidden' : ''}>
    <span class="cover cover--art ${COVERS[k % 4]}">${esc(initial(it.artist || it.title))}${art ? `<img class="cover__img" src="${esc(art)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer">` : ''}</span>
    <div class="feed__main"><div class="feed__title">${link(it, `${it.artist ? `${it.artist} — ` : ''}${it.title}`)}</div><div class="feed__sub">${esc(it.subtitle)}</div></div>
    <div class="feed__meta">${it.status === 'new' ? '<span class="chip chip--fresh">New</span>' : ''}<div class="feed__date">${esc(niceDate(it.date))}</div>${acts(it)}</div></li>`;
}

// Artwork only ever comes from the image hosts the page's CSP allows; Shopify's CDN can resize, so ask for a thumbnail.
const ART_HOSTS = /^https:\/\/(?:cdn\.shopify\.com|imagescdn\.juno\.co\.uk|coverartarchive\.org)\//;
function thumb(url) {
  if (!ART_HOSTS.test(url || '')) return '';
  try { const u = new URL(url); if (u.hostname === 'cdn.shopify.com') u.searchParams.set('width', '192'); return u.href; } catch { return ''; }
}
// A picture that fails to load (blocked, moved, offline) is removed so the lettered tile underneath shows instead.
document.addEventListener('error', e => { if (e.target instanceof HTMLImageElement && e.target.classList.contains('cover__img')) e.target.remove(); }, true);

/** Vinyl tile: artist and album each get their own (wrapping) line so long names are never cut short; the facts sit beneath and the
 *  date is small, right-aligned and DD/MM/YYYY. The expanded view also lists every edition with its price. */
function vinylRow(it, k, hide) {
  const m = it.meta || {};
  const art = thumb(m.image);
  const [when, day] = it.date ? ['releases', fmtDMY(it.date)] : m.listed ? ['listed', fmtDMY(m.listed)] : ['date', 'TBC'];
  const many = m.variants > 1;
  const facts = [m.format, many ? `${m.variants} variants` : '', m.price ? `${many ? 'from ' : ''}${m.price}` : '', m.shop].filter(Boolean).map(esc).join(' · ');
  const also = (m.also || []).length ? `<div class="vin__label">Also at ${m.also.map(esc).join(', ')}</div>` : '';
  const eds = (m.editions || []).length
    ? `<div class="vin__eds more" hidden>${m.editions.map(e => `<a class="vin__ed" href="${esc(safeUrl(e.url))}" target="_blank" rel="noopener"><span>${esc(e.name)}</span><b>${esc(e.price)}</b></a>`).join('')}${m.label ? `<div class="vin__label">Label · ${esc(m.label)}</div>` : ''}${also}</div>`
    : (m.label || also ? `<div class="vin__eds more" hidden>${m.label ? `<div class="vin__label">Label · ${esc(m.label)}</div>` : ''}${also}</div>` : '');
  return `<li class="feed feed--vinyl${it.status === 'new' ? ' feed--new' : ''}${it.status === 'pinned' ? ' feed--pinned' : ''}${hide ? ' more' : ''}"${hide ? ' hidden' : ''}>
    <span class="cover cover--art ${COVERS[k % 4]}">${esc(initial(it.artist || it.title))}${art ? `<img class="cover__img" src="${esc(art)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer">` : ''}</span>
    <div class="feed__main">
      <div class="vin__artist" title="${esc(it.artist || '')}">${link(it, it.artist || it.title)}</div>
      <div class="vin__album" title="${esc(it.title)}">${link(it, it.title)}</div>
      <div class="vin__facts">${facts}</div>${eds}</div>
    <div class="feed__meta">${it.status === 'new' ? '<span class="chip chip--fresh">New</span>' : ''}
      <div class="vin__when"><small>${when}</small>${esc(day)}</div>${acts(it)}</div></li>`;
}

function plainRow(it, k, hide) {                              // no cover: gig radar
  const t = it.meta?.time ? ` · ${it.meta.time}` : '';
  const tribute = it.meta?.tribute
    ? '<div class="feed__flag"><span class="chip chip--warn" title="Ticketmaster lists this as a tribute/cover act, or its name says so">Tribute act</span></div>' : '';
  return `<li class="feed${it.status === 'new' ? ' feed--new' : ''}${it.status === 'pinned' ? ' feed--pinned' : ''}${hide ? ' more' : ''}"${hide ? ' hidden' : ''}>
    <div class="feed__main"><div class="feed__title">${link(it, it.artist || it.title)}</div><div class="feed__sub">${esc(it.subtitle)}</div>${tribute}</div>
    <div class="feed__meta">${it.status === 'new' ? '<span class="chip chip--fresh">New</span>' : ''}<div class="feed__date">${esc(niceDate(it.date))}${esc(t)}</div>${acts(it)}</div></li>`;
}

// Clicking an unread headline both opens it (default <a> navigation, target="_blank") and marks it *read* —
// a separate concept from status="seen" (which "Mark as seen"/"Mark all seen" already use for "acknowledged,
// not necessarily opened"). data-item-read fires the dedicated /read endpoint, independent of status, so bulk
// "mark as seen" never counts as having actually read something, and vice versa.
function newsRow(it, k, hide, todayIso) {
  const unread = !it.meta?.read;
  const isNew = it.status === 'new' && unread;                    // "New" excludes anything already opened, even if status hasn't moved on
  const readAttr = unread ? ` data-item-read="${it.id}"` : '';
  return `<li class="feed feed--news${isNew ? ' feed--new' : ''}${it.status === 'pinned' ? ' feed--pinned' : ''}${hide ? ' more' : ''}"${hide ? ' hidden' : ''}>
    <div class="feed__main"><div class="news__src">${esc(it.subtitle)}<span class="news__age">${esc(dayAge(it.date, todayIso))}</span></div>
      <div class="feed__title feed__title--wrap">${link(it, it.title, readAttr)}</div></div>
    <div class="feed__meta">${isNew ? '<span class="chip chip--fresh">New</span>' : ''}${acts(it)}</div></li>`;
}

// ---- filtering (expanded view) --------------------------------------------------------------------------------------
function narrow(data, ctx, todayIso, extra = {}) {
  let list = data.items;
  const f = ctx.filter;
  if (f === 'new') list = list.filter(i => i.status === 'new');
  else if (f === 'pinned') list = list.filter(i => i.status === 'pinned');
  else if (f === 'dismissed') list = data.dismissed;
  else if (f === 'upcoming') list = list.filter(i => (i.date || '') >= todayIso);
  else if (extra[f]) list = list.filter(extra[f]);
  if (ctx.search) list = list.filter(i => `${i.artist || ''} ${i.title} ${i.subtitle} ${(i.meta?.artists || []).join(' ')}`.toLowerCase().includes(ctx.search));
  return list;
}

/** Shared skeleton: handles the empty / syncing / error / needs-artists states, then the rows. */
function feedCard(s, ctx, { w, kind, render, extra, emptyIcon, emptyTitle, emptyText, unit, metaExtra = '', hint = '', compact = COMPACT }) {
  const today = s.now.slice(0, 10);
  const d = w.data;
  if (!w.configured) return null;                                   // caller handles "needs a key"
  const meta = (d ? `${d.total} ${unit}` : '') + metaExtra + (d ? newChip(d.new) : '') + staleChip(w);
  if (!d || (w.error && !d.total && !d.dismissed?.length && w.fetched_at == null)) {
    return { meta: w.error ? 'error' : 'syncing…', body: empty(emptyIcon, w.error ? `Couldn't sync ${emptyTitle.toLowerCase()}` : 'Syncing…', w.error || 'Gathering the first results — this takes a few minutes.') };
  }
  const list = ctx.expanded ? narrow(d, ctx, today, extra) : d.items;
  if (!list.length) {
    if (ctx.expanded && (ctx.filter !== 'all' || ctx.search)) return { meta, body: empty(emptyIcon, 'Nothing matches', 'Try a different filter.') };
    if (d.following === 0 && kind !== 'news') return { meta, body: empty(emptyIcon, 'Follow some artists', 'Choose the artists to watch in Settings → Music.', settingsLink('Choose artists')) };
    return { meta, body: empty(emptyIcon, w.fetched_at == null ? 'Syncing…' : emptyTitle, w.fetched_at == null ? 'Gathering the first results — this takes a few minutes.' : emptyText) };
  }
  const rows = list.map((it, k) => render(it, k, k >= compact, today)).join('');
  return { meta, body: `<ul class="rows rows--cols">${rows}</ul>${hint}` };
}

// ---- Releases ----------------------------------------------------------------------------------------------------
function releases(s, ctx) {
  const w = s.widgets.releases;
  return feedCard(s, ctx, { w, kind: 'release', render: (it, k, hide) => musicRow(it, k, hide),
    emptyIcon: 'disc', emptyTitle: 'No new albums yet', emptyText: `Watching ${w.data?.following ?? 0} artists for new studio albums.`, unit: 'albums · musicbrainz', compact: LONG });
}

// ---- Vinyl radar: records you can pre-order --------------------------------------------------------------------------
function vinyl(s, ctx) {
  const w = s.widgets.vinyl, d = w.data;
  const hint = d ? `<div class="note more" hidden>Pre-orders from ${d.stores} label and record stores, matched to the artists you follow. Release dates are shown where a store gives one; otherwise when the pre-order was listed. Manage stores in <a href="/settings#music">Settings → Music</a>.${d.errors?.length ? ` Couldn’t read: ${esc(d.errors.join(', '))}.` : ''}</div>` : '';
  return feedCard(s, ctx, {
    w, kind: 'vinyl', hint, unit: `pre-orders · ${d?.stores ?? 0} stores`, compact: LONG,
    render: vinylRow,
    extra: { dated: i => !!i.date },
    emptyIcon: 'disc', emptyTitle: 'No pre-orders found', emptyText: `Nothing on pre-order for the artists you follow, across ${d?.stores ?? 0} stores.`,
  });
}

// ---- Gig radar ----------------------------------------------------------------------------------------------------
function radar(s, ctx) {
  const w = s.widgets.radar;
  if (!w.configured) {
    return { meta: 'needs a key', body: empty('bolt', 'Gig radar needs a Ticketmaster key', 'A free key finds upcoming shows near you for the artists you follow.', settingsLink('Add key')) };
  }
  return feedCard(s, ctx, { w, kind: 'radar', render: (it, k, hide) => plainRow(it, k, hide),
    extra: { tribute: i => !!i.meta?.tribute },
    emptyIcon: 'bolt', emptyTitle: 'No shows found', emptyText: `Nothing announced within ${w.data?.radius ?? 50} miles for the artists you follow.`,
    unit: `shows nearby · ≤${w.data?.radius ?? 50} mi`, compact: LONG });
}

// ---- Headlines ---------------------------------------------------------------------------------------------------
const VINYL_WORDS = /vinyl|\bLPs?\b|reissue|repress|pressing|variant|colou?red|box ?set/i;

// Source/tag/vinyl filtering are News-only, so they live here as plain browser-side UI state (same pattern as
// cards.js's own `ui`) rather than as Shell filter chips: Shell's chip set is static, built once per card
// definition, and shared with Releases/Vinyl/Radar — source/tag options here depend on live data (which feeds
// are enabled, which artists are actually mentioned today), which that shared mechanism isn't set up for.
export const newsUi = { source: '', tag: '', vinyl: false };

function newsToolbar(d) {
  const sourceCounts = {};
  for (const i of d.items) if (i.status === 'new') sourceCounts[i.meta?.feed] = (sourceCounts[i.meta?.feed] || 0) + 1;
  const sourceOpts = Object.entries(d.sources || {})
    .map(([id, name]) => `<option value="${esc(id)}"${newsUi.source === id ? ' selected' : ''}>${esc(name)}${sourceCounts[id] ? ` (${sourceCounts[id]} new)` : ''}</option>`).join('');
  const pool = newsUi.source ? d.items.filter(i => i.meta?.feed === newsUi.source) : d.items;                   // tag options reflect the chosen source, if any
  const tags = [...new Set(pool.flatMap(i => i.meta?.artists || []))].sort((a, b) => a.localeCompare(b));
  const tagOpts = tags.map(name => `<option value="${esc(name)}"${newsUi.tag === name ? ' selected' : ''}>${esc(name)}</option>`).join('');
  const active = newsUi.source || newsUi.tag || newsUi.vinyl;
  return `<div class="news__toolbar maxbar">
    <select class="news__source" data-news-source aria-label="Filter by source"><option value="">All sources</option>${sourceOpts}</select>
    <select class="news__source" data-news-tag-select aria-label="Filter by band"><option value="">All bands</option>${tagOpts}</select>
    <button class="fchip" data-news-vinyl aria-pressed="${newsUi.vinyl}">Vinyl</button>
    <button class="fchip" data-news-reset${active ? '' : ' disabled'}>Reset filters</button></div>`;
}

function newsCard(s, ctx) {
  const w = s.widgets.news;
  const d = w.data;
  const errs = d?.errors?.length ? `<div class="note more" hidden>Some feeds couldn't be read: ${esc(d.errors.join(', '))}</div>` : '';
  const bySource = i => !newsUi.source || i.meta?.feed === newsUi.source;
  const byTag = i => !newsUi.tag || (i.meta?.artists || []).includes(newsUi.tag);
  const byVinyl = i => !newsUi.vinyl || VINYL_WORDS.test(i.title);
  const filtering = d && (newsUi.source || newsUi.tag || newsUi.vinyl);
  const keep = i => bySource(i) && byTag(i) && byVinyl(i);
  const w2 = filtering ? { ...w, data: { ...d, items: d.items.filter(keep), dismissed: d.dismissed.filter(keep) } } : w;
  const result = feedCard(s, ctx, { w: w2, kind: 'news', hint: errs, render: (it, k, hide, today) => newsRow(it, k, hide, today),
    extra: { unread: i => !i.meta?.read, 'news-new': i => i.status === 'new' && !i.meta?.read },
    emptyIcon: 'news', emptyTitle: 'No headlines', emptyText: 'Add feeds or loosen your keyword rules in Settings → News.',
    unit: `headlines · ${Object.keys(d?.sources || {}).length} feeds` });
  if (result && d && ctx.expanded) result.body = newsToolbar(d) + result.body;
  return result;
}

// ---- Next gig -------------------------------------------------------------------------------------------------------
function gigLine(g, past = false) {
  const where = [g.venue, g.city].filter(Boolean).join(', ');
  const tix = g.ticket_url ? ` <a class="feed__link" href="${esc(safeUrl(g.ticket_url))}" target="_blank" rel="noopener">tickets</a>` : '';
  return `<li class="gigline"><span class="gigline__d mono">${esc(fmtDay(g.date))}</span>
    <div class="gigline__main"><b>${esc(g.artist)}</b><div class="gigline__sub">${esc(where)}${g.doors ? ` · doors ${esc(g.doors)}` : ''}${g.notes ? ` · ${esc(g.notes)}` : ''}${tix}</div></div>
    <span class="chip">${past ? esc(`${-g.in_days}d ago`) : esc(relDays(g.in_days))}</span></li>`;
}

function gigs(s, ctx) {
  const w = s.widgets.gigs, d = w.data;
  if (!d || (!d.next && !d.past.length)) {
    return { meta: 'none booked', body: empty('bolt', 'No gigs booked', 'Add your tickets and get a countdown, plus a reminder the day before.', settingsLink('Add a gig')) };
  }
  const n = d.next;
  const list = ctx.expanded ? { upcoming: d.upcoming, past: d.past, all: [...d.past.slice().reverse(), ...d.upcoming] }[ctx.filter || 'upcoming'] : [];
  const expandedList = ctx.expanded ? `<ul class="gigs more" hidden>${list.map(g => gigLine(g, g.in_days < 0)).join('') || '<li class="note">Nothing here.</li>'}</ul>` : '';
  if (!n) {
    return { meta: 'none upcoming', body: `${empty('bolt', 'No upcoming gigs', 'Nothing booked ahead.', settingsLink('Add a gig'))}${expandedList}` };
  }
  const hero = n.in_days === 0 ? { count: 'GIG', unit: 'tonight' } : { count: String(n.in_days).padStart(2, '0'), unit: n.in_days === 1 ? 'day' : 'days' };
  const where = [n.venue, n.city].filter(Boolean).join(', ');
  const others = d.upcoming.slice(1, 4).map(g => `<div class="subrow">${ico('bolt')}${esc(g.artist)}<span class="mono">${esc(fmtDay(g.date))}</span></div>`).join('');
  return {
    meta: `${d.count} booked`,
    body: `<div class="hero"><span class="hero__count">${hero.count}</span><span class="hero__unit">${hero.unit}</span></div>
      <div class="hero__name">${esc(n.artist)}</div>
      <div class="hero__sub">${esc(where)} · ${esc(fmtDay(n.date))}${n.doors ? ` · doors ${esc(n.doors)}` : ''}</div>
      ${others}${expandedList}`,
  };
}

// ---- registry entries ---------------------------------------------------------------------------------------------------
const CURATE = [{ label: 'All', value: 'all' }, { label: 'New', value: 'new' }, { label: 'Pinned', value: 'pinned' }];
const DISMISSED = { label: 'Dismissed', value: 'dismissed' };

export const contentCards = [
  { id: 'gigs', area: 'gig', label: 'Next gig', render: gigs, headerAction: { icon: 'plus', label: 'Add a gig', href: '/settings?add=gig#music' },
    filters: [{ label: 'Upcoming', value: 'upcoming' }, { label: 'Past', value: 'past' }, { label: 'All', value: 'all' }], defaultFilter: 'upcoming' },
  { id: 'releases', area: 'rel', label: 'Releases', flush: true, render: releases, search: true, markSeen: 'release',
    filters: [...CURATE, { label: 'Upcoming', value: 'upcoming' }, DISMISSED] },
  { id: 'vinyl', area: 'vin', label: 'Vinyl radar', flush: true, render: vinyl, search: true, markSeen: 'vinyl',
    filters: [...CURATE, { label: 'Has date', value: 'dated' }, DISMISSED] },
  { id: 'radar', area: 'radar', label: 'Gig radar', flush: true, render: radar, search: true, markSeen: 'radar', filters: [...CURATE, { label: 'Tributes', value: 'tribute' }, DISMISSED] },
];

// Headlines moved off the dashboard onto its own News page (see web/js/news.js) — kept here since it's still
// just a content card, and the render function is unchanged.
export const newsDef = { id: 'news', area: 'news', label: 'Headlines', flush: true, render: newsCard, search: true, markSeen: 'news',
  filters: [{ label: 'Unread', value: 'unread' }, { label: 'New', value: 'news-new' }, { label: 'Pinned', value: 'pinned' }, DISMISSED, { label: 'All', value: 'all' }] };
