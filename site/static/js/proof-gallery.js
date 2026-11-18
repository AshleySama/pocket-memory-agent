// Copyright 2026 AshleySama | SPDX-License-Identifier: Apache-2.0
// Optional progressive enhancement: without JS both actual examples remain visible.
document.querySelectorAll('[data-gallery]').forEach((gallery) => {
  const buttons = [...gallery.querySelectorAll('[data-view]')];
  const panels = [...gallery.querySelectorAll('[data-panel]')];
  const show = (id, animate = false) => {
    buttons.forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.view === id)));
    panels.forEach((panel) => { panel.hidden = panel.id !== id; });
    const active = panels.find(panel => panel.id === id);
    if (animate && active?.animate && document.documentElement.dataset.motion === 'on') {
      active.getAnimations().forEach(animation => animation.cancel());
      active.animate([{opacity:0,transform:'translateY(12px)'},{opacity:1,transform:'none'}],
        {duration:550,easing:'cubic-bezier(.16,1,.3,1)'});
    }
  };
  buttons.forEach((button, index) => {
    button.addEventListener('click', () => show(button.dataset.view, true));
    button.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (index + 1) % buttons.length;
      else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (index - 1 + buttons.length) % buttons.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = buttons.length - 1;
      if (next === undefined) return;
      event.preventDefault();
      buttons[next].focus();
      show(buttons[next].dataset.view, true);
    });
  });
  if (buttons.length) show(buttons[0].dataset.view);
});
