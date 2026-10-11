// Card definitions. Each card has: id, grid area, label, and render(state, ctx) -> { meta, body }.
// Markup and class names follow the approved mockup (web/mock/), so the CSS is shared.
//
// Anything marked class="more" (with `hidden`) is expanded-view-only; the shell reveals it when the card opens.
import { esc, ico, safeUrl, hostUrl, fmtDay, relDays, hhmm, bytes, duration, uptimeText, isoOf, parseDate } from './util.js';
import { contentCards } from './content-cards.js';

/** UI state that lives in the browser rather than the server (in-flight start/stop, selected log). */
export const ui = { pending: {}, selected: null, logText: '' };

const COLOURS = { violet: 'var(--accent-hi)', silver: 'var(--silver)', indigo: '#6d5bd0' };
const staleChip = w => (w.error ? ' <span class="chip chip--warn">stale</span>' : '');
const empty = (icon, title, text, chip = '') =>
  `<div class="empty">${ico(icon)}<div class="empty__title">${esc(title)}</div><p>${esc(text)}</p>${chip ? `<span class="chip">${esc(chip)}</span>` : ''}</div>`;
/** Owner-only pin toggle: shows on hover, and stays visible once pinned. */
export const pinButton = (kind, id, pinned) =>
  `<button class="pinbtn${pinned ? ' is-on' : ''}" data-pin="${kind}" data-id="${esc(id)}" data-to="${!pinned}" aria-label="${pinned ? 'Unpin' : 'Pin to top'}" title="${pinned ? 'Unpin' : 'Pin to top'}">${ico('pin')}</button>`;

// ---- Now: clock + the only two bin collections that matter ---------------------------------------------

function binRow(item, streams, hour) {
  if (!item) return '<li class="note">No upcoming collection found.</li>';
  const s = streams[item.stream] || { title: item.stream, sub: '', colour: '#6b7280' };
  const chip = item.in_days === 0 ? '<span class="chip chip--today">Today</span>'
    : item.in_days === 1 && hour >= 17 ? '<span class="chip chip--today">Tonight</span>' : '';
  return `<li class="bin" style="--sw:${esc(s.colour)}"><span class="bin__swatch"></span>
    <div><div class="bin__name">${esc(s.title)}${chip}</div><div class="bin__sub">${esc(s.sub)}</div></div>
    <div class="bin__when">${esc(fmtDay(item.date))}<br>${esc(relDays(item.in_days))}</div></li>`;
}

function monthGrid(first, marks, streams, todayIso) {
  const y = first.getFullYear(), m0 = first.getMonth();
  const lead = (first.getDay() + 6) % 7, days = new Date(y, m0 + 1, 0).getDate();
  let cells = [...'MTWTFSS'].map(d => `<span class="cal__dow">${d}</span>`).join('') + '<span class="cd cd--out"></span>'.repeat(lead);
  for (let d = 1; d <= days; d++) {
    const iso = isoOf(y, m0, d);
    const bars = (marks[iso] || []).map(k => `<i style="--sw:${esc(streams[k]?.colour || '#6b7280')}"></i>`).join('');
    cells += `<span class="cd${iso === todayIso ? ' cd--today' : ''}${iso < todayIso ? ' cd--past' : ''}"><b>${d}</b>${bars}</span>`;
  }
  return `<div class="cal"><div class="cal__h">${esc(first.toLocaleDateString('en-GB', { month: 'long', year: 'numeric' }))}</div><div class="cal__g">${cells}</div></div>`;
}

function collectionCalendars(data, todayIso) {
  const marks = Object.fromEntries(data.upcoming.map(u => [u.date, u.streams]));
  const t = parseDate(todayIso);
  const months = [0, 1, 2].map(i => monthGrid(new Date(t.getFullYear(), t.getMonth() + i, 1), marks, data.streams, todayIso)).join('');
  const legend = Object.values(data.streams)
    .map(s => `<span style="--sw:${esc(s.colour)}"><i></i>${esc(s.title)} <span class="dim">— ${esc(s.sub)}</span></span>`).join('');
  return `<div class="calm more" hidden>${months}<div class="cal__legend">${legend}</div></div>`;
}

