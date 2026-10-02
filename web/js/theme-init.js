// Runs before first paint: puts this device's theme on the page straight away (see theme.js for the full story).
try {
  var t = localStorage.getItem('theme');
  if (t === 'light') document.documentElement.dataset.theme = 'light';
  else if (t && t.indexOf('c:') === 0) {
    var s = JSON.parse(localStorage.getItem('theme-spec') || 'null'), r = document.documentElement;
    if (s) {
      if (s.base === 'light') r.dataset.theme = 'light';
      for (var k in s.vars) r.style.setProperty(k, s.vars[k]);
      if (s.bg) r.classList.add('has-bg');
      if (!s.glow) r.classList.add('no-glow');
    }
  }
} catch (e) {}
