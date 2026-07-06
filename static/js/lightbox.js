/* ══════════════════════════════════════════════════════════════════════════
   Caryanams Reusable Image Lightbox (CYLightbox)
   ------------------------------------------------------------------------
   A small, dependency-free full-screen image viewer used across the user
   site. Markup: templates/_image_lightbox.html · Styles: css/lightbox.css

   Public API:
     CYLightbox.open(images, startIndex, opts)
        images     — array of image URLs (string[])
        startIndex — index to open on (default 0)
        opts.alt   — alt text for the image (optional)
        opts.onChange(index) — optional callback fired when the visible
                                image changes (used by car_detail.html to
                                keep its own gallery/thumbnails in sync)
     CYLightbox.close()

   Any element already on the page can also just add class "cy-lb-trigger"
   and a `data-lb-src="/path/to/image.jpg"` attribute — no JS wiring needed;
   see the delegated click listener at the bottom of this file, which is
   how listing/home car cards get lightbox support with zero extra markup.
   ══════════════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  const state = {
    images: [],
    index: 0,
    scale: 1,
    translateX: 0,
    translateY: 0,
    dragging: false,
    dragStartX: 0,
    dragStartY: 0,
    onChange: null,
  };

  let els = null; // cached DOM refs, populated on first use

  function cacheEls() {
    if (els) return els;
    els = {
      overlay:  document.getElementById('cyLightbox'),
      stage:    document.getElementById('cyLbStage'),
      img:      document.getElementById('cyLbImg'),
      prevBtn:  document.getElementById('cyLbPrev'),
      nextBtn:  document.getElementById('cyLbNext'),
      closeBtn: document.getElementById('cyLbClose'),
      counter:  document.getElementById('cyLbCounter'),
    };
    return els;
  }

  function resetZoom() {
    state.scale = 1;
    state.translateX = 0;
    state.translateY = 0;
    applyTransform();
    const e = cacheEls();
    if (e.img) e.img.classList.remove('cy-lb-zoomed', 'cy-lb-dragging');
  }

  function applyTransform() {
    const e = cacheEls();
    if (!e.img) return;
    e.img.style.transform = `translate(${state.translateX}px, ${state.translateY}px) scale(${state.scale})`;
  }

  function render() {
    const e = cacheEls();
    if (!e.overlay || !state.images.length) return;
    const src = state.images[state.index];
    // Same URL the page already loaded elsewhere -> browser cache serves it
    // instantly, no re-fetch from the server.
    e.img.src = src;
    resetZoom();
    const multi = state.images.length > 1;
    e.prevBtn.style.display = multi ? 'flex' : 'none';
    e.nextBtn.style.display = multi ? 'flex' : 'none';
    e.counter.style.display = multi ? 'block' : 'none';
    if (multi) e.counter.textContent = (state.index + 1) + ' / ' + state.images.length;
    if (typeof state.onChange === 'function') state.onChange(state.index);
  }

  function open(images, startIndex, opts) {
    opts = opts || {};
    if (!images || !images.length) return;
    const e = cacheEls();
    if (!e.overlay) return; // partial not included on this page
    state.images   = images;
    state.index    = Math.min(Math.max(startIndex || 0, 0), images.length - 1);
    state.onChange = opts.onChange || null;
    if (opts.alt) e.img.alt = opts.alt;
    render();
    e.overlay.style.display = 'flex';
    // Force reflow so the fade-in transition actually runs
    // eslint-disable-next-line no-unused-expressions
    e.overlay.offsetHeight;
    e.overlay.classList.add('cy-lb-open');
    document.body.classList.add('cy-lb-locked');
  }

  function close() {
    const e = cacheEls();
    if (!e.overlay) return;
    e.overlay.classList.remove('cy-lb-open');
    document.body.classList.remove('cy-lb-locked');
    // Wait for the fade-out transition before hiding, so it animates out
    setTimeout(() => {
      if (!e.overlay.classList.contains('cy-lb-open')) {
        e.overlay.style.display = 'none';
      }
    }, 260);
  }

  function next() {
    if (state.images.length < 2) return;
    state.index = (state.index + 1) % state.images.length;
    render();
  }

  function prev() {
    if (state.images.length < 2) return;
    state.index = (state.index - 1 + state.images.length) % state.images.length;
    render();
  }

  function toggleDoubleClickZoom(e) {
    if (state.scale > 1) {
      resetZoom();
    } else {
      state.scale = 2.2;
      state.translateX = 0;
      state.translateY = 0;
      applyTransform();
      cacheEls().img.classList.add('cy-lb-zoomed');
    }
  }

  function onWheelZoom(e) {
    e.preventDefault();
    const delta = e.deltaY < 0 ? 0.18 : -0.18;
    state.scale = Math.min(4, Math.max(1, state.scale + delta));
    if (state.scale <= 1) { resetZoom(); return; }
    cacheEls().img.classList.add('cy-lb-zoomed');
    applyTransform();
  }

  function initDrag(imgEl) {
    imgEl.addEventListener('mousedown', (e) => {
      if (state.scale <= 1) return;
      state.dragging  = true;
      state.dragStartX = e.clientX - state.translateX;
      state.dragStartY = e.clientY - state.translateY;
      imgEl.classList.add('cy-lb-dragging');
    });
    window.addEventListener('mousemove', (e) => {
      if (!state.dragging) return;
      state.translateX = e.clientX - state.dragStartX;
      state.translateY = e.clientY - state.dragStartY;
      applyTransform();
    });
    window.addEventListener('mouseup', () => {
      state.dragging = false;
      imgEl.classList.remove('cy-lb-dragging');
    });
  }

  function initSwipe(stageEl) {
    let touchStartX = 0, touchStartY = 0, touchMoved = false;
    stageEl.addEventListener('touchstart', (e) => {
      if (e.touches.length !== 1) return;
      touchStartX = e.touches[0].clientX;
      touchStartY = e.touches[0].clientY;
      touchMoved  = false;
    }, { passive: true });
    stageEl.addEventListener('touchmove', () => { touchMoved = true; }, { passive: true });
    stageEl.addEventListener('touchend', (e) => {
      if (state.scale > 1) return; // don't swipe-navigate while zoomed in
      const dx = touchStartX - e.changedTouches[0].clientX;
      const dy = touchStartY - e.changedTouches[0].clientY;
      if (touchMoved && Math.abs(dx) > 45 && Math.abs(dx) > Math.abs(dy)) {
        dx > 0 ? next() : prev();
      }
    }, { passive: true });
  }

  function initOnce() {
    const e = cacheEls();
    if (!e.overlay || e.overlay.dataset.cyLbInit) return;
    e.overlay.dataset.cyLbInit = '1';

    e.closeBtn.addEventListener('click', close);
    e.prevBtn.addEventListener('click', prev);
    e.nextBtn.addEventListener('click', next);

    // Click outside the image (on the dark backdrop) closes the viewer
    e.overlay.addEventListener('click', (evt) => {
      if (evt.target === e.overlay) close();
    });

    // ESC to close, arrow keys to navigate
    document.addEventListener('keydown', (evt) => {
      if (!e.overlay.classList.contains('cy-lb-open')) return;
      if (evt.key === 'Escape') close();
      if (evt.key === 'ArrowLeft') prev();
      if (evt.key === 'ArrowRight') next();
    });

    e.img.addEventListener('dblclick', toggleDoubleClickZoom);
    e.img.addEventListener('wheel', onWheelZoom, { passive: false });
    initDrag(e.img);
    initSwipe(e.stage);
  }

  // ── Public API ──────────────────────────────────────────────────────────
  window.CYLightbox = { open, close, next, prev };

  // ── Auto-init once the DOM (and the included partial) is ready ─────────
  document.addEventListener('DOMContentLoaded', initOnce);

  // ── Site-wide auto-wiring: any car card image can opt in just by having
  //    class="cy-lb-trigger" — no per-page JS needed (used by the listing
  //    and home page grids). Clicking the image opens the lightbox instead
  //    of following the card's link; clicking elsewhere on the card still
  //    navigates to the car's detail page as before. ──────────────────────
  document.addEventListener('click', function (e) {
    // Generic opt-in: any element marked class="cy-lb-trigger" with a
    // data-lb-src attribute (used for the car detail page's main gallery
    // image and thumbnails, where next/prev context already exists).
    const trigger = e.target.closest('.cy-lb-trigger');
    // Zero-markup support: listing/home grid cards already render their
    // photo inside a `.car-img-wrap` element — reuse that class as the hook
    // so no template changes were needed to enable the lightbox there.
    const cardImg = e.target.closest('.car-img-wrap img');

    const hit = trigger || cardImg;
    if (!hit) return;

    const src = (trigger && trigger.getAttribute('data-lb-src')) || hit.src;
    if (!src) return;

    e.preventDefault();
    e.stopPropagation();
    initOnce();
    open([src], 0, { alt: hit.alt || '' });
  }, true);
})();