function now(s) {
  const w = s.widgets.bins;
  const hour = new Date(s.now).getHours();
  const today = s.now.slice(0, 10);
  let list, extra = '';
  if (!w.configured) {
    list = '<li class="note">Add your council property reference in settings to see bin days.</li>';
  } else if (!w.data?.next) {                                               // nothing synced yet (the widget's view is {} until the first fetch)
    list = `<li class="note">${w.error ? `Couldn't read the council page: ${esc(w.error)}` : 'Fetching bin days…'}</li>`;
  } else {
    const d = w.data;
    list = binRow(d.next.recycling, d.streams, hour) + binRow(d.next.domestic, d.streams, hour)
      + `<li class="note more" hidden>Recycling alternates blue and brown; domestic follows the council's three-weekly cycle. Synced ${esc(hhmm(w.fetched_at))} from the council website.</li>`
      + d.upcoming.slice(0, 14).map(u => `<li class="sched more" hidden><span class="sched__d">${esc(fmtDay(u.date))}</span><span class="sched__b">${
        u.streams.map(k => `<span style="--sw:${esc(d.streams[k]?.colour || '#6b7280')}"><i></i>${esc(d.streams[k]?.title || k)}</span>`).join('')}</span></li>`).join('');
    extra = collectionCalendars(d, today);
  }
  return {
    meta: w.fetched_at ? `bins synced ${esc(hhmm(w.fetched_at))}${staleChip(w)}` : 'bins',
    body: `<div class="now__clock"><div class="clock__time" data-clock></div>
      <div class="clock__date"><span data-dow></span><span data-dmy></span></div></div>
      ${extra}<div class="now__label hud">Next collections</div><ul class="rows">${list}</ul>`,
  };
}

// ---- Diary ------------------------------------------------------------------------------------------------

function addDays(iso, n) { const d = parseDate(iso); d.setDate(d.getDate() + n); return isoOf(d.getFullYear(), d.getMonth(), d.getDate()); }

function diary(s, ctx) {
  const w = s.widgets.diary;
  if (!w.configured) return { meta: 'not set up', body: empty('cal', 'Connect a calendar', 'Add a Google Calendar secret iCal address in settings.') };
  if (!w.data?.events) return { meta: w.error ? 'error' : 'loading', body: empty('cal', w.error ? "Couldn't read the calendar" : 'Loading…', w.error || '') };

  const today = s.now.slice(0, 10), tomorrow = addDays(today, 1);
  const range = ctx.expanded ? Number(ctx.filter || 14) : 14;
  const cutoff = addDays(today, range);
  const groups = new Map();
  for (const e of w.data.events) {
    if (e.date > cutoff) continue;
    if (!groups.has(e.date)) groups.set(e.date, []);
    groups.get(e.date).push(e);
  }
  let shown = 0, idx = 0;
  const html = [...groups].map(([date, evs]) => {
    const compact = idx < 4 && shown + evs.length <= 6;         // what fits in the small card; the rest is expanded-only
    idx++; shown += evs.length;
    const label = date === today ? `Today<span> · ${esc(fmtDay(date))}</span>` : date === tomorrow ? `Tomorrow<span> · ${esc(fmtDay(date))}</span>` : esc(fmtDay(date));
    const rows = evs.map(e => {
      const sub = [e.until ? `until ${fmtDay(e.until)}` : '', e.location].filter(Boolean).join(' · ');
      return `<li class="ev" style="--cal:${COLOURS[e.colour] || COLOURS.violet}"><span class="ev__t">${e.all_day ? 'All day' : esc(e.time)}</span><span class="ev__bar"></span>
        <div class="ev__main"><div class="ev__title">${esc(e.title)}</div>${sub ? `<div class="ev__sub">${esc(sub)}</div>` : ''}</div></li>`;
    }).join('');
    return `<div class="dg${compact ? '' : ' more'}"${compact ? '' : ' hidden'}><div class="day${date <= tomorrow ? ' day--next' : ''}">${label}</div><ul class="rows">${rows}</ul></div>`;
  }).join('');
  return {
    meta: `${esc(w.data.calendars[0]?.name || 'calendar')} · ${esc(hhmm(w.fetched_at))}${staleChip(w)}`,
    body: html ? `<div class="cols">${html}</div>` : empty('cal', 'Nothing planned', `No events in the next ${range} days.`),
  };
}

// ---- Projects ---------------------------------------------------------------------------------------------

