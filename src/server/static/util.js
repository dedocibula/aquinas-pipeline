// Shared fetch-JSON wrapper used by review.js, proposals.js, comments.js.
// Load this before any script that references window.AQ.
(function () {
  'use strict';

  window.AQ = window.AQ || {};

  AQ.postJson = function (url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(function (resp) {
      return resp.json().then(function (data) {
        return { status: resp.status, data: data };
      });
    });
  };

  AQ.getJson = function (url) {
    return fetch(url).then(function (resp) {
      return resp.json().then(function (data) {
        return { status: resp.status, data: data };
      });
    });
  };

  // ---------------------------------------------------------------------------
  // Markdown-style inline markup <-> HTML, shared by review.js
  // ---------------------------------------------------------------------------

  var ESCAPE_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;' };

  function _escapeHtml(text) {
    return String(text).replace(/[&<>]/g, function (c) { return ESCAPE_MAP[c]; });
  }

  // Converts **bold**, *italic*, _underline_ markdown into <b>/<i>/<u> HTML.
  // Bold is matched before italic so "**x**" isn't consumed by the single-asterisk rule.
  AQ.renderMarkup = function (text) {
    var html = _escapeHtml(text || '');
    html = html.replace(/\*\*([^*]+?)\*\*/g, '<b>$1</b>');
    html = html.replace(/\*([^*]+?)\*/g, '<i>$1</i>');
    html = html.replace(/_([^_]+?)_/g, '<u>$1</u>');
    return html;
  };

  var TAG_MARKERS = { B: '**', I: '*', U: '_' };
  var BLOCK_TAGS = { DIV: true, P: true };

  // Walks a DOM node's children, converting <b>/<i>/<u> back into markdown markers.
  // <br> and block wrappers (<div>/<p>, which browsers insert on Enter in a
  // contenteditable) become newlines so multi-line text round-trips.
  AQ.markupToMarkdown = function (rootNode) {
    var out = '';
    rootNode.childNodes.forEach(function (node) {
      if (node.nodeType === Node.TEXT_NODE) {
        out += node.textContent;
      } else if (node.nodeType === Node.ELEMENT_NODE) {
        var tag = node.tagName;
        if (tag === 'BR') {
          out += '\n';
        } else if (BLOCK_TAGS[tag]) {
          if (out.length && !/\n$/.test(out)) out += '\n';
          out += AQ.markupToMarkdown(node);
        } else {
          var marker = TAG_MARKERS[tag];
          var inner = AQ.markupToMarkdown(node);
          out += marker ? marker + inner + marker : inner;
        }
      }
    });
    return out;
  };

}());
