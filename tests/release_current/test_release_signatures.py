"""Opt-in signing contract tests. Mocked signatures are NOT signing acceptance."""
import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest import mock

from scripts import prepare_offline_delivery as delivery
from scripts import release_signatures as signatures
import test_delivery as fixtures


class SignatureDeliveryTests(unittest.TestCase):
    setUp = fixtures.DeliveryTests.setUp
    tearDown = fixtures.DeliveryTests.tearDown
    signer = 'A' * 40

    def arguments(self, signed=True):
        result = ['--built-app', str(self.build), '--model-source', str(self.models),
                  '--destination', str(self.root/'out'), '--version', 'fixture.2',
                  '--mode', 'update', '--schema-id', 'legacy-unversioned',
                  '--executor', 'standalone', '--updater-exe', str(self.helper)]
        if signed:
            result += ['--expected-signer-thumbprint', self.signer]
        return result

    def hashes(self):
        return {'PocketMemory.exe': signatures.file_hash(self.build/'PocketMemory.exe'),
                delivery.WORKER_RELATIVE_PATH: signatures.file_hash(self.helper)}

    def report(self):
        return dict(passed=True, expected_signer_thumbprint=self.signer,
                    endpoint_policy_verified=False, files=[dict(
                        file=str(p.absolute()), sha256=signatures.file_hash(p),
                        passed=True, signature_status='Valid', timestamped=True,
                        signer_thumbprint=self.signer)
                        for p in (self.build/'PocketMemory.exe', self.helper)])

    def run_mocked_signature_check(self, report, *, returncode=0, mutate=None):
        def execute(command, **kwargs):
            self.assertNotIn('-ExecutionPolicy', command)
            self.assertEqual(command[1:4], ['-NoProfile', '-NonInteractive', '-File'])
            Path(command[command.index('-OutputPath')+1]).write_text(json.dumps(report), encoding='utf-8')
            if mutate:
                mutate()
            return subprocess.CompletedProcess(command, returncode)
        with mock.patch.object(signatures.subprocess, 'run', side_effect=execute):
            return signatures.verify_release_signatures(self.build/'PocketMemory.exe', self.signer, self.helper)

    def test_no_publisher_uses_normal_cli_with_exact_manifest_and_model_requirements(self):
        output = io.StringIO()
        with mock.patch.object(signatures, 'verify_release_signatures') as verify, contextlib.redirect_stdout(output):
            delivery.main(self.arguments(False))
        verify.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertEqual(result['release_signatures'], {
            'requested': False, 'passed': None, 'endpoint_policy_verified': False})
        destination = self.root/'out'
        manifest = json.loads((destination/'update-manifest.json').read_text(encoding='utf-8'))
        files = manifest['files']
        actual_paths = {p.relative_to(destination/'payload').as_posix()
                        for p in (destination/'payload').rglob('*') if p.is_file()}
        self.assertEqual(actual_paths, {row['path'] for row in files})
        for row in files:
            copied = destination/'payload'/row['path']
            self.assertEqual(copied.stat().st_size, row['size'])
            self.assertEqual(signatures.file_hash(copied), row['sha256'])
        self.assertEqual(manifest['model_requirements'][0]['sha256'],
                         signatures.file_hash(self.models/'Qwen3-4B/model.gguf'))
        self.assertFalse((destination/'models').exists())

    def test_no_publisher_still_rejects_missing_model_before_layout_creation(self):
        (self.models/'Qwen3-4B/model.gguf').unlink()
        with mock.patch.object(signatures, 'verify_release_signatures') as verify:
            with self.assertRaisesRegex(ValueError, 'GGUF'):
                delivery.main(self.arguments(False))
        verify.assert_not_called()
        self.assertFalse((self.root/'out').exists())

    def test_explicit_empty_publisher_is_not_silently_treated_as_unsigned(self):
        arguments = self.arguments(False) + ['--expected-signer-thumbprint', '']
        with mock.patch.object(delivery, 'prepare') as prepare, mock.patch.object(signatures.subprocess, 'run') as execute:
            with self.assertRaisesRegex(ValueError, '40'):
                delivery.main(arguments)
        execute.assert_not_called()
        prepare.assert_not_called()
        self.assertFalse((self.root/'out').exists())

    def test_rejected_signature_stops_before_any_layout_or_model_hashing(self):
        with mock.patch.object(signatures, 'verify_release_signatures', side_effect=ValueError('NotSigned')):
            with mock.patch.object(delivery, 'prepare') as prepare:
                with self.assertRaisesRegex(ValueError, 'NotSigned'):
                    delivery.main(self.arguments())
                prepare.assert_not_called()
        self.assertFalse((self.root/'out').exists())

    def test_verified_hashes_reach_manifest_preparation(self):
        report = self.report()
        with mock.patch.object(signatures, 'verify_release_signatures', return_value=report):
            with mock.patch.object(delivery, 'prepare', return_value={}) as prepare, contextlib.redirect_stdout(io.StringIO()):
                delivery.main(self.arguments())
        self.assertEqual(prepare.call_args.kwargs['verified_executable_hashes'], self.hashes())

    def test_manifest_uses_exact_verified_program_bytes(self):
        verified = self.hashes()
        destination = self.root/'out'
        delivery.prepare(self.build, self.models, destination, version='fixture.2', mode='update',
                         updater_exe=self.helper, schema_id='legacy-unversioned', executor='standalone',
                         verified_executable_hashes=verified)
        manifest = json.loads((destination/'update-manifest.json').read_text(encoding='utf-8'))
        actual = {row['path']: row['sha256'] for row in manifest['files']}
        self.assertTrue(all(actual[path] == value for path, value in verified.items()))
        self.assertEqual((self.models/'Qwen3-4B/model.gguf').read_bytes(), b'FAKE-MODEL')

    def test_changed_main_or_incomplete_check_stops_before_destination(self):
        good = self.hashes()
        for values in ({**good, 'PocketMemory.exe': '0'*64}, {'PocketMemory.exe': good['PocketMemory.exe']}):
            with self.subTest(values=values):
                with self.assertRaisesRegex(ValueError, '签名核验后'):
                    delivery.prepare(self.build, self.models, self.root/'out', version='fixture.2', mode='update',
                                     updater_exe=self.helper, schema_id='legacy-unversioned', executor='standalone',
                                     verified_executable_hashes=values)
                self.assertFalse((self.root/'out').exists())

    def test_valid_mocked_gate_preserves_endpoint_unverified_status(self):
        result = self.run_mocked_signature_check(self.report())
        self.assertTrue(result['passed'])
        self.assertFalse(result['endpoint_policy_verified'])

    def test_invalid_signature_reports_fail_closed(self):
        for field, value in [('passed', False), ('signature_status', 'NotSigned'),
                             ('timestamped', False), ('signer_thumbprint', 'B'*40),
                             ('sha256', '0'*64)]:
            with self.subTest(field=field):
                report = self.report()
                report['files'][0][field] = value
                with self.assertRaises(ValueError):
                    self.run_mocked_signature_check(report)
        report = self.report()
        report['files'][1] = copy.deepcopy(report['files'][0])
        with self.assertRaises(ValueError):
            self.run_mocked_signature_check(report)

    def test_checker_failure_does_not_trust_success_shaped_report(self):
        with self.assertRaises(ValueError):
            self.run_mocked_signature_check(self.report(), returncode=2)

    def test_bytes_changed_during_signature_check_are_rejected(self):
        with self.assertRaises(ValueError):
            self.run_mocked_signature_check(self.report(), mutate=lambda: self.helper.write_bytes(b'changed'))

    def test_invalid_publisher_never_starts_process(self):
        with mock.patch.object(signatures.subprocess, 'run') as execute:
            with self.assertRaises(ValueError):
                signatures.verify_release_signatures(self.build/'PocketMemory.exe', 'any signer', self.helper)
            execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
