// Citadel online demo: runs before anything else on the page and turns the dashboard into a frozen, read-only snapshot.
// Written by tools/build_demo.py (the snapshot time below is filled in by the build). Nothing here talks to a server:
//  - the clock starts at the moment the snapshot was taken and ticks on from there, so "tomorrow", bin days and
//    countdowns read the way they did that day, however long ago it was built;
//  - /api/* GETs are answered from the saved JSON in data/ (anything not saved says so), every change is refused;
//  - the event stream is a stub that "connects" and then never says anything changed;
//  - the website's ?theme= picks the first theme, and Settings links say they aren't part of the demo.
(function () {
  'use strict';
  var SNAPSHOT = __SNAPSHOT_MS__;
  var NOT_HERE = 'Not available in the online demo';
  var READ_ONLY = 'This is a read-only demo — nothing is saved';
  var DATA = new URL('data/', document.currentScript.src).href;

  // ---- the clock ----------------------------------------------------------------------------------------------------
  var RealDate = Date;
  var offset = SNAPSHOT - RealDate.now();
  class DemoDate extends RealDate {
    constructor(...args) { if (args.length) super(...args); else super(RealDate.now() + offset); }
    static now() { return RealDate.now() + offset; }
  }
  window.Date = DemoDate;

  // ---- the theme the website opened us in (?theme=dark|light) -------------------------------------------------------
  try {
    var t = new URLSearchParams(location.search).get('theme');
    if (t === 'dark' || t === 'light') { localStorage.setItem('theme', t); localStorage.removeItem('theme-spec'); }
  } catch (e) { /* storage blocked: the page just starts dark */ }

  // ---- the API, from saved files ------------------------------------------------------------------------------------
  // The file name rule must match key_of() in tools/build_demo.py.
  function keyOf(url) {
    var rest = url.pathname.replace(/^\/api\/owner/, '') + url.search;
    return rest.replace(/[^A-Za-z0-9]+/g, '_').replace(/^_+|_+$/g, '') || 'index';
  }
  function reply(status, body) {
    return Promise.resolve(new Response(JSON.stringify(body), { status: status, headers: { 'Content-Type': 'application/json' } }));
  }
  var realFetch = window.fetch.bind(window);
  var cache = new Map();
  var index = null;                  // the keys the build saved (data/index.json)
  function saved(key) {
    if (!index) index = realFetch(DATA + 'index.json').then(function (r) { return r.ok ? r.json() : []; }).catch(function () { return []; });
    return index.then(function (keys) { return keys.indexOf(key) < 0 ? null : load(key); });
  }
  function load(key) {
    if (!cache.has(key)) {
      cache.set(key, realFetch(DATA + key + '.json').then(function (r) { return r.ok ? r.text() : null; }).catch(function () { return null; }));
    }
    return cache.get(key);
  }
  // The Monitoring tile polls every couple of seconds: play back the readings taken during the build, forwards then
  // backwards, so the numbers move without jumping when the loop comes round.
  var frame = 0;
  function monitoring() {
    return saved('monitoring_frames').then(function (text) {
      var frames = text ? JSON.parse(text) : [];
      if (!frames.length) return reply(503, { detail: NOT_HERE });
      var n = frames.length, cycle = Math.max(1, 2 * n - 2), i = frame++ % cycle;
      return reply(200, frames[i < n ? i : cycle - i]);
    });
  }
  window.fetch = function (input, init) {
    var url;
    try { url = new URL(typeof input === 'string' ? input : input.url, location.href); } catch (e) { return realFetch(input, init); }
    if (url.origin !== location.origin || url.pathname.indexOf('/api/') !== 0) return realFetch(input, init);
    var method = String((init && init.method) || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
    // 409, not 403: Citadel's api.js reloads the page on a 403 (it means "this device was revoked").
    if (method !== 'GET') return reply(409, { detail: READ_ONLY });
    var key = keyOf(url);
    if (key === 'monitoring') return monitoring();
    return saved(key).then(function (text) {
      return text === null ? reply(503, { detail: NOT_HERE })
        : new Response(text, { status: 200, headers: { 'Content-Type': 'application/json' } });
    });
  };

  // ---- the event stream: connects, then stays quiet -----------------------------------------------------------------
  function QuietEventSource(url) {
    var self = this;
    this.url = String(url);
    this.readyState = 0;
    this.onopen = this.onerror = this.onmessage = null;
    setTimeout(function () {
      if (self.readyState === 2) return;
      self.readyState = 1;
      if (typeof self.onopen === 'function') self.onopen(new Event('open'));
    }, 0);
  }
  QuietEventSource.CONNECTING = 0; QuietEventSource.OPEN = 1; QuietEventSource.CLOSED = 2;
  QuietEventSource.prototype.addEventListener = function () {};
  QuietEventSource.prototype.removeEventListener = function () {};
  QuietEventSource.prototype.close = function () { this.readyState = 2; };
  window.EventSource = QuietEventSource;

  // ---- links that lead out of the demo ------------------------------------------------------------------------------
  var toastTimer = 0;
  function toast(message) {          // the same toast element and style as Citadel's own (util.js)
    var el = document.getElementById('toast');
    if (!el) { el = document.createElement('div'); el.id = 'toast'; el.className = 'toast'; el.setAttribute('role', 'status'); document.body.append(el); }
    el.textContent = message;
    el.classList.add('is-on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.classList.remove('is-on'); }, 5000);
  }
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[href]');
    if (!a) return;
    var href = a.getAttribute('href');
    // Settings, and the Launchpad tiles of apps that only exist on the home network (their address was removed: "#").
    if (href.indexOf('/settings') === 0) { e.preventDefault(); e.stopPropagation(); toast('Settings aren’t available in the online demo'); }
    else if (href === '#' && a.target === '_blank') { e.preventDefault(); e.stopPropagation(); toast('That app runs on my home network only — ' + NOT_HERE.toLowerCase()); }
  }, true);
})();
