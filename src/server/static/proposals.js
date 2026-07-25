// Editor glossary-proposal UI: per-term change/remove actions and the
// missing-term form. Shared by article.html and question.html.
(function () {
  'use strict';

  // ---------------------------------------------------------------------------
  // Proposal kinds (glossary_proposal.kind CHECK constraint, migration 013).
  // The repo is deliberately build-free, so this JS cannot import Python —
  // this is the one permitted mirror of server/db.py's SENSE_WIDE_KINDS /
  // PER_SEGMENT_KINDS (and their underlying PROPOSAL_KIND_* strings). Keep
  // in sync by hand; do not add other Python-side duplicates elsewhere.
  // ---------------------------------------------------------------------------

  var KIND_CHANGE_EVERYWHERE = 'rendering';
  var KIND_WRONG_SENSE_HERE = 'sense_here';
  var KIND_REMOVE_HERE = 'remove_here';
  var KIND_RETIRE_EVERYWHERE = 'retire_sense';

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------

  function _setPendingBadge(senseId, on) {
    var badge = document.querySelector(
      '.term-pending[data-pending-for="' + senseId + '"]'
    );
    if (badge) badge.style.display = on ? '' : 'none';
  }

  function _scope(form) {
    var checked = form.querySelector('input[type="radio"]:checked');
    return checked ? checked.value : 'here';
  }

  function _resetForm(form) {
    var rendering = form.querySelector('.tpf-rendering');
    rendering.value = '';
    rendering.dataset.mode = '';
    form.querySelector('.tpf-note').value = '';
    var select = form.querySelector('.tpf-sense-select');
    select.value = '';
    select.style.display = 'none';
    rendering.style.display = '';
    form.querySelector('.tpf-remove-hint').style.display = 'none';
    form.querySelector('.tpf-status').textContent = '';
  }

  function _syncFormFields(form) {
    var act = form.dataset.act;
    var scope = _scope(form);
    var select = form.querySelector('.tpf-sense-select');
    var rendering = form.querySelector('.tpf-rendering');
    var removeHint = form.querySelector('.tpf-remove-hint');

    var showSelect = act === 'change' && scope === 'here';
    select.style.display = showSelect ? '' : 'none';
    rendering.style.display =
      act === 'change' && (scope === 'everywhere' || select.value === '__other__') ? '' : 'none';
    removeHint.style.display = act === 'remove' && scope === 'everywhere' ? '' : 'none';

    // The rendering field means two different things depending on scope:
    // "everywhere" -> the new winning Slovak rendering (prefill with current).
    // "here" + other -> a free-text sense suggestion (must start blank, else
    // an untouched click would silently submit the current rendering as a
    // "different sense" suggestion).
    if (act === 'change' && scope === 'everywhere') {
      if (rendering.dataset.mode !== 'rendering') {
        rendering.value = form.dataset.currentSk || '';
        rendering.dataset.mode = 'rendering';
      }
    } else if (rendering.dataset.mode === 'rendering') {
      rendering.value = '';
      rendering.dataset.mode = '';
    }

    if (showSelect && !select.dataset.loaded) {
      select.dataset.loaded = '1';
      fetch('/api/sense/' + form.dataset.senseId + '/alternatives')
        .then(function (resp) { return resp.json(); })
        .then(function (data) {
          if (!data.ok) return;
          data.senses
            .filter(function (s) { return String(s.sense_id) !== form.dataset.senseId; })
            .forEach(function (s) {
              var opt = document.createElement('option');
              opt.value = s.sense_id;
              opt.textContent = (s.slovak || '—') + (s.context_label ? ' (' + s.context_label + ')' : '');
              select.insertBefore(opt, select.querySelector('option[value="__other__"]'));
            });
        });
    }
  }

  // ---------------------------------------------------------------------------
  // Per-term change / remove buttons
  // ---------------------------------------------------------------------------

  document.querySelectorAll('.btn-term-act').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var segId = btn.dataset.segmentId;
      var form = document.getElementById('tpform-' + segId);
      if (!form) return;

      _resetForm(form);
      form.dataset.act = btn.dataset.act;
      form.dataset.senseId = btn.dataset.senseId;
      form.dataset.lemma = btn.dataset.lemma;
      form.dataset.currentSk = btn.dataset.currentSk || '';
      form.querySelector('.tpf-target').textContent =
        (btn.dataset.act === 'change' ? 'Change: ' : 'Remove: ') + btn.dataset.lemma;
      var select = form.querySelector('.tpf-sense-select');
      select.dataset.loaded = '';
      while (select.options.length > 2) select.remove(1);

      _syncFormFields(form);
      form.style.display = '';
      btn.closest('li').appendChild(form);
    });
  });

  document.querySelectorAll('.term-propose-form').forEach(function (form) {
    form.querySelectorAll('input[type="radio"]').forEach(function (radio) {
      radio.addEventListener('change', function () { _syncFormFields(form); });
    });
    form.querySelector('.tpf-sense-select').addEventListener('change', function () {
      _syncFormFields(form);
    });

    form.querySelector('.tpf-cancel').addEventListener('click', function () {
      form.style.display = 'none';
    });

    form.querySelector('.tpf-submit').addEventListener('click', function () {
      var act = form.dataset.act;
      var senseId = form.dataset.senseId;
      var scope = _scope(form);
      var rendering = form.querySelector('.tpf-rendering').value.trim();
      var note = form.querySelector('.tpf-note').value.trim();
      var select = form.querySelector('.tpf-sense-select');
      var status = form.querySelector('.tpf-status');

      var kind, body;
      if (act === 'change' && scope === 'everywhere') {
        kind = KIND_CHANGE_EVERYWHERE;
        body = { kind: kind, proposed_sk: rendering, note: note };
      } else if (act === 'change' && scope === 'here') {
        kind = KIND_WRONG_SENSE_HERE;
        body = {
          kind: kind,
          origin_segment_id: parseInt(form.id.replace('tpform-', ''), 10),
          note: note,
        };
        if (select.value && select.value !== '__other__') {
          body.proposed_sense_id = parseInt(select.value, 10);
        } else {
          body.proposed_sk = rendering;
        }
      } else if (act === 'remove' && scope === 'here') {
        kind = KIND_REMOVE_HERE;
        body = {
          kind: kind,
          origin_segment_id: parseInt(form.id.replace('tpform-', ''), 10),
          note: note,
        };
      } else {
        kind = KIND_RETIRE_EVERYWHERE;
        if (!note) {
          status.textContent = 'A reason is required to remove a term everywhere.';
          return;
        }
        body = { kind: kind, note: note };
      }

      form.querySelector('.tpf-submit').disabled = true;
      status.textContent = 'Submitting…';

      AQ.postJson('/api/sense/' + senseId + '/propose', body)
        .then(function (result) {
          form.querySelector('.tpf-submit').disabled = false;
          if (result.status === 200 && result.data.ok) {
            status.textContent = 'Proposed — pending approval.';
            _setPendingBadge(senseId, true);
            setTimeout(function () { form.style.display = 'none'; }, 1200);
          } else {
            status.textContent = (result.data && result.data.error) || 'Submit failed.';
          }
        })
        .catch(function () {
          form.querySelector('.tpf-submit').disabled = false;
          status.textContent = 'Submit failed — server error.';
        });
    });
  });

  // ---------------------------------------------------------------------------
  // Missing-term form
  // ---------------------------------------------------------------------------

  document.querySelectorAll('.btn-toggle-term-proposal').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var form = document.getElementById('taform-' + btn.dataset.segmentId);
      if (form) form.style.display = form.style.display === 'none' ? '' : 'none';
    });
  });

  document.querySelectorAll('.taf-cancel').forEach(function (btn) {
    btn.addEventListener('click', function () {
      btn.closest('.term-add-form').style.display = 'none';
    });
  });

  document.querySelectorAll('.taf-submit').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var form = btn.closest('.term-add-form');
      var lemma = form.querySelector('.taf-lemma').value.trim();
      var rendering = form.querySelector('.taf-rendering').value.trim();
      var note = form.querySelector('.taf-note').value.trim();
      var status = form.querySelector('.taf-status');

      btn.disabled = true;
      status.textContent = 'Submitting…';

      AQ.postJson('/api/term-proposal', {
        latin_lemma: lemma,
        proposed_sk: rendering,
        note: note,
        origin_segment_id: parseInt(btn.dataset.segmentId, 10),
      })
        .then(function (result) {
          btn.disabled = false;
          if (result.status === 200 && result.data.ok) {
            status.textContent = 'Proposed — pending approval.';
            form.querySelector('.taf-lemma').value = '';
            form.querySelector('.taf-rendering').value = '';
            form.querySelector('.taf-note').value = '';
          } else {
            status.textContent = (result.data && result.data.error) || 'Submit failed.';
          }
        })
        .catch(function () {
          btn.disabled = false;
          status.textContent = 'Submit failed — server error.';
        });
    });
  });

  // ---------------------------------------------------------------------------
  // Admin proposal queue (Stage 4) — approve / reject
  // ---------------------------------------------------------------------------

  function _decideProposal(btn, action) {
    var row = btn.closest('tr');
    var proposalId = btn.dataset.proposalId;
    var note = row.querySelector('.decision-note').value.trim();
    var status = row.querySelector('.decision-status');
    var buttons = row.querySelectorAll('.btn-approve-proposal, .btn-reject-proposal');

    var body = { note: note };
    if (action === 'approve') {
      var skInput = row.querySelector('.proposed-sk-edit');
      if (skInput) {
        var edited = skInput.value.trim();
        if (!edited) {
          status.textContent = 'Proposed text cannot be empty.';
          return;
        }
        body.proposed_sk = edited;
      }
    }

    buttons.forEach(function (b) { b.disabled = true; });
    status.textContent = action === 'approve' ? 'Applying…' : 'Rejecting…';

    AQ.postJson('/api/proposal/' + proposalId + '/' + action, body)
      .then(function (result) {
        if (result.status === 200 && result.data.ok) {
          status.textContent = action === 'approve' ? 'Applied.' : 'Rejected.';
          row.classList.add('proposal-decided');
          setTimeout(function () { row.remove(); }, 1200);
        } else {
          buttons.forEach(function (b) { b.disabled = false; });
          status.textContent = (result.data && result.data.error) || (action + ' failed.');
        }
      })
      .catch(function () {
        buttons.forEach(function (b) { b.disabled = false; });
        status.textContent = action + ' failed — server error.';
      });
  }

  document.querySelectorAll('.btn-approve-proposal').forEach(function (btn) {
    btn.addEventListener('click', function () { _decideProposal(btn, 'approve'); });
  });
  document.querySelectorAll('.btn-reject-proposal').forEach(function (btn) {
    btn.addEventListener('click', function () { _decideProposal(btn, 'reject'); });
  });

  // ---------------------------------------------------------------------------
  // Decision history — reopen a rejected proposal
  // ---------------------------------------------------------------------------

  document.querySelectorAll('.btn-reopen-proposal').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var row = btn.closest('tr');
      var proposalId = btn.dataset.proposalId;
      var status = row.querySelector('.decision-status');

      btn.disabled = true;
      status.textContent = 'Reopening…';

      AQ.postJson('/api/proposal/' + proposalId + '/reopen', {})
        .then(function (result) {
          if (result.status === 200 && result.data.ok) {
            status.textContent = 'Reopened as #' + result.data.proposal_id + ' — reloading…';
            setTimeout(function () { window.location.reload(); }, 800);
          } else {
            btn.disabled = false;
            status.textContent = (result.data && result.data.error) || 'Reopen failed.';
          }
        })
        .catch(function () {
          btn.disabled = false;
          status.textContent = 'Reopen failed — server error.';
        });
    });
  });

}());
