// Thin client for the owner API and its event stream.

async function request(method, path, body) {
  const res = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: { 'X-Hub': '1', ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}) },   // X-Hub: CSRF guard
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 403) { location.reload(); throw new Error('Not an owner device'); }          // revoked -> server now serves the guest page
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = message((await res.json()).detail) || detail; } catch { /* not JSON */ }
    throw new Error(detail);
  }
  return res.json();
}

/** FastAPI sends a string for our own errors and a list of {loc, msg} for validation failures. */
function message(detail) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail.map(d => {
      const field = (d.loc || []).filter(x => x !== 'body').join(' → ');
      const msg = String(d.msg || '').replace(/^Value error, /, '');
      return field ? `${field}: ${msg}` : msg;
    }).join('; ');
  }
  return '';
}

export const get = path => request('GET', path);
export const post = (path, body) => request('POST', path, body ?? {});
export const put = (path, body) => request('PUT', path, body ?? {});
export const patch = (path, body) => request('PATCH', path, body ?? {});
export const del = path => request('DELETE', path);

/** Server-sent events. Every event is just "something changed"; the caller re-fetches the state.
 *
 * A browser allows only six connections to one address, shared by all its tabs, and each open stream holds one of them
 * for good. Once six are taken by streams, every other request to Citadel queues forever (pages stuck on a spinner). So:
 * one stream per tab and URL, shared by everything on the page that subscribes; and a tab that's been hidden for a while
 * lets go of it, reconnecting when it's shown again. Subscribers then hear onStatus(true) and re-fetch what they missed. */
const DEFAULT_EVENTS = ['widget', 'projects', 'health', 'config'];
const HIDDEN_GRACE = 15_000;                       // a quick look at another tab keeps the stream
const streams = new Map();                         // url -> {es, subs, timer}

function openStream(url, s) {
  const es = new EventSource(url);
  s.es = es;
  es.onopen = () => s.subs.forEach(x => x.onStatus(true));
  es.onerror = () => s.subs.forEach(x => x.onStatus(false));   // EventSource reconnects by itself (server sends retry: 3000)
  for (const x of s.subs) for (const name of x.names) es.addEventListener(name, x.onChange);
}

export function subscribe(onChange, onStatus = () => {}, url = '/api/owner/events', names = DEFAULT_EVENTS) {
  let s = streams.get(url);
  if (!s) streams.set(url, s = { es: null, subs: new Set(), timer: 0 });
  const sub = { onChange, onStatus, names };
  s.subs.add(sub);
  if (s.es) {
    for (const name of names) s.es.addEventListener(name, onChange);
    if (s.es.readyState === EventSource.OPEN) onStatus(true);
  } else if (!document.hidden) openStream(url, s);
}

document.addEventListener('visibilitychange', () => {
  for (const [url, s] of streams) {
    clearTimeout(s.timer);
    if (document.hidden) s.timer = setTimeout(() => { s.es?.close(); s.es = null; }, HIDDEN_GRACE);
    else if (!s.es) openStream(url, s);
  }
});