export function selectedSlug(state) {
  const list = state.projects.filter(p => p.show_projects);   // only ever a project with a visible row to select
  if (ui.selected && list.some(p => p.slug === ui.selected)) return ui.selected;
  return (list.find(p => p.managed) || list[0])?.slug || null;
}

const ms = n => (n < 1 ? '<1 ms' : `${n} ms`);
const STATE_TEXT = { running: 'running', starting: 'starting…', unhealthy: 'not responding', external: 'running (external)', stopped: 'stopped' };

function projects(s, ctx) {
  const up = p => p.state === 'running' || p.state === 'external';
  const now = new Date(s.now).getTime() / 1000;
  // A project can opt out of the Projects card (Settings → "Show in Projects") once it's launched from the
  // Launchpad instead — a standalone app with nothing to stop doesn't need a row here at all.
  let list = s.projects.filter(p => p.show_projects);
  if (ctx.expanded && ctx.filter === 'running') list = list.filter(p => up(p) || p.state === 'starting');
  if (ctx.expanded && ctx.filter === 'stopped') list = list.filter(p => p.state === 'stopped');
  const sel = selectedSlug(s);
  const rows = list.map(p => {
    const busy = ui.pending[p.slug];
    const dot = { running: 'dot--ok', external: 'dot--ok', starting: 'dot--warn', unhealthy: 'dot--bad' }[p.state] || '';
    const name = p.url && up(p)
      ? `<a class="proj__name" href="${esc(hostUrl(p.url))}" target="_blank" rel="noopener">${esc(p.name)} ${ico('ext', 'xi')}</a>`
      : `<span class="proj__name">${esc(p.name)}</span>`;
    // Once running, a live-ticking uptime (from systemd's own record of when the unit started) replaces the
    // health check's latency reading — more useful at a glance, and it keeps counting through page refreshes.
    const uptime = p.state === 'running' && p.since != null
      ? `<span data-since="${Number(p.since)}">${esc(uptimeText(now - p.since))}</span>` : esc(STATE_TEXT[p.state]);
    const detail = p.kind === 'web' ? `:${esc(String(p.port))} · ${uptime}` : `desktop app · ${uptime}`;
    const act = busy ? `<button class="btn" disabled>${busy === 'start' ? 'Starting…' : 'Stopping…'}</button>`
      : p.managed ? `<button class="btn btn--stop" data-act="stop" data-slug="${esc(p.slug)}">${ico('stop')}Stop</button>`
      : p.state === 'external' ? '<span class="chip">External</span>'
      : `<button class="btn btn--go" data-act="start" data-slug="${esc(p.slug)}">${ico('play')}Start</button>`;
    return `<li class="proj${ctx.expanded && p.slug === sel ? ' is-selected' : ''}" data-select data-slug="${esc(p.slug)}"><span class="dot ${dot}"></span>
      <div>${name}<div class="proj__desc">${detail}${p.lan_visible ? '' : ' · <span class="chip chip--warn">this pc only</span>'}</div></div>
      <div class="proj__act">${pinButton('project', p.slug, p.pinned)}${act}</div></li>`;
  }).join('');
  const visible = s.projects.filter(p => p.show_projects);
  const selected = visible.find(p => p.slug === sel);
  const log = selected
    ? `<div class="logwrap more" hidden><div class="hud">Log · ${esc(selected.name)} · live</div><pre class="log" data-log>${esc(ui.logText || 'No output yet — start the project to see its log.')}</pre></div>` : '';
  return {
    meta: `${visible.filter(up).length} / ${visible.length} running`,
    body: `<ul class="rows">${rows || '<li class="note">No projects match.</li>'}</ul>${log}`,
  };
}

// ---- Launchpad --------------------------------------------------------------------------------------------

