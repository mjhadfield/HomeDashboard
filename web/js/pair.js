// An unpaired screen: ask the server for a short code, show it, and poll until an owner approves it.
// Approval sets this screen's (read-only) cookie on the poll response, then we reload into the TV view.
const code = document.getElementById('code');
const status = document.getElementById('status');
const say = (dot, text) => { status.innerHTML = `<span class="dot ${dot}"></span>${text}`; };

async function pair() {
  let res;
  try { res = await fetch('/api/pair/start', { method: 'POST' }); } catch { say('dot--warn', 'Can’t reach the dashboard — retrying…'); return setTimeout(pair, 5000); }
  if (res.status === 429) { code.textContent = '······'; say('dot--warn', 'Too many screens waiting — retrying…'); return setTimeout(pair, 20000); }
  if (!res.ok) { say('dot--bad', 'Something went wrong — retrying…'); return setTimeout(pair, 10000); }
  const { code: c, secret, expires_in } = await res.json();
  code.textContent = c;
  say('dot--warn', 'Waiting for approval');
  const deadline = Date.now() + expires_in * 1000;

  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 2000));
    try {
      const poll = await fetch('/api/pair/poll', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ secret }) });
      if (poll.status === 410) break;                                   // expired: get a fresh code
      if (poll.ok && (await poll.json()).status === 'ok') { say('dot--ok', 'Paired'); return location.replace('/'); }
    } catch { /* network blip: keep polling */ }
  }
  pair();
}
pair();
