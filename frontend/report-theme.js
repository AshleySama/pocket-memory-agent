/* Copyright 2026 AshleySama
 * SPDX-License-Identifier: Apache-2.0
 */

/* Use the notebook's theme preference without loading its application or styles. */
(() => {
  'use strict';
  const key = 'pocket-memory-theme';
  const themes = new Set(['aqua-memory', 'paper-warm', 'ink-scholar', 'nordic-mint', 'midnight-focus']);
  const apply = theme => {
    document.documentElement.dataset.theme = themes.has(theme) ? theme : 'aqua-memory';
  };
  const readLocal = () => {
    try { return localStorage.getItem(key); } catch { return null; }
  };
  let preferenceRevision = 0;
  apply(readLocal());
  window.addEventListener('storage', event => {
    if (event.key === key || event.key === null) {
      preferenceRevision += 1;
      apply(event.newValue);
    }
  });
  window.addEventListener('focus', () => {
    const theme = readLocal();
    if (themes.has(theme)) apply(theme);
  });
  // The saved setting also works when the application opens on a different port.
  // A later Settings change must win over an older, still-pending response.
  const requestRevision = preferenceRevision;
  fetch('/api/settings', {cache: 'no-store'})
    .then(response => response.ok ? response.json() : null)
    .then(settings => {
      if (settings && themes.has(settings.theme) && preferenceRevision === requestRevision) {
        apply(settings.theme);
        try { localStorage.setItem(key, settings.theme); } catch { /* private browsing */ }
      }
    })
    .catch(() => { /* Keep the locally selected theme when settings are unavailable. */ });
})();
