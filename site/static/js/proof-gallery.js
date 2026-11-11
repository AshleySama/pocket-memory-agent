// Copyright 2026 AshleySama | SPDX-License-Identifier: Apache-2.0
// Optional progressive enhancement: without JS both actual examples remain visible.
document.querySelectorAll('[data-gallery]').forEach((gallery) => {
  const buttons = [...gallery.querySelectorAll('[data-view]')];
  const panels = [...gallery.querySelectorAll('[data-panel]')];
  const show = (id) => {
    buttons.forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.view === id)));
    panels.forEach((panel) => { panel.hidden = panel.id !== id; });
  };
  buttons.forEach((button) => button.addEventListener('click', () => show(button.dataset.view)));
  if (buttons.length) show(buttons[0].dataset.view);
});
