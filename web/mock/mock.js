// Mockup-only behaviour: live clock, fake start/stop, filter chips, and the
// expand-a-card interaction. Replaced by real widget custom elements + API calls in the build phase.

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;

// ---- clock --------------------------------------------------------------
function tick() {
  const d = new Date();
  $$('[data-clock]').forEach(t => t.innerHTML = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}<small>${String(d.getSeconds()).padStart(2, '0')}</small>`);
  $$('[data-date]').forEach(t => t.textContent = d.toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long' }));
  $$('[data-dow]').forEach(t => t.textContent = d.toLocaleDateString('en-GB', { weekday: 'long' }));
  $$('[data-dmy]').forEach(t => t.textContent = d.toLocaleDateString('en-GB', { day: 'numeric', month: 'long' }));
}
tick(); setInterval(tick, 1000);

// ---- fake start/stop ------------------------------------------------------
$$('[data-proj]').forEach(row => {
  const btn = $('[data-toggle]', row), dot = $('.dot', row);
  if (!btn) return;
  btn.addEventListener('click', () => {
    const running = dot.classList.contains('dot--ok');
    dot.className = 'dot ' + (running ? '' : 'dot--ok');
    btn.className = 'btn ' + (running ? 'btn--go' : 'btn--stop');
    btn.innerHTML = `<svg class="i"><use href="#i-${running ? 'play' : 'stop'}"/></svg>${running ? 'Start' : 'Stop'}`;
  });
});

// ---- dismiss / pin on feed items ---------------------------------------------
document.addEventListener('click', e => {
  const b = e.target.closest('.feed__acts button'); if (!b) return;
  const row = b.closest('.feed');
  if (b.dataset.act === 'dismiss') { row.style.transition = 'opacity .3s, transform .3s'; row.style.opacity = 0; row.style.transform = 'translateX(12px)'; setTimeout(() => { row.remove(); clip(); }, 300); }
  if (b.dataset.act === 'pin') b.style.color = b.style.color ? '' : 'var(--accent-soft)';
});

// ---- filter chips (visual only in the mockup) ------------------------------------
document.addEventListener('click', e => {
  const f = e.target.closest('.fchip'); if (!f) return;
  $$('.fchip', f.parentElement).forEach(x => x.setAttribute('aria-pressed', x === f));
});

// ---- expand / collapse a card -----------------------------------------------------
let openCard = null, placeholder = null;

function apply(card, on) {
  if (on) {
    placeholder = card.cloneNode(false);            // keeps the grid cell (same classes + area)
    placeholder.classList.add('ph'); placeholder.removeAttribute('id'); placeholder.style.viewTransitionName = '';
    placeholder.style.height = card.offsetHeight + 'px';
    card.after(placeholder);
    card.classList.add('is-max');
    document.body.classList.add('has-max');
  } else {
    card.classList.remove('is-max');
    document.body.classList.remove('has-max');
    placeholder?.remove(); placeholder = null;
  }
  $$('.more, .maxbar', card).forEach(el => el.hidden = !on);
  const btn = $('[data-max]', card);
  btn.setAttribute('aria-expanded', on);
  btn.setAttribute('aria-label', on ? 'Collapse' : 'Expand');
  btn.innerHTML = `<svg class="i"><use href="#i-${on ? 'shrink' : 'expand'}"/></svg>`;
  $$('.card__body', card).forEach(b => b.scrollTop = 0);
  openCard = on ? card : null;
  clip();
}

function toggle(card) {
  const on = !card.classList.contains('is-max');
  if (on && openCard) return;
  if (document.startViewTransition && !reduced) {
    card.style.viewTransitionName = 'maxcard';
    document.startViewTransition(() => apply(card, on)).finished.finally(() => { card.style.viewTransitionName = ''; });
  } else apply(card, on);
}

document.addEventListener('click', e => {
  const card = e.target.closest('.card');
  if (openCard && !e.target.closest('.is-max')) { toggle(openCard); return; }               // click on the backdrop
  if (!card || e.target.closest('a, input, .fchip, .feed__acts, [data-toggle], [data-act]')) return;
  const onHead = e.target.closest('.card__head');
  const gear = e.target.closest('.card__head .btn:not([data-max])');
  if (gear) return;
  if (e.target.closest('[data-max]') || onHead) toggle(card);
});
document.addEventListener('keydown', e => { if (e.key === 'Escape' && openCard) toggle(openCard); });

// ---- fade at the bottom of a card that's scrolled/clipped ---------------------------------
function clip() {
  $$('.card__body').forEach(b => {
    b.classList.toggle('is-clipped', b.scrollHeight > b.clientHeight + 2);
    b.classList.toggle('at-end', b.scrollTop + b.clientHeight >= b.scrollHeight - 2);
  });
}
$$('.card__body').forEach(b => b.addEventListener('scroll', clip, { passive: true }));
addEventListener('resize', clip);
document.fonts?.ready.then(clip); requestAnimationFrame(clip);

// ---- nav jump: flash the card (the fixed 1440p layout has nothing to scroll to) -----------
$$('.rail a[href^="#"], .tabbar a[href^="#"]').forEach(a => a.addEventListener('click', () => {
  const t = $(a.getAttribute('href')); if (!t || !t.classList.contains('card')) return;
  t.classList.remove('flash'); void t.offsetWidth; t.classList.add('flash');
}));
