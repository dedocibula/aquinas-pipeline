// Shared parallel-text viewer chrome: reference-language switcher and
// detail-panel toggles. Used by article.html and question.html.
(function () {
  'use strict';

  var sel = document.getElementById('ref-lang-select');
  if (sel) {
    sel.addEventListener('change', function () {
      var lang = this.value;
      document.querySelectorAll('.ref-text').forEach(function (el) {
        el.style.display = el.dataset.lang === lang ? '' : 'none';
      });
    });
  }

  document.querySelectorAll('.detail-toggle').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var targetId = btn.dataset.target;
      var panel    = document.getElementById(targetId);
      if (!panel) return;
      var open = panel.style.display !== 'none';
      if (open) {
        panel.style.display = 'none';
        btn.setAttribute('aria-expanded', 'false');
        btn.innerHTML = '&#9654; details';
      } else {
        panel.style.display = '';
        btn.setAttribute('aria-expanded', 'true');
        btn.innerHTML = '&#9660; details';
      }
    });

    // Sync initial button label for panels that start open (needs_human)
    var targetId = btn.dataset.target;
    var panel    = document.getElementById(targetId);
    if (panel && panel.style.display !== 'none') {
      btn.innerHTML = '&#9660; details';
    }
  });

}());
