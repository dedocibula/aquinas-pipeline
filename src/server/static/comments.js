// Comment thread sidebar logic shared by article.html and question.html.
// Requires _currentUserEmail to be set as a global before this script is loaded.
(function () {
  'use strict';

  var sidebar = document.getElementById('comment-sidebar');
  if (!sidebar) return;

  var listEl     = document.getElementById('comment-list');
  var statusEl   = document.getElementById('comment-sidebar-status');
  var textarea   = document.getElementById('comment-textarea');
  var addBtn     = document.getElementById('comment-add-btn');
  var resolveBtn = document.getElementById('comment-resolve-btn');
  var reopenBtn  = document.getElementById('comment-reopen-btn');
  var closeBtn   = document.getElementById('comment-sidebar-close');

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------

  function _fmtTime(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString();
  }

  function _button(segId) {
    return document.querySelector('.btn-comment[data-segment-id="' + segId + '"]');
  }

  function _row(segId) {
    return document.querySelector('tr[data-segment-id="' + segId + '"]');
  }

  function _setStatus(msg) {
    if (!statusEl) return;
    if (msg) { statusEl.textContent = msg; statusEl.style.display = ''; }
    else     { statusEl.textContent = ''; statusEl.style.display = 'none'; }
  }

  function _updateButton(segId, openCount, unread) {
    var btn = _button(segId);
    if (!btn) return;
    btn.dataset.openCount = String(openCount);
    btn.dataset.unread = String(unread);
    btn.classList.toggle('btn-comment-open', openCount > 0);
    btn.innerHTML = '\u{1F4AC}' + (openCount > 0 ? ' ' + openCount : '') +
      '<span class="comment-unread-dot"' + (unread > 0 ? '' : ' style="display:none"') + '></span>';
  }

  function _renderComment(c) {
    var card = document.createElement('div');
    card.className = 'comment-card' + (c.resolved ? ' comment-card-resolved' : '');
    card.dataset.commentId = c.comment_id;

    var meta = document.createElement('div');
    meta.className = 'comment-meta';
    meta.textContent = c.author + ' · ' + _fmtTime(c.created_at);
    card.appendChild(meta);

    var body = document.createElement('div');
    body.className = 'comment-body';
    body.textContent = c.body;
    card.appendChild(body);

    if (c.author === _currentUserEmail) {
      var del = document.createElement('button');
      del.className = 'btn-comment-delete';
      del.textContent = 'Delete';
      del.addEventListener('click', function () { deleteComment(c.comment_id); });
      card.appendChild(del);
    }

    return card;
  }

  function _renderThread(segId, thread) {
    listEl.innerHTML = '';
    if (!thread.comments.length) {
      var empty = document.createElement('div');
      empty.className = 'comment-empty';
      empty.textContent = 'No comments yet.';
      listEl.appendChild(empty);
    } else {
      thread.comments.forEach(function (c) { listEl.appendChild(_renderComment(c)); });
    }
    resolveBtn.style.display = thread.resolved ? 'none' : '';
    reopenBtn.style.display  = thread.resolved ? '' : 'none';
    _updateButton(segId, thread.open_count, 0);
  }

  // ---------------------------------------------------------------------------
  // API calls
  // ---------------------------------------------------------------------------

  function openSidebar(segId) {
    var prevRow = document.querySelector('tr.row-comment-active');
    if (prevRow) prevRow.classList.remove('row-comment-active');

    sidebar.dataset.segmentId = segId;
    sidebar.style.display = '';
    textarea.value = '';
    _setStatus('Loading…');
    listEl.innerHTML = '';

    var row = _row(segId);
    if (row) row.classList.add('row-comment-active');

    fetch('/api/segment/' + segId + '/comments')
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        _setStatus('');
        if (!data.ok) { _setStatus('Failed to load comments.'); return; }
        _renderThread(segId, data);
      })
      .catch(function () { _setStatus('Failed to load comments — server error.'); });
  }

  function closeSidebar() {
    sidebar.style.display = 'none';
    var row = document.querySelector('tr.row-comment-active');
    if (row) row.classList.remove('row-comment-active');
    sidebar.dataset.segmentId = '';
  }

  function addComment() {
    var segId = sidebar.dataset.segmentId;
    var body  = textarea.value.trim();
    if (!segId || !body) return;
    addBtn.disabled = true;
    fetch('/api/segment/' + segId + '/comments', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ body: body }),
    })
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        addBtn.disabled = false;
        if (!data.ok) { alert('Add comment failed: ' + (data.error || 'server error')); return; }
        textarea.value = '';
        var emptyEl = listEl.querySelector('.comment-empty');
        if (emptyEl) emptyEl.remove();
        listEl.appendChild(_renderComment(data.comment));
        // A new comment always reopens the thread, but earlier comments keep
        // whatever resolved state they already had — only the thread-level
        // resolved flag (derived from open_count) flips.
        resolveBtn.style.display = '';
        reopenBtn.style.display  = 'none';
        _updateButton(segId, data.open_count, 0);
      })
      .catch(function () {
        addBtn.disabled = false;
        alert('Add comment failed — server error.');
      });
  }

  function setResolved(resolved) {
    var segId = sidebar.dataset.segmentId;
    if (!segId) return;
    var url = '/api/segment/' + segId + '/comments/' + (resolved ? 'resolve' : 'reopen');
    fetch(url, { method: 'POST' })
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        if (!data.ok) { alert('Action failed: ' + (data.error || 'server error')); return; }
        listEl.querySelectorAll('.comment-card').forEach(function (el) {
          el.classList.toggle('comment-card-resolved', resolved);
        });
        resolveBtn.style.display = resolved ? 'none' : '';
        reopenBtn.style.display  = resolved ? '' : 'none';
        _updateButton(segId, resolved ? 0 : listEl.querySelectorAll('.comment-card').length, 0);
      })
      .catch(function () { alert('Action failed — server error.'); });
  }

  function deleteComment(commentId) {
    var segId = sidebar.dataset.segmentId;
    if (!confirm('Delete this comment?')) return;
    fetch('/api/comment/' + commentId, { method: 'DELETE' })
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        if (!data.ok) { alert('Delete failed: ' + (data.error || 'server error')); return; }
        var card = listEl.querySelector('.comment-card[data-comment-id="' + commentId + '"]');
        if (card) card.remove();
        if (!listEl.querySelector('.comment-card')) {
          var empty = document.createElement('div');
          empty.className = 'comment-empty';
          empty.textContent = 'No comments yet.';
          listEl.appendChild(empty);
        }
        var openCount = listEl.querySelectorAll('.comment-card:not(.comment-card-resolved)').length;
        _updateButton(segId, openCount, 0);
        // Deleting the last open comment can implicitly resolve the thread (mirrors
        // list_comments' resolved = bool(comments) and open_count==0 on the server);
        // keep the Resolve/Reopen toggle in sync instead of leaving it stale.
        var hasComments = !!listEl.querySelector('.comment-card');
        var nowResolved = hasComments && openCount === 0;
        resolveBtn.style.display = nowResolved ? 'none' : '';
        reopenBtn.style.display  = nowResolved ? '' : 'none';
      })
      .catch(function () { alert('Delete failed — server error.'); });
  }

  // ---------------------------------------------------------------------------
  // Wiring
  // ---------------------------------------------------------------------------

  document.querySelectorAll('.btn-comment').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var segId = btn.dataset.segmentId;
      if (sidebar.style.display !== 'none' && sidebar.dataset.segmentId === segId) {
        closeSidebar();
      } else {
        openSidebar(segId);
      }
    });
  });

  closeBtn.addEventListener('click', closeSidebar);
  addBtn.addEventListener('click', addComment);
  resolveBtn.addEventListener('click', function () { setResolved(true); });
  reopenBtn.addEventListener('click', function () { setResolved(false); });

}());