// A project can opt in (Settings → Projects → "Show in Launchpad") to also appear here — the primary place for
// launching things. A web project's tile is a plain clickthrough link, same as any other (it still start/stops from
// the Projects card); a desktop project has no page to link to, so its tile *is* the start button.
function projectTile(p) {
  const running = p.state === 'running' || p.state === 'external';
  if (p.kind === 'web') {
    const dot = running ? '<span class="dot dot--ok"></span>' : p.managed ? '<span class="dot dot--warn"></span>' : '<span class="dot dot--bad"></span>';
    return `<a class="tile${p.pinned ? ' tile--pinned' : ''}" href="${esc(hostUrl(p.url))}" target="_blank" rel="noopener">
      ${ico(p.icon)}<div><b>${esc(p.name)}</b><small>${p.port ? `:${p.port}` : esc(STATE_TEXT[p.state])}</small></div>${dot}</a>`;
  }
  const busy = ui.pending[p.slug];
  const dot = running ? '<span class="dot dot--ok"></span>' : '';
  return `<button class="tile${p.pinned ? ' tile--pinned' : ''}" type="button" data-act="start" data-slug="${esc(p.slug)}" ${busy ? 'disabled' : ''}>
    ${ico(p.icon)}<div><b>${esc(p.name)}</b><small>${busy ? 'Launching…' : (running ? 'running' : 'desktop app')}</small></div>${dot}</button>`;
}

function launchpad(s, ctx) {
  const q = ctx.expanded ? ctx.search : '';
  const items = [
    ...s.links.map(l => ({ key: `link:${l.id}`, pinned: l.pinned, pin_order: l.pin_order, main: l.tier === 'main', search: `${l.name} ${l.subtitle}`,
      html: `<a class="tile${l.primary ? ' tile--primary' : ''}${l.pinned ? ' tile--pinned' : ''}" href="${esc(safeUrl(l.url))}" target="_blank" rel="noopener">
        ${ico(l.icon)}<div><b>${esc(l.name)}</b><small>${esc(l.subtitle)}</small></div>${l.up === true ? '<span class="dot dot--ok"></span>' : l.up === false ? '<span class="dot dot--bad"></span>' : ''}</a>`,
      pin: pinButton('link', l.id, l.pinned) })),
    ...s.projects.filter(p => p.show_launchpad).map(p => ({ key: `project:${p.slug}`, pinned: p.pinned, pin_order: p.pin_order, main: true, search: `${p.name} ${p.description || ''}`,
      html: projectTile(p), pin: pinButton('project', p.slug, p.pinned) })),
  ];
  const list = q ? items.filter(i => i.search.toLowerCase().includes(q)) : items;
  // Pinned/main-tier tiles render in the compact card too — 9 (3x3) show without scrolling, the rest reveal by
  // scrolling the card itself (like the other cards' compact view), no need to expand. Only "more"-tier, unpinned
  // links (the deliberately lower-priority ones) stay archive-only, same as before. pin_order is one sequence
  // shared by links and projects server-side, so sorting by it gives the real pin order across both — the item
  // pinned first shows first, regardless of which kind of thing it is or when its source array was sorted.
  const sorted = [...list].sort((a, b) => (a.pin_order ?? Infinity) - (b.pin_order ?? Infinity));
  const tiles = sorted.map(i => {
    const more = !(i.pinned || i.main);
    return `<div class="tilewrap${more ? ' more' : ''}"${more ? ' hidden' : ''}>${i.html}${i.pin}</div>`;
  }).join('');
  return { meta: '', body: tiles ? `<div class="tiles">${tiles}</div>` : empty('globe', 'No links', 'Nothing matches.') };
}

// ---- System -----------------------------------------------------------------------------------------------

function bar(label, sub, value, pct, hot = false) {
  const v = Math.max(0, Math.min(100, Number(pct) || 0));
  return `<div class="bar"><div class="bar__top"><span>${esc(label)}${sub ? ` <span class="dim">${esc(sub)}</span>` : ''}</span><b>${esc(value)}</b></div>
    <div class="bar__track"><div class="bar__fill${hot ? ' bar__fill--hot' : ''}" style="--v:${v}%"></div></div></div>`;
}

