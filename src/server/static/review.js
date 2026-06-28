// Review panel logic shared by article.html and question.html.
// Requires _currentUserEmail to be set as a global before this script is loaded.
(function () {
  'use strict';

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------

  function _getVersion(segId) {
    var p = document.getElementById('review-' + segId);
    return p ? parseInt(p.dataset.humanVersion || '0', 10) : 0;
  }

  function _setVersion(segId, v) {
    var p = document.getElementById('review-' + segId);
    if (p) p.dataset.humanVersion = String(v);
  }

  function _showTab(segId, tab) {
    var mp    = document.getElementById('rpane-machine-' + segId);
    var hp    = document.getElementById('rpane-human-' + segId);
    var panel = document.getElementById('review-' + segId);
    if (!panel) return;
    panel.querySelectorAll('.tab-btn').forEach(function (b) {
      b.classList.toggle('tab-active', b.dataset.tab === tab);
    });
    if (mp) mp.style.display = tab === 'machine' ? '' : 'none';
    if (hp) hp.style.display = tab === 'human'   ? '' : 'none';
  }

  function _showSlovakDisplay(segId, show) {
    var d = document.getElementById('sdisp-' + segId);
    if (d) d.style.display = show ? '' : 'none';
  }

  function _autoResize(ta) {
    ta.style.height = 'auto';
    ta.style.height = ta.scrollHeight + 'px';
  }

  function _updateDisplayText(segId, text) {
    var span = document.getElementById('text-'     + segId);
    var em   = document.getElementById('awaiting-' + segId);
    if (text) {
      if (span) { span.textContent = text; span.style.display = ''; }
      if (em)   em.style.display = 'none';
    } else {
      if (span) span.style.display = 'none';
      if (em)   em.style.display = '';
    }
  }

  function _updateNoteDisplay(segId, note) {
    var el = document.getElementById('note-display-' + segId);
    if (!el) return;
    if (note) { el.textContent = note; el.style.display = ''; }
    else      { el.textContent = ''; el.style.display = 'none'; }
  }

  function _updateHumanBadge(segId, reviewed) {
    var btn = document.querySelector('.btn-review[data-segment-id="' + segId + '"]');
    if (!btn) return;
    if (reviewed) {
      btn.classList.add('btn-review-done');
      btn.innerHTML = '&#10003; Reviewed';
      btn.title = 'Reviewed by ' + _currentUserEmail;
    } else {
      btn.classList.remove('btn-review-done');
      btn.innerHTML = '&#9998; Review';
      btn.title = '';
    }
  }

  function _setClearNoteEnabled(segId, enabled) {
    var btn = document.querySelector('.btn-rev-clearnote[data-segment-id="' + segId + '"]');
    if (btn) btn.disabled = !enabled;
  }

  function _closePanel(segId) {
    var panel = document.getElementById('review-' + segId);
    if (panel) panel.style.display = 'none';
    _showSlovakDisplay(segId, true);
  }

  function _doAction(segId, action, extra) {
    var body = Object.assign(
      { action: action, expected_version: _getVersion(segId) },
      extra || {}
    );
    return fetch('/api/segment/' + segId + '/review', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(function (resp) {
      return resp.json().then(function (data) {
        return { status: resp.status, data: data };
      });
    });
  }

  function _handleResult(segId, result, onOk) {
    if (result.status === 200 && result.data.ok) {
      _setVersion(segId, result.data.human_version);
      if (onOk) onOk(result.data);
    } else if (result.status === 409) {
      alert('This segment was changed by another editor — please reload the page.');
    } else {
      alert('Action failed: ' + ((result.data && result.data.error) || 'server error'));
    }
  }

  // ---------------------------------------------------------------------------
  // Event handlers
  // ---------------------------------------------------------------------------

  // Open / close review panel
  document.querySelectorAll('.btn-review').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var segId = btn.dataset.segmentId;
      var panel = document.getElementById('review-' + segId);
      if (!panel) return;
      var open = panel.style.display !== 'none';
      if (open) {
        _closePanel(segId);
      } else {
        panel.style.display = '';
        _showTab(segId, 'human');
        _showSlovakDisplay(segId, false);
        var ta = document.getElementById('htextarea-' + segId);
        if (ta) _autoResize(ta);
      }
    });
  });

  // Tab switching
  document.querySelectorAll('.tab-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      _showTab(btn.dataset.segmentId, btn.dataset.tab);
    });
  });

  // Auto-resize human textareas on input
  document.querySelectorAll('.human-textarea').forEach(function (ta) {
    ta.addEventListener('input', function () { _autoResize(ta); });
  });

  // Cancel — close panel and restore displayed text
  document.querySelectorAll('.btn-rev-cancel').forEach(function (btn) {
    btn.addEventListener('click', function () {
      _closePanel(btn.dataset.segmentId);
    });
  });

  // Add Note toggle
  document.querySelectorAll('.btn-rev-addnote').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var a = document.getElementById('notearea-' + btn.dataset.segmentId);
      if (a) a.style.display = a.style.display === 'none' ? '' : 'none';
    });
  });

  // Accept — saves human text if present, else records acceptance; closes panel
  document.querySelectorAll('.btn-rev-accept').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var segId  = btn.dataset.segmentId;
      var ta     = document.getElementById('htextarea-' + segId);
      var text   = ta ? ta.value.trim() : '';
      var action = text ? 'save' : 'accept';
      var extra  = text ? { text: text } : {};
      btn.disabled = true; btn.textContent = 'Saving…';
      _doAction(segId, action, extra)
        .then(function (result) {
          _handleResult(segId, result, function () {
            if (text) _updateDisplayText(segId, text);
            _updateHumanBadge(segId, true);
            _closePanel(segId);
          });
          btn.disabled = false; btn.textContent = 'Accept';
        })
        .catch(function () {
          alert('Accept failed — server error.');
          btn.disabled = false; btn.textContent = 'Accept';
        });
    });
  });

  // Reset — removes human text, note, and review row; closes panel
  document.querySelectorAll('.btn-rev-reset').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var segId = btn.dataset.segmentId;
      if (!confirm('Remove the human translation and note for this segment?')) return;
      btn.disabled = true; btn.textContent = 'Resetting…';
      _doAction(segId, 'reset')
        .then(function (result) {
          _handleResult(segId, result, function () {
            var mpane = document.getElementById('rpane-machine-' + segId);
            var mtEl  = mpane ? mpane.querySelector('.machine-text-ro') : null;
            var mt    = mtEl ? mtEl.textContent.trim() : '';
            var machineText = (mt && mt !== '— no machine translation —') ? mt : '';
            _updateDisplayText(segId, machineText);
            var ta = document.getElementById('htextarea-' + segId);
            if (ta) { ta.value = machineText; _autoResize(ta); }
            _updateNoteDisplay(segId, '');
            _updateHumanBadge(segId, false);
            var nta = document.getElementById('ntextarea-' + segId);
            if (nta) nta.value = '';
            _setClearNoteEnabled(segId, false);
            _closePanel(segId);
          });
          btn.disabled = false; btn.textContent = 'Reset';
        })
        .catch(function () {
          alert('Reset failed — server error.');
          btn.disabled = false; btn.textContent = 'Reset';
        });
    });
  });

  // Save Note
  document.querySelectorAll('.btn-rev-savenote').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var segId = btn.dataset.segmentId;
      var nta   = document.getElementById('ntextarea-' + segId);
      var note  = nta ? nta.value.trim() : '';
      btn.disabled = true; btn.textContent = 'Saving…';
      _doAction(segId, 'note', { note: note })
        .then(function (result) {
          _handleResult(segId, result, function () {
            _updateNoteDisplay(segId, note);
            _updateHumanBadge(segId, true);
            _setClearNoteEnabled(segId, !!note);
          });
          btn.disabled = false; btn.textContent = 'Save Note';
        })
        .catch(function () {
          alert('Save Note failed — server error.');
          btn.disabled = false; btn.textContent = 'Save Note';
        });
    });
  });

  // Clear Note — sends empty note to backend (clears human_note to NULL)
  document.querySelectorAll('.btn-rev-clearnote').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var segId = btn.dataset.segmentId;
      btn.disabled = true; btn.textContent = 'Clearing…';
      _doAction(segId, 'note', { note: '' })
        .then(function (result) {
          _handleResult(segId, result, function () {
            var nta = document.getElementById('ntextarea-' + segId);
            if (nta) nta.value = '';
            _updateNoteDisplay(segId, '');
            _setClearNoteEnabled(segId, false);
          });
          btn.disabled = false; btn.textContent = 'Clear Note';
        })
        .catch(function () {
          alert('Clear Note failed — server error.');
          btn.disabled = false; btn.textContent = 'Clear Note';
        });
    });
  });

  // ---------------------------------------------------------------------------
  // Polish helpers
  // ---------------------------------------------------------------------------

  function _doPolish(segId) {
    return fetch('/api/segment/' + segId + '/polish', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    }).then(function (resp) {
      return resp.json().then(function (data) {
        return { status: resp.status, data: data };
      });
    });
  }

  function _updatePolishDisplay(segId, polishedText, guardFlags, flipped) {
    // Show polished text in the machine pane
    var polishSection = document.getElementById('polish-draft-' + segId);
    var polishText    = document.getElementById('polish-text-'  + segId);
    if (polishSection) polishSection.style.display = '';
    if (polishText)    polishText.textContent = polishedText;

    // Show guard info
    var guardEl = document.getElementById('polish-guard-' + segId);
    if (guardEl && guardFlags) {
      var ok = guardFlags.ok;
      guardEl.textContent = ok
        ? '✓ Guards: ok'
        : '⚠ Guards: ' + JSON.stringify(guardFlags);
      guardEl.style.display = '';
    }

    // If the segment was needs_human and we flipped it, update badge + row class
    if (flipped) {
      var badge = document.querySelector('[data-badge="' + segId + '"]');
      if (badge) {
        badge.className = badge.className.replace(/badge-warn\b/, 'badge-ok');
        badge.innerHTML = '&#10003;';
        badge.title = 'translated';
      }
      var row = document.querySelector('tr[data-segment-id="' + segId + '"]');
      if (row) row.classList.remove('row-needs-human');
      // Hide the "Accept + Polish" button (segment no longer needs_human)
      var acceptBtn = document.querySelector('.btn-accept-polish[data-segment-id="' + segId + '"]');
      if (acceptBtn) acceptBtn.style.display = 'none';
    }

    // Update the main display text to show polished version (human takes precedence, but if
    // there's no human text the display should show the polished version)
    var humanText = document.getElementById('htextarea-' + segId);
    var hasHuman  = humanText && humanText.value.trim() !== '';
    if (!hasHuman) {
      _updateDisplayText(segId, polishedText);
    }
  }

  // Accept + Polish — for needs_human segments: polishes draft and flips status
  document.querySelectorAll('.btn-accept-polish').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var segId = btn.dataset.segmentId;
      btn.disabled = true; btn.textContent = 'Polishing…';
      _doPolish(segId)
        .then(function (result) {
          if (result.status === 200 && result.data.ok) {
            _updatePolishDisplay(segId, result.data.polished_text,
                                 result.data.guard_flags, result.data.flipped);
            _closePanel(segId);
          } else {
            alert('Polish failed: ' + ((result.data && result.data.error) || 'server error'));
            btn.disabled = false; btn.textContent = 'Accept + Polish';
          }
        })
        .catch(function () {
          alert('Accept + Polish failed — server error.');
          btn.disabled = false; btn.textContent = 'Accept + Polish';
        });
    });
  });

  // Re-polish — for translated segments: updates polish text without changing status
  document.querySelectorAll('.btn-repolish').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var segId = btn.dataset.segmentId;
      var label = btn.textContent.trim();
      btn.disabled = true; btn.textContent = 'Polishing…';
      _doPolish(segId)
        .then(function (result) {
          if (result.status === 200 && result.data.ok) {
            _updatePolishDisplay(segId, result.data.polished_text,
                                 result.data.guard_flags, result.data.flipped);
          } else {
            alert('Re-polish failed: ' + ((result.data && result.data.error) || 'server error'));
          }
          btn.disabled = false; btn.textContent = label;
        })
        .catch(function () {
          alert('Re-polish failed — server error.');
          btn.disabled = false; btn.textContent = label;
        });
    });
  });

}());
