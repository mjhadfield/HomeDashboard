// The content cards: Next gig, Releases, Vinyl radar, Gig radar. (News has its own page: news.js.)
// Every row can be pinned, dismissed, or its artist muted; the expanded view adds filters, search and a
// "Dismissed" list you can restore from. Everything from the network passes through esc()/safeUrl().
import { esc, ico, safeUrl, fmtDay, fmtDMY, relDays, hhmm, parseDate, gcalUrl } from './util.js';

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

// ---- row actions ------------------------------------------------------------------------------------------------
function acts(it, lead = '') {
  if (it.status === 'dismissed') {
    return `<div class="feed__acts feed__acts--wide"><button class="btn" data-item-act="restore" data-id="${it.id}" data-to="seen">Restore</button></div>`;
  }
  const pinned = it.status === 'pinned';
  // Order: seen (only while it's new) · dismiss · mute · pin. The pin sits furthest right and, once pinned, stays lit
  // even when the row isn't hovered.
  const seen = it.status === 'new' ? `<button class="btn btn--icon" data-item-act="seen" data-id="${it.id}" data-to="seen" aria-label="Mark as seen" title="Mark as seen — clears the New highlight">${ico('check')}</button>` : '';
  return `<div class="feed__acts">${lead}${seen}
    <button class="btn btn--icon" data-item-act="dismiss" data-id="${it.id}" data-to="dismissed" aria-label="Dismiss" title="Dismiss">${ico('x')}</button>
    ${it.artist ? `<button class="btn btn--icon" data-item-act="mute" data-artist="${esc(it.artist)}" aria-label="Mute ${esc(it.artist)}" title="Mute ${esc(it.artist)}">${ico('mute')}</button>` : ''}
    <button class="btn btn--icon${pinned ? ' is-pinned' : ''}" data-item-act="pin" data-id="${it.id}" data-to="${pinned ? 'seen' : 'pinned'}" aria-label="${pinned ? 'Unpin' : 'Pin'}" aria-pressed="${pinned}" title="${pinned ? 'Unpin' : 'Pin to top'}">${ico('pin')}</button></div>`;
}

const link = (it, text, attrs = '') => `<a class="feed__link" href="${esc(safeUrl(it.url))}" target="_blank" rel="noopener"${attrs}>${esc(text)}</a>`;
/** "Artist — Title", or just the title for anything without one — used in confirmation prompts, so the dialog names the actual record. */
const rowName = it => esc(it.artist ? `${it.artist} — ${it.title}` : it.title);

function musicRow(it, k, hide) { return musicRowWith(it, k, hide, ''); }
export function musicRowWith(it, k, hide, lead) {
  const more = hide;
  const art = thumb(it.meta?.image);
  return `<li class="feed${it.status === 'new' ? ' feed--new' : ''}${it.status === 'pinned' ? ' feed--pinned' : ''}${more ? ' more' : ''}"${more ? ' hidden' : ''}>
    <span class="cover cover--art ${COVERS[k % 4]}">${esc(initial(it.artist || it.title))}${art ? `<img class="cover__img" src="${esc(art)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer">` : ''}</span>
    <div class="feed__main"><div class="feed__title">${link(it, `${it.artist ? `${it.artist} — ` : ''}${it.title}`)}</div><div class="feed__sub">${esc(it.subtitle)}</div></div>
    <div class="feed__meta">${it.status === 'new' ? '<span class="chip chip--fresh">New</span>' : ''}<div class="feed__date">${esc(niceDate(it.date))}</div>${acts(it, lead)}</div></li>`;
}

// Artwork only ever comes from the image hosts the page's CSP allows; Shopify's CDN can resize, so ask for a thumbnail.
const ART_HOSTS = /^https:\/\/(?:cdn\.shopify\.com|imagescdn\.juno\.co\.uk|coverartarchive\.org)\//;
function thumb(url) {
  if (!ART_HOSTS.test(url || '')) return '';
  try { const u = new URL(url); if (u.hostname === 'cdn.shopify.com') u.searchParams.set('width', '192'); return u.href; } catch { return ''; }
}
// A manually-added purchase's cover is fetched server-side once and served same-origin (see vinyl.fetch_manual_cover)
// rather than hotlinked, so it's a plain relative path — no resize param, no ART_HOSTS check needed.
const cover = url => (url || '').startsWith('/covers/') ? url : thumb(url);
// A picture that fails to load (blocked, moved, offline) is removed so the lettered tile underneath shows instead.
document.addEventListener('error', e => { if (e.target instanceof HTMLImageElement && e.target.classList.contains('cover__img')) e.target.remove(); }, true);

/** Vinyl tile: artist and album each get their own (wrapping) line so long names are never cut short; the facts sit beneath and the
 *  date is small, right-aligned and DD/MM/YYYY. The expanded view also lists every edition with its price. */
