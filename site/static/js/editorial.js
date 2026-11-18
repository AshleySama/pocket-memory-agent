// Copyright 2026 AshleySama | SPDX-License-Identifier: Apache-2.0
(() => {
  'use strict';

  const initialize = () => {
    const root = document.documentElement;
    const reveals = [...document.querySelectorAll('[data-reveal]')];
    const drifts = [...document.querySelectorAll('[data-drift]')];
    const toggles = [...document.querySelectorAll('.motion-toggle')];
    const replays = [...document.querySelectorAll('.motion-replay')];
    const progress = document.querySelector('.reading-progress > span');
    const topbar = document.querySelector('.topbar');
    const backToTop = document.querySelector('.back-to-top');
    const seen = new Set();
    const animations = new Map();
    const storageKey = 'pocket-memory-page-motion';
    const supported = typeof Element.prototype.animate === 'function' && 'IntersectionObserver' in window;
    let enabled = supported;
    let observer;
    let frame = 0;

    // Only an explicit choice changes the default. System preferences do not override it.
    try { enabled = supported && sessionStorage.getItem(storageKey) !== 'off'; } catch (_) { /* Storage is optional. */ }

    const navigation = [...document.querySelectorAll('.nav a[href^="#"]')].map(link => {
      try { return { link, section: document.getElementById(decodeURIComponent(link.hash.slice(1))) }; }
      catch (_) { return { link, section: null }; }
    }).filter(item => item.section);
    const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
    const inViewport = element => {
      const rect = element.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < window.innerHeight;
    };

    const clearAnimation = element => {
      const animation = animations.get(element);
      if (animation) { animations.delete(element); animation.cancel(); }
      // These are reserved for the reveal/drift enhancement, never persistent layout.
      ['transform', 'clip-path', 'opacity'].forEach(property => element.style.removeProperty(property));
    };

    const syncControls = () => {
      root.dataset.motion = enabled ? 'on' : 'off';
      toggles.forEach(button => {
        button.setAttribute('aria-pressed', String(enabled));
        button.setAttribute('aria-label', enabled ? '关闭页面动效' : '开启页面动效');
        button.title = enabled ? '关闭页面动效' : '开启页面动效';
        button.disabled = !supported;
        const label = button.querySelector('[data-motion-label]');
        if (label) label.textContent = enabled ? '动效已开启' : '动效已关闭';
      });
      replays.forEach(button => { button.disabled = !enabled; });
    };

    const showEverything = () => {
      observer?.disconnect();
      root.classList.remove('motion-ready');
      reveals.forEach(element => { clearAnimation(element); element.classList.add('is-visible'); });
      drifts.forEach(element => element.style.removeProperty('transform'));
    };

    const failOpen = () => {
      enabled = false;
      showEverything();
      syncControls();
    };

    const reveal = (element, delay = 0) => {
      clearAnimation(element);
      element.classList.add('is-visible');
      seen.add(element);
      observer?.unobserve(element);
      if (!enabled || document.hidden) return;
      const kind = element.dataset.reveal;
      const first = { opacity: 0, transform: 'translate3d(0, 28px, 0)' };
      const last = { opacity: 1, transform: 'translate3d(0, 0, 0)' };
      if (kind === 'left') first.transform = 'translate3d(-32px, 0, 0)';
      if (kind === 'right') first.transform = 'translate3d(32px, 0, 0)';
      if (kind === 'media') {
        first.transform = 'translate3d(0, 34px, 0) scale(.985)';
        last.transform = 'translate3d(0, 0, 0) scale(1)';
      }
      if (kind === 'title') {
        first.clipPath = 'inset(0 0 24% 0)';
        last.clipPath = 'inset(0 0 0% 0)';
      }
      try {
        const animation = element.animate([first, last], {
          duration: kind === 'title' || kind === 'media' ? 1100 : 950,
          delay,
          easing: 'cubic-bezier(.16, 1, .3, 1)',
          fill: 'both'
        });
        animations.set(element, animation);
        animation.finished.then(() => {
          if (animations.get(element) === animation) {
            animations.delete(element);
            animation.cancel();
          }
        }).catch(() => { /* Cancelling a replay or disabling motion is expected. */ });
      } catch (_) { failOpen(); }
    };

    const revealBatch = elements => {
      if (document.hidden) return;
      const groups = new Map();
      elements.forEach(element => {
        const group = element.closest('section, .hero') || element.parentElement;
        const index = groups.get(group) || 0;
        groups.set(group, index + 1);
        const explicitDelay = Number(element.dataset.delay);
        const delay = element.hasAttribute('data-delay') && Number.isFinite(explicitDelay)
          ? clamp(explicitDelay, 0, 240) : Math.min(index * 100, 240);
        reveal(element, delay);
      });
    };

    const revealViewport = (replay = false) => {
      if (!enabled) return;
      revealBatch(reveals.filter(element => (replay || !seen.has(element)) && inViewport(element)));
    };

    const startMotion = () => {
      try {
        if (!observer) observer = new IntersectionObserver(entries => {
          if (!enabled || document.hidden) return;
          try { revealBatch(entries.filter(entry => entry.isIntersecting && !seen.has(entry.target)).map(entry => entry.target)); }
          catch (_) { failOpen(); }
        }, { rootMargin: '0px 0px -4% 0px', threshold: 0 });
        reveals.forEach(element => {
          element.classList.toggle('is-visible', seen.has(element));
          if (!seen.has(element)) observer.observe(element);
        });
        // Add the hiding rule only after all prerequisites have initialized.
        root.classList.add('motion-ready');
        revealViewport(true);
      } catch (_) { failOpen(); }
    };

    const renderScroll = () => {
      frame = 0;
      if (document.hidden) return;
      const height = window.innerHeight;
      const scrollTop = window.scrollY;
      const scrollRange = Math.max(0, root.scrollHeight - height);
      if (progress) progress.style.transform = `scaleX(${scrollRange ? clamp(scrollTop / scrollRange, 0, 1) : 0})`;
      topbar?.classList.toggle('is-scrolled', scrollTop > 16);
      if (backToTop) {
        const visible = scrollTop > height * .7;
        backToTop.classList.toggle('is-visible', visible);
        backToTop.setAttribute('aria-hidden', String(!visible));
        backToTop.tabIndex = visible ? 0 : -1;
      }
      let active = null;
      let nearestTop = -Infinity;
      // Match the section just below the sticky header. An unlisted section
      // (demo/FAQ) must not inherit the preceding chapter's active state.
      const headerThreshold = (topbar?.getBoundingClientRect().bottom || 0) + 48;
      // The last section cannot always reach the header before scrolling stops.
      const atPageEnd = scrollRange > 0 && scrollTop >= scrollRange - 2;
      const navigationThreshold = atPageEnd ? Math.max(headerThreshold, height * .6) : headerThreshold;
      navigation.forEach(item => {
        const sectionRect = item.section.getBoundingClientRect();
        const sectionTop = sectionRect.top;
        if (sectionTop <= navigationThreshold && sectionRect.bottom > navigationThreshold && sectionTop > nearestTop) {
          active = item;
          nearestTop = sectionTop;
        }
      });
      navigation.forEach(item => {
        if (item === active) item.link.setAttribute('aria-current', 'location');
        else item.link.removeAttribute('aria-current');
      });
      drifts.forEach(element => {
        if (!enabled || window.innerWidth < 900) { element.style.removeProperty('transform'); return; }
        const rect = (element.parentElement || element).getBoundingClientRect();
        if (rect.bottom < -24 || rect.top > height + 24) return;
        const offset = clamp((height / 2 - (rect.top + rect.height / 2)) / height * 20, -12, 12);
        element.style.transform = `translate3d(0, ${offset.toFixed(2)}px, 0)`;
      });
    };

    const requestFrame = () => {
      if (!frame && !document.hidden) frame = requestAnimationFrame(renderScroll);
    };

    toggles.forEach(button => button.addEventListener('click', () => {
      if (!supported) return;
      enabled = !enabled;
      try { sessionStorage.setItem(storageKey, enabled ? 'on' : 'off'); } catch (_) { /* Storage is optional. */ }
      if (enabled) startMotion();
      else showEverything();
      syncControls();
      requestFrame();
    }));
    replays.forEach(button => button.addEventListener('click', () => revealViewport(true)));
    backToTop?.addEventListener('click', event => {
      event.preventDefault();
      window.scrollTo({ top: 0, behavior: enabled ? 'smooth' : 'auto' });
    });
    window.addEventListener('scroll', requestFrame, { passive: true });
    window.addEventListener('resize', requestFrame, { passive: true });
    window.addEventListener('load', requestFrame, { once: true });
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        if (frame) { cancelAnimationFrame(frame); frame = 0; }
        animations.forEach(animation => animation.pause());
      } else {
        animations.forEach(animation => animation.play());
        revealViewport();
        requestFrame();
      }
    });
    // Keyboard navigation must never land on content that is still hidden.
    document.addEventListener('focusin', event => {
      let element = event.target instanceof Element ? event.target.closest('[data-reveal]') : null;
      while (element) {
        if (!seen.has(element)) {
          clearAnimation(element);
          element.classList.add('is-visible');
          seen.add(element);
          observer?.unobserve(element);
        }
        element = element.parentElement?.closest('[data-reveal]');
      }
    });

    syncControls();
    if (enabled) startMotion();
    else showEverything();
    requestFrame();

    // Original-image links remain ordinary links if native dialog is unavailable.
    const imageLinks = [...document.querySelectorAll('a[data-lightbox]')];
    if (!imageLinks.length || typeof HTMLDialogElement === 'undefined' || !HTMLDialogElement.prototype.showModal) return;
    const dialog = document.createElement('dialog');
    dialog.className = 'image-dialog';
    dialog.setAttribute('aria-label', '查看产品截图');
    const toolbar = document.createElement('div');
    toolbar.className = 'image-dialog-toolbar';
    const caption = document.createElement('span');
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'image-dialog-close';
    close.setAttribute('aria-label', '关闭截图（Esc）');
    close.textContent = '关闭 ×';
    const canvas = document.createElement('div');
    canvas.className = 'image-dialog-canvas';
    const image = document.createElement('img');
    image.decoding = 'async';
    canvas.append(image);
    toolbar.append(caption, close);
    dialog.append(toolbar, canvas);
    document.body.append(dialog);
    let opener = null;
    let savedBodyStyle = null;
    let backdropPressed = false;

    const unlockBody = () => {
      if (!savedBodyStyle) return;
      Object.entries(savedBodyStyle).forEach(([property, saved]) => {
        if (saved.value) document.body.style.setProperty(property, saved.value, saved.priority);
        else document.body.style.removeProperty(property);
      });
      savedBodyStyle = null;
    };
    const outsideDialog = event => {
      const rect = dialog.getBoundingClientRect();
      return event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom;
    };
    close.addEventListener('click', () => dialog.close());
    dialog.addEventListener('cancel', event => { event.preventDefault(); dialog.close(); });
    dialog.addEventListener('pointerdown', event => { backdropPressed = event.target === dialog && outsideDialog(event); });
    dialog.addEventListener('click', event => {
      if (backdropPressed && event.target === dialog && outsideDialog(event)) dialog.close();
      backdropPressed = false;
    });
    dialog.addEventListener('close', () => {
      unlockBody();
      const previous = opener;
      opener = null;
      if (previous?.isConnected) previous.focus({ preventScroll: true });
      requestFrame();
    });
    imageLinks.forEach(link => link.addEventListener('click', event => {
      if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      const thumbnail = link.querySelector('img');
      const description = link.dataset.caption || thumbnail?.alt || link.getAttribute('aria-label') || '产品截图';
      image.src = link.href;
      image.alt = thumbnail?.alt || description;
      caption.textContent = description;
      dialog.setAttribute('aria-label', `查看截图：${description}`);
      opener = link;
      try {
        dialog.showModal();
        event.preventDefault();
        savedBodyStyle = Object.fromEntries(['overflow', 'padding-right'].map(property => [property, {
          value: document.body.style.getPropertyValue(property), priority: document.body.style.getPropertyPriority(property)
        }]));
        const scrollbar = Math.max(0, window.innerWidth - root.clientWidth);
        const padding = parseFloat(getComputedStyle(document.body).paddingRight) || 0;
        document.body.style.setProperty('overflow', 'hidden');
        if (scrollbar) document.body.style.setProperty('padding-right', `${padding + scrollbar}px`);
        canvas.scrollTop = 0;
        canvas.scrollLeft = 0;
        close.focus({ preventScroll: true });
      } catch (_) {
        unlockBody();
        if (dialog.open) dialog.close();
        opener = null;
      }
    }));
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize, { once: true });
  else initialize();
})();
