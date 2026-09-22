// Card chrome and behaviour: builds each card, keeps its content fresh without flicker, and handles the
// expand-to-full-view interaction (View Transitions morph, Esc / backdrop / button to close).
import { $, $$, esc, ico } from './util.js';

const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;

export class Shell {
  /** @param defs card definitions; @param renderCard(id) asks the app to re-render one card */
  constructor(grid, defs, renderCard) {
    this.grid = grid;
    this.renderCard = renderCard;
    this.cards = new Map();
    this.open = null;          // the expanded card element, if any
    this.placeholder = null;   // keeps its slot in the grid while it is expanded
    defs.forEach(def => this.#build(def));
    this.#bind();
  }

  #build(def) {
    const el = document.createElement('section');
    el.className = `card a-${def.area}${def.cls ? ` ${def.cls}` : ''}`;
    el.id = def.id;
    const first = def.defaultFilter ?? def.filters?.[0]?.value;
    const chips = (def.filters || []).map(f =>
      `<button class="fchip" data-filter="${esc(f.value)}" aria-pressed="${f.value === first}">${esc(f.label)}</button>`).join('');
    const seen = def.markSeen ? `<button class="fchip fchip--action" data-mark-seen="${esc(def.markSeen)}">Mark all seen</button>` : '';
    const search = (seen ? seen : '') + (def.search ? '<input class="search" type="search" placeholder="Filter…" aria-label="Filter">' : '');
    // A small, always-visible shortcut next to Expand — e.g. Next gig's "Add a gig" — so a card with things in it
    // already isn't only reachable through Settings' empty-state link.
    const headerAction = def.headerAction
      ? `<a class="btn btn--icon" href="${esc(def.headerAction.href)}" aria-label="${esc(def.headerAction.label)}" title="${esc(def.headerAction.label)}">${ico(def.headerAction.icon)}</a>` : '';
    // pinnedOpen: a card mounted permanently full-detail (e.g. the News page) — no expand button (there's
    // nothing to expand into), and its filters/search start visible instead of hidden-until-expanded.
    const maxBtn = def.pinnedOpen ? '' : `<button class="btn btn--icon" data-max aria-label="Expand" aria-expanded="false">${ico('expand')}</button>`;
    el.innerHTML = `<div class="card__in">
      <div class="card__head"><span class="hud">${esc(def.label)}</span><span class="card__tools"><span class="meta"></span>${headerAction}
        ${maxBtn}</span></div>
      ${chips || search ? `<div class="maxbar"${def.pinnedOpen ? '' : ' hidden'}>${chips}${search}</div>` : ''}
      <div class="card__body${def.flush ? ' card__body--flush' : ''}"></div></div>`;
    this.grid.append(el);
    this.cards.set(def.id, { el, def, meta: $('.meta', el), body: $('.card__body', el), last: null,
      filter: first ?? null, search: '' });
  }

  /** What a card's render function needs to know about its own UI state. */
  ctx(id) {
    const c = this.cards.get(id);
    return { expanded: c.def.pinnedOpen || c.el.classList.contains('is-max'), filter: c.filter, search: c.search };
  }

  /** Replace a card's content — only touching the DOM if something actually changed. */
  update(id, { meta = '', body = '' }) {
    const c = this.cards.get(id);
    const key = `${meta}\u0000${body}`;
    if (key === c.last) return;
    c.last = key;
    const top = c.body.scrollTop;
    c.meta.innerHTML = meta;
    c.body.innerHTML = body;
    const on = c.def.pinnedOpen || c.el.classList.contains('is-max');
    $$('.more', c.body).forEach(e => { e.hidden = !on; });     // expanded-only content
    c.body.scrollTop = top;
    this.clip();
  }

  // ---- expand / collapse -----------------------------------------------------------------------

  #apply(card, on) {
    const c = this.cards.get(card.id);
    if (on) {
      this.placeholder = card.cloneNode(false);                 // holds the grid cell (same classes -> same area)
      this.placeholder.classList.add('ph');
      this.placeholder.removeAttribute('id');
      this.placeholder.style.viewTransitionName = '';
      this.placeholder.style.height = `${card.offsetHeight}px`;
      card.after(this.placeholder);
      card.classList.add('is-max');
      document.body.classList.add('has-max');
    } else {
      card.classList.remove('is-max');
      document.body.classList.remove('has-max');
      this.placeholder?.remove();
      this.placeholder = null;
    }
    $$('.more, .maxbar', card).forEach(e => { e.hidden = !on; });
    const btn = $('[data-max]', card);
    btn.setAttribute('aria-expanded', on);
    btn.setAttribute('aria-label', on ? 'Collapse' : 'Expand');
    btn.innerHTML = ico(on ? 'shrink' : 'expand');
    c.body.scrollTop = 0;
    this.open = on ? card : null;
    this.renderCard(card.id);                                   // some cards render differently when expanded
    this.clip();
    document.dispatchEvent(new CustomEvent('card:max', { detail: { id: card.id, on } }));
  }

  toggle(card) {
    const on = !card.classList.contains('is-max');
    if (on && this.open) return;
    if (document.startViewTransition && !reduced) {
      card.style.viewTransitionName = 'maxcard';
      document.startViewTransition(() => this.#apply(card, on)).finished.finally(() => { card.style.viewTransitionName = ''; });
    } else {
      this.#apply(card, on);
    }
  }

  // ---- events ---------------------------------------------------------------------------------

  #bind() {
    document.addEventListener('click', e => {
      if (this.open && !e.target.closest('.is-max')) { this.toggle(this.open); return; }       // backdrop click
      const card = e.target.closest('.card');
      const c = card && this.cards.get(card.id);
      if (!c) return;

      const chip = e.target.closest('.fchip:not(.fchip--action)');
      if (chip) {
        $$('.fchip', chip.parentElement).forEach(x => x.setAttribute('aria-pressed', x === chip));
        c.filter = chip.dataset.filter;
        this.renderCard(card.id);
        return;
      }
      if (e.target.closest('a, input, button:not([data-max]), [data-act], [data-select]')) return;
      if (!c.def.pinnedOpen && (e.target.closest('[data-max]') || e.target.closest('.card__head'))) this.toggle(card);
    });

    let timer;
    document.addEventListener('input', e => {
      const box = e.target.closest('.search');
      const c = box && this.cards.get(box.closest('.card')?.id);
      if (!c) return;
      clearTimeout(timer);
      timer = setTimeout(() => { c.search = box.value.trim().toLowerCase(); this.renderCard(c.el.id); }, 120);
    });

    document.addEventListener('keydown', e => { if (e.key === 'Escape' && this.open) this.toggle(this.open); });

    for (const c of this.cards.values()) c.body.addEventListener('scroll', () => this.clip(), { passive: true });
    addEventListener('resize', () => this.clip());
    document.fonts?.ready.then(() => this.clip());
  }

  /** Fade the bottom edge of any card whose content is clipped. */
  clip() {
    for (const { body } of this.cards.values()) {
      body.classList.toggle('is-clipped', body.scrollHeight > body.clientHeight + 2);
      body.classList.toggle('at-end', body.scrollTop + body.clientHeight >= body.scrollHeight - 2);
    }
  }
}
