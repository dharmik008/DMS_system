/**
 * static/js/csrf.js
 * ────────────────────────────────────────────────────────────────────────
 * App-wide CSRF protection helper (Flask-WTF).
 *
 * Every page includes <meta name="csrf-token" content="{{ csrf_token() }}">
 * in its <head>. This script reads that token and automatically attaches
 * it as the "X-CSRFToken" header to EVERY fetch() and XMLHttpRequest call
 * that uses a state-changing method (POST / PUT / PATCH / DELETE) — so
 * existing and future AJAX calls across the whole app are protected
 * without having to edit each call site individually.
 *
 * Plain <form method="POST"> submissions are protected separately via a
 * hidden {{ csrf_token() }} input field rendered inside each form.
 */
(function () {
  'use strict';

  var UNSAFE_METHODS = ['POST', 'PUT', 'PATCH', 'DELETE'];

  function getCsrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute('content') : null;
  }

  // Expose so page-specific scripts can also read it if needed
  // (e.g. to stamp it into a dynamically-built FormData / JSON body).
  window.getCsrfToken = getCsrfToken;

  // ── Patch window.fetch ──────────────────────────────────────────────────
  if (window.fetch) {
    var originalFetch = window.fetch;
    window.fetch = function (input, init) {
      init = init || {};
      var method = (init.method || 'GET').toUpperCase();

      if (UNSAFE_METHODS.indexOf(method) !== -1) {
        var token = getCsrfToken();
        if (token) {
          if (init.headers instanceof Headers) {
            if (!init.headers.has('X-CSRFToken')) {
              init.headers.set('X-CSRFToken', token);
            }
          } else if (Array.isArray(init.headers)) {
            init.headers = init.headers.concat([['X-CSRFToken', token]]);
          } else {
            init.headers = Object.assign({}, init.headers, { 'X-CSRFToken': token });
          }
        }
      }
      return originalFetch.call(this, input, init);
    };
  }

  // ── Patch XMLHttpRequest (legacy AJAX, if any) ──────────────────────────
  var origOpen = XMLHttpRequest.prototype.open;
  var origSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function (method) {
    this._csrfMethod = (method || 'GET').toUpperCase();
    return origOpen.apply(this, arguments);
  };

  XMLHttpRequest.prototype.send = function (body) {
    if (UNSAFE_METHODS.indexOf(this._csrfMethod) !== -1) {
      var token = getCsrfToken();
      if (token) {
        try {
          this.setRequestHeader('X-CSRFToken', token);
        } catch (e) {
          // header already set or request already sent — ignore
        }
      }
    }
    return origSend.apply(this, arguments);
  };
})();
