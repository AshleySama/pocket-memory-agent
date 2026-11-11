# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""No-network checks for the update checker and protected install failures."""
import io
import json
import ssl
import tempfile
import time
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from pocket_memory import app_updates as updates
from pocket_memory.safe_update import UpdateError


class UpdateNetworkFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pm-update-network-')
        self.root = Path(self.temp.name)
        config = types.SimpleNamespace(path=self.root/'config.json', data_dir=self.root/'data')
        self.service = updates.AppUpdateService(config, config.data_dir,
                                               server_port=6797, exit_callback=mock.Mock(), current_version='1.2.0')

    def tearDown(self):
        self.temp.cleanup()

    def finish(self):
        deadline = time.monotonic() + 3
        while self.service._busy and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertFalse(self.service._busy)
        return self.service.status()

    def feed(self):
        return dict(schema_version=1, app_id='pocket-memory', published=True,
                    version='1.2.1', notes='修复', package=dict(
                        url=f'https://{updates.TRUSTED_HOST}/PocketMemory-1.2.1-update.zip',
                        size=12, sha256='a'*64))

    def test_remote_network_failures_are_not_install_errors(self):
        errors = [urllib.error.URLError(PermissionError(10013, 'socket denied')),
                  urllib.error.URLError('DNS unavailable'), TimeoutError('read timed out'),
                  ConnectionResetError('reset'),
                  urllib.error.HTTPError(updates.FEED_URL, 503, 'busy', {}, None)]
        for error in errors:
            with self.subTest(type=type(error).__name__), mock.patch.object(updates, '_open_https', side_effect=error):
                self.service._offer = self.feed()
                self.service._set(has_update=True, latest_version='1.2.1', notes='stale', package_bytes=12)
                with self.assertLogs(updates.logger, 'WARNING') as log:
                    self.service.check_async()
                    state = self.finish()
                self.assertEqual(state['state'], 'unavailable')
                self.assertEqual(state['message'], '暂无可用更新。')
                self.assertFalse(state['has_update'])
                self.assertFalse(state['can_update'])
                self.assertIsNone(state['latest_version'])
                self.assertIsNone(self.service._offer)
                self.assertEqual(state['notes'], '')
                self.assertTrue(any('Traceback' in text for text in log.output))

    def test_retry_after_network_recovers_can_offer_update(self):
        with mock.patch.object(updates, '_open_https', side_effect=TimeoutError()), self.assertLogs(updates.logger,'WARNING'):
            self.service.check_async()
            self.finish()
        with mock.patch.object(updates, '_open_https', return_value=io.BytesIO(json.dumps(self.feed()).encode())):
            self.service.check_async()
            state = self.finish()
        self.assertEqual(state['state'], 'available')
        self.assertTrue(state['has_update'])
        self.assertEqual(state['latest_version'], '1.2.1')

    def test_bad_metadata_and_redirect_remain_errors(self):
        for failure in [UpdateError('禁止重定向'), UpdateError('程序包校验失败')]:
            with self.subTest(error=str(failure)), mock.patch.object(updates, '_open_https', side_effect=failure):
                self.service.check_async()
                state = self.finish()
            self.assertEqual(state['state'], 'error')
            self.assertEqual(state['message'], str(failure))
        with mock.patch.object(updates, '_open_https', return_value=io.BytesIO(b'{invalid json')):
            self.service.check_async()
            state = self.finish()
        self.assertEqual(state['state'], 'error')
        self.assertIn('JSON', state['message'])

    def test_certificate_verification_failure_is_not_hidden(self):
        failure = urllib.error.URLError(ssl.SSLCertVerificationError('certificate invalid'))
        with mock.patch.object(updates, '_open_https', side_effect=failure):
            self.service.check_async()
            state = self.finish()
        self.assertEqual(state['state'], 'error')
        self.assertIn('安全连接', state['message'])

    def test_unavailable_during_install_is_still_an_install_failure(self):
        self.service.preview = False
        self.service.install = self.root/'install'
        self.service._offer = self.feed()
        self.service._set(can_update=True)
        with mock.patch.object(updates, '_open_https', side_effect=TimeoutError()):
            self.service.start_update()
            state = self.finish()
        self.assertEqual(state['state'], 'error')
        self.assertFalse(state['can_update'])
        self.service.exit_callback.assert_not_called()
        self.assertFalse(self.service.install.exists())


if __name__ == '__main__':
    unittest.main()
