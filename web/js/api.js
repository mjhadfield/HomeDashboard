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

/** Server-sent events. Every event is just "something changed"; the caller re-fetches the state. */
export function subscribe(onChange, onStatus, url = '/api/owner/events') {
  const es = new EventSource(url);
  es.onopen = () => onStatus(true);
  es.onerror = () => onStatus(false);            // EventSource reconnects by itself (server sends retry: 3000)
  for (const name of ['widget', 'projects', 'health', 'config']) es.addEventListener(name, onChange);
  return es;
}