function system(s) {
  const w = s.widgets.system;
  if (!w.data?.disks) return { meta: '', body: empty('chip', 'Reading host…', w.error || '') };
  const d = w.data;
  const checks = [
    ...s.projects.filter(p => p.kind === 'web').map(p => ({ name: p.name, ok: p.state === 'running' || p.state === 'external', detail: `:${p.port} · ${p.latency_ms != null ? ms(p.latency_ms) : STATE_TEXT[p.state]}` })),
    ...s.links.filter(l => l.up !== null).map(l => ({ name: l.name, ok: l.up, detail: l.subtitle })),
  ];
  const disks = d.disks.map(k => bar(k.name, k.mount, `${bytes(k.used)} / ${bytes(k.size)}`, k.percent, k.percent >= 85)).join('');
  const mem = d.mem, memPct = mem.used / mem.total * 100;
  return {
    meta: `${esc(d.host)} · ${esc(s.ip)}${staleChip(w)}`,
    body: `${disks}
      ${d.cpu_percent != null ? `<div class="more" hidden>${bar('cpu', '', `${d.cpu_percent}%`, d.cpu_percent)}</div>` : ''}
      <div class="more" hidden>${bar('memory', '', `${bytes(mem.used)} / ${bytes(mem.total)}`, memPct)}</div>
      <dl class="kv"><dt>uptime</dt><dd>${esc(duration(d.uptime_s))}</dd><dt>load</dt><dd>${d.load.map(x => x.toFixed(2)).join(' · ')}</dd>
        <dt>services</dt><dd>${checks.filter(c => c.ok).length} / ${checks.length} up</dd></dl>
      <ul class="svcs">${checks.map(c => `<li class="svc more" hidden><span class="dot ${c.ok ? 'dot--ok' : ''}"></span><span>${esc(c.name)}</span><span class="mono dim">${esc(c.detail)}</span></li>`).join('')}</ul>`,
  };
}

// ---- Monitoring ---------------------------------------------------------------------------------------------

const tempClass = t => (t == null ? '' : t >= 85 ? ' mon-temp--hot' : t >= 70 ? ' mon-temp--warm' : '');
// A column's heading carries its temperature, so the card stays short enough for the 1440p grid even in a browser window.
const monHead = (label, temp, withTemp = true) =>
  `<div class="mon-head"><span class="mon-col__label">${esc(label)}</span>${withTemp
    ? `<span class="mon-temp${tempClass(temp)}" title="${esc(label)} temperature"><small>temp</small>${temp != null ? `${temp.toFixed(1)}°` : '—'}</span>` : ''}</div>`;
const busiestCore = cores => cores.reduce((max, p) => (p != null && (max == null || p > max) ? p : max), null);