function vinylRow(it, k, hide) { return vinylRowWith(it, k, hide, ''); }
export function vinylRowWith(it, k, hide, lead) {
  const m = it.meta || {};
  const art = thumb(m.image);
  const [when, day] = it.date ? ['releases', fmtDMY(it.date)] : m.listed ? ['listed', fmtDMY(m.listed)] : ['date', 'TBC'];
  const many = m.variants > 1;
  const facts = [m.format, many ? `${m.variants} variants` : '', m.price ? `${many ? 'from ' : ''}${m.price}` : '', m.shop].filter(Boolean).map(esc).join(' · ');
  // Each "also at" store gets its own hover tooltip (pressing name + price) and links straight to that store's
  // own product page — not just the primary listing's. CSS-only hover reveal, same technique as .feed__acts.
  const also = (m.also || []).length ? `<div class="vin__label">Also at ${m.also.map(a => `<span class="vin__also"><a class="vin__also-link" href="${esc(safeUrl(a.url))}" target="_blank" rel="noopener">${esc(a.shop)}</a><span class="vin__also-tip" role="tooltip">${esc(a.name)}${a.price ? ` · ${esc(a.price)}` : ''}</span></span>`).join(', ')}</div>` : '';
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
      <div class="vin__when"><small>${when}</small>${esc(day)}</div>${vinylActs(it, lead)}</div></li>`;
}

// Vinyl-only row actions: no per-row artist-mute (Settings → Music's artist list still has one), a Purchased
// tick instead — moves it to Confirmed Pre-orders (see vinylUi/preorderList below). A copy of the shared acts(),
// not a shared-function change, since no other card needs this swap.
function vinylActs(it, lead = '') {
  if (it.status === 'dismissed') {
    return `<div class="feed__acts feed__acts--wide"><button class="btn" data-item-act="restore" data-id="${it.id}" data-to="seen">Restore</button></div>`;
  }
  const pinned = it.status === 'pinned';
  const seen = it.status === 'new' ? `<button class="btn btn--icon" data-item-act="seen" data-id="${it.id}" data-to="seen" aria-label="Mark as seen" title="Mark as seen — clears the New highlight">${ico('check')}</button>` : '';
  return `<div class="feed__acts">${lead}${seen}
    <button class="btn btn--icon" data-item-act="dismiss" data-id="${it.id}" data-to="dismissed" aria-label="Dismiss" title="Dismiss">${ico('x')}</button>
    <button class="btn btn--icon" data-item-act="purchased" data-id="${it.id}" data-to="purchased" data-title="${rowName(it)}"${it.date && /^\d{4}-\d{2}-\d{2}$/.test(it.date) ? ` data-release-date="${esc(it.date)}"` : ''} aria-label="Mark purchased" title="Mark purchased — moves it to Confirmed Pre-orders">${ico('check')}</button>
    <button class="btn btn--icon${pinned ? ' is-pinned' : ''}" data-item-act="pin" data-id="${it.id}" data-to="${pinned ? 'seen' : 'pinned'}" aria-label="${pinned ? 'Unpin' : 'Pin'}" aria-pressed="${pinned}" title="${pinned ? 'Unpin' : 'Pin to top'}">${ico('pin')}</button></div>`;
}

function plainRow(it, k, hide) {                              // no cover: gig radar
  const t = it.meta?.time ? ` · ${it.meta.time}` : '';
  const tribute = it.meta?.tribute
    ? '<div class="feed__flag"><span class="chip chip--warn" title="Ticketmaster lists this as a tribute/cover act, or its name says so">Tribute act</span></div>' : '';
  return `<li class="feed${it.status === 'new' ? ' feed--new' : ''}${it.status === 'pinned' ? ' feed--pinned' : ''}${hide ? ' more' : ''}"${hide ? ' hidden' : ''}>
    <div class="feed__main"><div class="feed__title">${link(it, it.artist || it.title)}</div><div class="feed__sub">${esc(it.subtitle)}</div>${tribute}</div>
    <div class="feed__meta">${it.status === 'new' ? '<span class="chip chip--fresh">New</span>' : ''}<div class="feed__date">${esc(niceDate(it.date))}${esc(t)}</div>${acts(it, bookAct(it))}</div></li>`;
}

// Booked: opens the quick form (app.js → openBooking), filled in from what Ticketmaster said, which turns the show into
// a booked gig and opens it as a new Google Calendar event.
const bookAct = it => `<button class="btn btn--icon" data-item-act="book" data-id="${it.id}" data-artist="${esc(it.artist || it.title)}"
  data-venue="${esc(it.meta?.venue || '')}" data-city="${esc(it.meta?.city || '')}" data-gig-day="${/^\d{4}-\d{2}-\d{2}$/.test(it.date || '') ? esc(it.date) : ''}"
  data-time="${esc(it.meta?.time || '')}" data-url="${esc(safeUrl(it.url) === '#' ? '' : it.url)}" aria-label="Booked" title="Booked — add to Gigs and your diary">${ico('cal')}</button>`;

// ---- filtering (expanded view) --------------------------------------------------------------------------------------
export function narrow(data, ctx, todayIso, extra = {}) {
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

// ---- Vinyl radar: records you can pre-order, and what's already been bought ----------------------------------------

// Dual-content tile: the header itself has two clickable segments ("Vinyl Radar" / "Confirmed Pre-orders") that swap
// the whole body: plain bolt-on state rather than an extension to Shell's shared per-card state (filter/search only,
// used by every other grid card too) — it's a two-value enum, so it doesn't need a generic mechanism.
export const vinylUi = { mode: 'radar' };

function vinylHeader() {
  return `<button type="button" class="hud__seg" data-vinyl-mode="radar" aria-pressed="true">Vinyl Radar</button>` +
         `<button type="button" class="hud__seg" data-vinyl-mode="preorders" aria-pressed="false">Confirmed Pre-orders</button>`;
}

// Once release day arrives, "3d ago"-style ageing stops being useful — it's not late, it just hasn't been
// acknowledged yet — so day 0 and every day after both read as one open-ended "still waiting" state.
const daysBadge = n => n == null ? '<span class="chip">TBC</span>' : n <= 0 ? '<span class="chip chip--fresh">Any moment!</span>' : `<span class="chip chip--fresh">${esc(relDays(n))}</span>`;

function preorderRow(it) { return preorderRowWith(it, ''); }
export function preorderRowWith(it, lead) {
  const m = it.meta || {};
  const art = cover(m.image);
  // A store link survives onto a purchased row two ways: it's already the keeper row's own url when bought
  // straight off the radar, or it's whatever was typed into the manual-purchase form in Settings. Either way,
  // no url just means plain text — a link to "#" would look clickable and do nothing.
  const hasUrl = !!it.url;
  const name = rowName(it);
  return `<li class="feed feed--preorder">
    <span class="cover cover--art ${COVERS[0]}">${esc(initial(it.artist || it.title))}${art ? `<img class="cover__img" src="${esc(art)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer">` : ''}</span>
    <div class="feed__main">
      <div class="vin__artist" title="${esc(it.artist || '')}">${it.artist ? (hasUrl ? link(it, it.artist) : esc(it.artist)) : ''}</div>
      <div class="vin__album" title="${esc(it.title)}">${hasUrl ? link(it, it.title) : esc(it.title)}</div>
      <div class="vin__facts">${esc(m.variant || m.format || '')}</div></div>
    <div class="feed__meta">
      <div class="preorder__row">${daysBadge(it.in_days)}
        <input type="date" class="preorder-date" data-preorder-date="${it.id}" value="${it.date && /^\d{4}-\d{2}-\d{2}$/.test(it.date) ? it.date : ''}" aria-label="Release date — leave blank if TBC"></div>
      <div class="preorder__row">${lead}
        <button class="btn" data-item-act="received" data-id="${it.id}" data-title="${name}">Received</button>
        <button class="btn" data-item-act="remove" data-id="${it.id}" data-title="${name}">Remove</button></div></div></li>`;
}

function preorderList(s) {
  const list = s.widgets.vinyl.data?.preorders || [];
  if (!list.length) {
    return { meta: '0 confirmed', body: empty('disc', 'No confirmed pre-orders yet', "Mark a pre-order as Purchased on the radar, or add one bought elsewhere in Settings → Music.", settingsLink('Add a purchase')) };
  }
  return { meta: `${list.length} confirmed`, body: `<ul class="rows rows--cols">${list.map(preorderRow).join('')}</ul>` };
}

function vinyl(s, ctx) {
  if (vinylUi.mode === 'preorders') return preorderList(s);
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

// ---- Next gig -------------------------------------------------------------------------------------------------------
function gigLine(g, past = false) {
  const where = [g.venue, g.city].filter(Boolean).join(', ');
  const tix = g.ticket_url ? ` <a class="feed__link" href="${esc(safeUrl(g.ticket_url))}" target="_blank" rel="noopener">tickets</a>` : '';
  // A booked gig can be (re)added to the diary any time — for one typed in Settings, or if the Google tab was closed unsaved.
  const diary = past ? '' : ` <a class="feed__link" href="${esc(gcalUrl(g))}" target="_blank" rel="noopener" title="Add to Google Calendar">diary</a>`;
  return `<li class="gigline"><span class="gigline__d mono">${esc(fmtDay(g.date))}</span>
    <div class="gigline__main"><b>${esc(g.artist)}</b><div class="gigline__sub">${esc(where)}${g.doors ? ` · doors ${esc(g.doors)}` : ''}${g.notes ? ` · ${esc(g.notes)}` : ''}${tix}${diary}</div></div>
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
      <div class="hero__sub">${esc(where)} · ${esc(fmtDay(n.date))}${n.doors ? ` · doors ${esc(n.doors)}` : ''} · <a class="feed__link" href="${esc(gcalUrl(n))}" target="_blank" rel="noopener" title="Add to Google Calendar">diary</a></div>
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
  { id: 'vinyl', area: 'vin', label: vinylHeader(), labelHtml: true, flush: true, render: vinyl, markSeen: 'vinyl',
    filters: [...CURATE, { label: 'Has date', value: 'dated' }, DISMISSED] },
  { id: 'radar', area: 'radar', label: 'Gig radar', flush: true, render: radar, search: true, markSeen: 'radar', filters: [...CURATE, { label: 'Tributes', value: 'tribute' }, DISMISSED] },
];
