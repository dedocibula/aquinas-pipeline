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

  function _updateDisplayText(segId, text) {
    var span = document.getElementById('text-'     + segId);
    var em   = document.getElementById('awaiting-' + segId);
    if (text) {
      if (span) { span.innerHTML = AQ.renderMarkup(text); span.style.display = ''; }
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

  // Wraps (or unwraps, toggling off) the current selection within editorDiv in <tagName>.
  function _wrapSelection(editorDiv, tagName) {
    var sel = window.getSelection();
    if (!sel.rangeCount) return;
    var range = sel.getRangeAt(0);
    if (!editorDiv.contains(range.commonAncestorContainer)) return;
    if (range.collapsed) return; // nothing selected to wrap or unwrap

    // Toggle off: if an ancestor of the selection is already <tagName>, unwrap it.
    var node = range.commonAncestorContainer;
    if (node.nodeType === Node.TEXT_NODE) node = node.parentNode;
    var existing = node.closest ? node.closest(tagName) : null;
    if (existing && editorDiv.contains(existing)) {
      var parent     = existing.parentNode;
      var firstChild = existing.firstChild;
      var lastChild  = existing.lastChild;
      while (existing.firstChild) parent.insertBefore(existing.firstChild, existing);
      parent.removeChild(existing);
      if (firstChild && lastChild) {
        var unwrapRange = document.createRange();
        unwrapRange.setStartBefore(firstChild);
        unwrapRange.setEndAfter(lastChild);
        sel.removeAllRanges();
        sel.addRange(unwrapRange);
      }
      editorDiv.focus();
      return;
    }

    var wrapper = document.createElement(tagName);
    try {
      range.surroundContents(wrapper);
    } catch (e) {
      // Selection spans multiple sibling nodes or partially overlaps existing
      // formatting — flatten to plain text before re-wrapping. Nesting isn't
      // supported (see plan), so this avoids producing <b><i>..</i></b>-style
      // markup that AQ.renderMarkup can't parse back on the next load.
      wrapper.textContent = range.extractContents().textContent;
      range.insertNode(wrapper);
    }

    var newRange = document.createRange();
    newRange.selectNodeContents(wrapper);
    sel.removeAllRanges();
    sel.addRange(newRange);
    editorDiv.focus();
  }

  function _doAction(segId, action, extra) {
    var body = Object.assign(
      { action: action, expected_version: _getVersion(segId) },
      extra || {}
    );
    return AQ.postJson('/api/segment/' + segId + '/review', body);
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
        // needs_human segments open on the machine tab so "Accept + Polish" is immediately visible
        var defaultTab = (panel.dataset.needsHuman === '1') ? 'machine' : 'human';
        _showTab(segId, defaultTab);
        _showSlovakDisplay(segId, false);
      }
    });
  });

  // Tab switching
  document.querySelectorAll('.tab-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      _showTab(btn.dataset.segmentId, btn.dataset.tab);
    });
  });

  // Render already-translated segments' Slovak text and human editor content as markup on load
  document.querySelectorAll('.slovak-text').forEach(function (span) {
    if (span.textContent) span.innerHTML = AQ.renderMarkup(span.textContent);
  });
  document.querySelectorAll('.human-editor').forEach(function (div) {
    div.innerHTML = AQ.renderMarkup(div.textContent);
  });

  // Formatting toolbar — bold / italic / underline
  function _bindFormatButton(selector, tagName) {
    document.querySelectorAll(selector).forEach(function (btn) {
      btn.addEventListener('click', function () {
        var editor = document.getElementById('heditor-' + btn.dataset.segmentId);
        if (editor) _wrapSelection(editor, tagName);
      });
    });
  }
  _bindFormatButton('.btn-fmt-bold', 'b');
  _bindFormatButton('.btn-fmt-italic', 'i');
  _bindFormatButton('.btn-fmt-underline', 'u');

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
      var editor = document.getElementById('heditor-' + segId);
      var text   = editor ? AQ.markupToMarkdown(editor).trim() : '';
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
            var editor = document.getElementById('heditor-' + segId);
            if (editor) editor.innerHTML = AQ.renderMarkup(machineText);
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
  // Approve / Un-Approve — needs_human segments
  // ---------------------------------------------------------------------------

  document.querySelectorAll('.btn-approve').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var segId    = btn.dataset.segmentId;
      var approved = btn.classList.contains('btn-approved');
      var url      = '/api/segment/' + segId + (approved ? '/unapprove' : '/approve');
      btn.disabled = true;
      btn.textContent = approved ? 'Reverting…' : 'Approving…';

      AQ.postJson(url, {})
        .then(function (result) {
          if (result.status === 200 && result.data.ok) {
            if (!approved) {
              // Approved: flip badge, strip needs-human row class, change to Un-Approve
              var badge = document.querySelector('[data-badge="' + segId + '"]');
              if (badge) {
                badge.className = badge.className.replace(/badge-warn\b/, 'badge-ok');
                badge.textContent = '✓';
                badge.title = 'translated';
              }
              var row = document.querySelector('tr[data-segment-id="' + segId + '"]');
              if (row) row.classList.remove('row-needs-human');
              btn.classList.add('btn-approved');
              btn.textContent = 'Un-Approve';
            } else {
              // Un-approved: restore needs-human state
              var badge = document.querySelector('[data-badge="' + segId + '"]');
              if (badge) {
                badge.className = badge.className.replace(/badge-ok\b/, 'badge-warn');
                badge.textContent = '⚠';
                badge.title = 'needs review';
              }
              var row = document.querySelector('tr[data-segment-id="' + segId + '"]');
              if (row) row.classList.add('row-needs-human');
              btn.classList.remove('btn-approved');
              btn.textContent = 'Approve';
            }
          } else {
            var err = (result.data && result.data.error) || 'server error';
            alert((approved ? 'Un-Approve' : 'Approve') + ' failed: ' + err);
            btn.textContent = approved ? 'Un-Approve' : 'Approve';
          }
          btn.disabled = false;
        })
        .catch(function () {
          alert((approved ? 'Un-Approve' : 'Approve') + ' failed — server error.');
          btn.disabled = false;
          btn.textContent = approved ? 'Un-Approve' : 'Approve';
        });
    });
  });

}());
