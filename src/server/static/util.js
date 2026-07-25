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

}());
