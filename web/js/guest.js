// Guest page: shows only the links the owner has marked for guests (Jellyfin). No auth, no owner data.
import { esc, ico, safeUrl } from './util.js';

const doors = document.getElementById('doors');
const status = document.getElementById('status');

async function load() {
  try {
    const { links } = await (await fetch('/api/guest')).json();
    doors.innerHTML = links.map(l => `<a class="door cut" href="${esc(safeUrl(l.url))}">${ico(l.icon)}
      <span class="door__title">${esc(l.name)}</span><span class="door__sub">${esc(l.subtitle)}</span></a>`).join('')
      || '<p class="guest__hello">Nothing is shared here yet.</p>';
    const down = links.some(l => l.up === false);
    const known = links.some(l => l.up !== null);
    status.innerHTML = known ? `<span class="dot ${down ? 'dot--bad' : 'dot--ok'}"></span>${down ? 'Server unreachable right now' : 'Media server online'}` : '';
  } catch {
    status.innerHTML = '<span class="dot dot--warn"></span>Can’t reach the dashboard';
  }
}
load();
setInterval(load, 30000);