function monitoring(s, ctx) {
  const m = s.monitoring;
  if (!m) return { meta: '', body: empty('chip', 'Reading sensors…', '') };
  const cpuTemp = m.cpu.temp.package;
  const gpuTemp = m.gpu?.temp ?? null;
  const cpuPct = m.cpu.percent ?? 0;
  const memPct = m.mem.used / m.mem.total * 100;

  if (!ctx.expanded) {
    const busiest = busiestCore(m.cpu.per_core);
    const vramPct = m.gpu ? m.gpu.mem_used_mb / m.gpu.mem_total_mb * 100 : 0;
    const powerPct = m.gpu?.power_limit_w ? m.gpu.power_w / m.gpu.power_limit_w * 100 : 0;
    const gpuBars = m.gpu
      ? `${bar('Usage', '', `${m.gpu.percent}%`, m.gpu.percent, m.gpu.percent >= 85)}
         ${bar('VRAM', '', `${Math.round(vramPct)}%`, vramPct, vramPct >= 85)}
         ${m.gpu.power_limit_w ? bar('Power', '', `${m.gpu.power_w.toFixed(0)} W`, powerPct, powerPct >= 90) : ''}`
      : '<p class="dim">No GPU detected.</p>';
    return {
      meta: '',
      body: `<div class="mon-split">
        <div class="mon-col">
          ${monHead('CPU', cpuTemp)}
          <div class="mon-bars">
            ${bar('Usage', '', m.cpu.percent != null ? `${m.cpu.percent}%` : '—', cpuPct, cpuPct >= 85)}
            ${bar('Busiest core', '', busiest != null ? `${busiest}%` : '—', busiest ?? 0, (busiest ?? 0) >= 85)}
            ${bar('RAM', '', `${Math.round(memPct)}%`, memPct, memPct >= 85)}
          </div>
        </div>
        <div class="mon-col">
          ${monHead('GPU', gpuTemp, !!m.gpu)}
          <div class="mon-bars">${gpuBars}</div>
        </div>
      </div>`,
    };
  }

  const cores = m.cpu.per_core.map((p, i) => bar(`core ${i}`, '', p != null ? `${p}%` : '—', p ?? 0, (p ?? 0) >= 85)).join('');
  const chiplets = m.cpu.temp.chiplets.map(c => `<dt>${esc(c.label)}</dt><dd>${c.temp.toFixed(1)}°C</dd>`).join('');
  const mem = m.mem, swapPct = mem.swap_total ? mem.swap_used / mem.swap_total * 100 : 0;
  const gpuSection = m.gpu
    ? `<div class="mon-section"><span class="hud">GPU · ${esc(m.gpu.name)}</span>
        ${bar('Usage', '', `${m.gpu.percent}%`, m.gpu.percent, m.gpu.percent >= 85)}
        ${bar('VRAM', '', `${bytes(m.gpu.mem_used_mb * 1024 * 1024)} / ${bytes(m.gpu.mem_total_mb * 1024 * 1024)}`, m.gpu.mem_used_mb / m.gpu.mem_total_mb * 100)}
        ${m.gpu.power_limit_w ? bar('Power', '', `${m.gpu.power_w.toFixed(0)} W / ${m.gpu.power_limit_w.toFixed(0)} W`, m.gpu.power_w / m.gpu.power_limit_w * 100) : ''}
        <dl class="kv">
          <dt>temperature</dt><dd>${m.gpu.temp.toFixed(1)}°C</dd>
          <dt>core clock</dt><dd>${m.gpu.clock_sm_mhz.toFixed(0)} MHz</dd>
          <dt>memory clock</dt><dd>${m.gpu.clock_mem_mhz.toFixed(0)} MHz</dd>
          ${m.gpu.fan_percent != null ? `<dt>fan</dt><dd>${m.gpu.fan_percent.toFixed(0)}%</dd>` : ''}
        </dl>
      </div>`
    : `<div class="mon-section"><span class="hud">GPU</span><p class="dim">No NVIDIA GPU detected.</p></div>`;

  return {
    meta: '',
    body: `<div class="mon-section"><span class="hud">CPU</span>
        ${bar('Overall', '', m.cpu.percent != null ? `${m.cpu.percent}%` : '—', cpuPct, cpuPct >= 85)}
        <div class="mon-cores">${cores}</div>
        <dl class="kv">
          <dt>package temp</dt><dd>${cpuTemp != null ? `${cpuTemp.toFixed(1)}°C` : '—'}</dd>
          ${chiplets}
          <dt>load (1/5/15m)</dt><dd>${m.load.map(x => x.toFixed(2)).join(' · ')}</dd>
        </dl>
      </div>
      ${gpuSection}
      <div class="mon-section"><span class="hud">Memory</span>
        ${bar('Used', '', `${bytes(mem.used)} / ${bytes(mem.total)}`, memPct, memPct >= 85)}
        ${mem.swap_total ? bar('Swap', '', `${bytes(mem.swap_used)} / ${bytes(mem.swap_total)}`, swapPct) : ''}
        <dl class="kv"><dt>buffers + cached</dt><dd>${bytes(mem.buffers + mem.cached)}</dd></dl>
      </div>`,
  };
}

// ---- Registry (order = order in the flowing grid on small screens) -----------------------------------------

const content = Object.fromEntries(contentCards.map(c => [c.id, c]));

export const cards = [
  { id: 'now', area: 'now', cls: 'now', label: 'Now', flush: true, render: now },
  content.gigs,
  { id: 'diary', area: 'diary', label: 'Diary', flush: true, render: diary,
    filters: [{ label: 'Next 7 days', value: '7' }, { label: 'Next 14 days', value: '14' }, { label: 'Next 30 days', value: '30' }, { label: 'Next 60 days', value: '60' }], defaultFilter: '14' },
  { id: 'projects', area: 'proj', label: 'Projects', flush: true, render: projects,
    filters: [{ label: 'All', value: 'all' }, { label: 'Running', value: 'running' }, { label: 'Stopped', value: 'stopped' }] },
  content.releases,
  content.vinyl,
  content.radar,
  // Headlines moved to its own News page (rail/tabbar) — this slot is now live hardware monitoring.
  { id: 'monitoring', area: 'news', label: 'Monitoring', render: monitoring },
  { id: 'launchpad', area: 'launch', label: 'Launchpad', render: launchpad, search: true },
  { id: 'system', area: 'sys', label: 'System', render: system },
];
