# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Update-only receipt for a shown, loaded and usable native desktop window.

This is deliberately separate from the server-only health probe. An HTTP port
or a successful Popen is not proof that the desktop was opened.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .config import app_directory
from .safe_update import UpdateError, _absolute, _atomic_json, _no_links, _overlap
from .version import APP_VERSION

logger = logging.getLogger(__name__)
_FLAGS = ('PM_UPDATE_DESKTOP_TOKEN', 'PM_UPDATE_DESKTOP_RECEIPT',
          'PM_UPDATE_DESKTOP_DATA_DIR')


def update_desktop_requested() -> bool:
    return any(name in os.environ for name in _FLAGS)


def _local_path(value: str) -> Path:
    path = Path(value)
    if not value or not path.is_absolute() or value.replace('/', '\\').startswith('\\\\'):
        raise UpdateError('Desktop readiness requires an absolute local path')
    return _absolute(path)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UpdateError('Desktop health endpoint must not redirect')


class DesktopReadiness:
    def __init__(self, url: str, token: str, receipt: Path,
                 data_dir: Path, config_path: Path):
        self.url = url.rstrip('/')
        self.token = token
        self.receipt = receipt
        self.data_dir = data_dir
        self.config_path = config_path
        self.pid = os.getpid()
        self.executable = _absolute(sys.executable)
        self.install_dir = _absolute(app_directory())
        self.shown = threading.Event()
        self.loaded = threading.Event()
        self.stopped = threading.Event()
        self._lock = threading.Lock()
        self._state = None
        self.window = None
        self._validate_destination()
        if self.receipt.exists() or self.receipt.with_name(self.receipt.name + '.tmp').exists():
            raise UpdateError('Desktop readiness destination already exists')

    @classmethod
    def from_environment(cls, url: str):
        if not update_desktop_requested():
            return None
        token = os.environ.get('PM_UPDATE_DESKTOP_TOKEN', '')
        if not re.fullmatch(r'[0-9a-f]{64}', token):
            raise UpdateError('Invalid desktop readiness token')
        parsed = urlsplit(url)
        if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {'', '/'} or parsed.port is None):
            raise UpdateError('Desktop readiness requires a local application URL')
        receipt = _local_path(os.environ.get('PM_UPDATE_DESKTOP_RECEIPT', ''))
        data_dir = _local_path(os.environ.get('PM_UPDATE_DESKTOP_DATA_DIR', ''))
        config_path = _local_path(os.environ.get('PM_UPDATE_CONFIG_PATH', ''))
        if not data_dir.is_dir() or not config_path.is_file():
            raise UpdateError('Desktop readiness data/config identity is unavailable')
        return cls(url, token, receipt, data_dir, config_path)

    def _validate_destination(self):
        _no_links(self.receipt)
        if self.receipt.name != 'desktop-ready.json' or not self.receipt.parent.is_dir():
            raise UpdateError('Invalid desktop readiness destination')
        if any(_overlap(self.receipt.parent, protected)
               for protected in (self.install_dir, self.data_dir, self.config_path)):
            raise UpdateError('Desktop readiness destination overlaps protected files')

    @property
    def failed(self):
        return self._state == 'error'

    def attach(self, window):
        self.window = window
        # "loaded" can precede native visibility; it never implies "shown".
        window.events.shown += self._on_shown
        window.events.loaded += self._on_loaded

    def _on_shown(self, *args):
        self.shown.set()

    def _on_loaded(self, *args):
        self.loaded.set()

    def _health(self):
        request = Request(self.url + '/api/update-health',
                          headers={'X-Pocket-Update-Token': self.token})
        # Corporate HTTP proxies must not intercept the local receipt handshake.
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=3) as response:
            body = response.read(65537)
        if len(body) > 65536:
            raise UpdateError('Desktop health response is too large')
        return json.loads(body)

    def _validate_health(self, health):
        if (not isinstance(health, dict) or health.get('ok') is not True
                or not isinstance(health.get('token'), str)
                or not secrets.compare_digest(health['token'], self.token)
                or health.get('app_version') != APP_VERSION
                or type(health.get('pid')) is not int or health['pid'] <= 0
                or health['pid'] == self.pid):
            raise UpdateError('Desktop health identity mismatch')
        if (_local_path(health.get('data_dir', '')) != self.data_dir
                or _local_path(health.get('config_path', '')) != self.config_path):
            raise UpdateError('Desktop health storage identity mismatch')

    def _write(self, state: str, **details):
        with self._lock:
            if self._state is not None:
                return
            self._validate_destination()
            payload = dict(state=state, token=self.token, pid=self.pid,
                           executable=str(self.executable), app_version=APP_VERSION,
                           data_dir=str(self.data_dir), config_path=str(self.config_path),
                           url=self.url, shown=self.shown.is_set(), page_ready=False)
            payload.update(details)
            _atomic_json(self.receipt, payload)
            self._state = state

    def fail(self, reason: str):
        # Callers use fixed reason codes, never exception text or request headers.
        self._write('error', reason=reason)

    def run(self, timeout: float = 90, poll_interval: float = .2):
        deadline = time.monotonic() + timeout
        try:
            while not self.stopped.is_set() and time.monotonic() < deadline:
                if self.shown.is_set() and self.loaded.is_set():
                    if self.window.evaluate_js('window.__PM_DESKTOP_READY__ === true') is True:
                        health = self._health()
                        self._validate_health(health)
                        self._write('ready', server_pid=health['pid'], page_ready=True)
                        return
                self.stopped.wait(poll_interval)
            self.fail('desktop_closed_before_ready' if self.stopped.is_set()
                      else 'desktop_readiness_timeout')
        except Exception as exc:
            logger.error('Desktop readiness failed (%s)', type(exc).__name__)
            try:
                self.fail('desktop_readiness_failed')
            except Exception:
                logger.error('Cannot write desktop failure receipt')
        finally:
            if self._state != 'ready' and self.window is not None:
                try:
                    self.window.destroy()
                except Exception:
                    logger.warning('Cannot close incomplete desktop window')

    def close(self):
        self.stopped.set()
        self.fail('desktop_closed_before_ready')
