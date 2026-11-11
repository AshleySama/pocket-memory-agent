# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Bounded automatic-update tests: tiny fixtures, fake network/processes only."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import time
import types
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
CANDIDATE = ROOT / "."
sys.path.insert(0, str(CANDIDATE))
from pocket_memory import app_updates as updates
from pocket_memory.safe_update import UpdateError

SPEC = importlib.util.spec_from_file_location("pm_test_update_worker", CANDIDATE / "scripts/update_worker.py")
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


def record(path, data):
    return {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class Response(io.BytesIO):
    pass


class AppUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pm-auto-fixture-")
        self.base = Path(self.temp.name)
        self.environment = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.base / "profile")})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def feed(self, version="1.2.1", payload=b"tiny zip"):
        return {"schema_version": 1, "app_id": "pocket-memory", "published": True,
                "version": version, "notes": "修复候选程序问题。",
                "package": {"url": f"https://{updates.TRUSTED_HOST}/PocketMemory-1.2.1-update.zip",
                            "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}}

    def manifest(self):
        files = {"PocketMemory.exe": b"fixture new exe", "_internal/runtime.bin": b"fixture new runtime"}
        return files, {"schema_version": 1, "app_id": "pocket-memory", "kind": "program-update", "version": "1.2.1",
                       "files": [record(path, data) for path, data in files.items()],
                       "model_requirements": [record("bge/model.bin", b"tiny model fixture")],
                       "data_compat": {"accepted_schema_ids": ["legacy-unversioned"],
                                       "target_schema_id": "legacy-unversioned", "migration": "none"}}

    def zip_fixture(self, *, extra=None, wrap="", mutation=None):
        files, manifest = self.manifest()
        if mutation:
            mutation(manifest, files)
        archive = self.base / ("archive-" + str(time.monotonic_ns()) + ".zip")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr(wrap + "update-manifest.json", json.dumps(manifest))
            for path, data in files.items():
                bundle.writestr(wrap + "payload/" + path, data)
            for path, data in (extra or []):
                bundle.writestr(path, data)
        return archive

    def installed_fixture(self):
        install = self.base / "install"
        source = self.base / "handoff/source"
        put(install / "PocketMemory.exe", b"fixture old exe")
        put(install / "_internal/runtime.bin", b"fixture old runtime")
        put(install / "_internal/models/bge/model.bin", b"tiny model fixture")
        data = install / "data-beta"
        put(data / "note.txt", b"user data remains")
        config = install / "beta-profile/PocketMemory/config.json"
        put(config, json.dumps({"data_dir": str(data)}).encode())
        files, manifest = self.manifest()
        for path, value in files.items():
            put(source / "payload" / path, value)
        put(source / "update-manifest.json", json.dumps(manifest).encode())
        request = {"schema_version": 1, "install_dir": str(install), "data_dir": str(data), "config_path": str(config),
                   "source_dir": str(source), "target_version": "1.2.1", "parent_pid": 999999, "launcher_pid": 999998,
                   "port": 51322, "health_token": "a" * 64, "local_app_data": str(install / "beta-profile"), "debug": False}
        request_path = source.parent / "request.json"
        put(request_path, json.dumps(request).encode())
        put(source.parent / "go.json", json.dumps({"token": request["health_token"]}).encode())
        return install, source, request_path, request

    def test_semver_strictly_orders_beta_and_stable(self):
        values = ["1.2.0-beta.1", "1.2.0-beta.2", "1.2.0-rc.1", "1.2.0", "1.2.1", "2.0.0"]
        self.assertEqual(values, sorted(values, key=updates.version_key))
        self.assertEqual(updates.version_key("1.2.1+one"), updates.version_key("1.2.1+two"))
        for value in ("1.2", "01.2.3", "1.2.3-beta.01", "1.2.3-beta..1", "1.2.3+."):
            with self.assertRaises(UpdateError):
                updates.version_key(value)

    def test_feed_requires_explicit_published_integrity_and_no_hooks(self):
        value = self.feed()
        self.assertEqual(updates.validate_feed(value), value)
        for key, replacement in (("published", 1), ("schema_version", True), ("app_id", "other")):
            bad = dict(value, **{key: replacement})
            with self.assertRaises(UpdateError):
                updates.validate_feed(bad)
        with self.assertRaises(UpdateError):
            updates.validate_feed(dict(value, hook="something.exe"))
        with self.assertRaises(UpdateError):
            updates.validate_feed(dict(value, published=False))

    def test_trusted_host_https_path_and_redirect_restrictions(self):
        for url in ("http://" + updates.TRUSTED_HOST + "/PocketMemory-x.zip",
                    "https://evil.example/PocketMemory-x.zip",
                    "https://" + updates.TRUSTED_HOST + ".evil/PocketMemory-x.zip",
                    "https://user@" + updates.TRUSTED_HOST + "/PocketMemory-x.zip",
                    "https://" + updates.TRUSTED_HOST + ":443/PocketMemory-x.zip",
                    "https://" + updates.TRUSTED_HOST + "/PocketMemory-../x.zip?token=bad",
                    "https://" + updates.TRUSTED_HOST + "/../PocketMemory-x.zip"):
            with self.assertRaises(UpdateError):
                updates.trusted_url(url, package=True)
        with self.assertRaises(UpdateError):
            updates._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example")

    def test_feed_404_is_unpublished_not_latest(self):
        failure = urllib.error.HTTPError(updates.FEED_URL, 404, "missing", {}, None)
        with mock.patch.object(updates, "_open_https", side_effect=failure):
            self.assertFalse(updates.fetch_feed()["published"])
        with mock.patch.object(updates, "_open_https", return_value=Response(b"x" * (updates.MAX_FEED_BYTES + 1))):
            with self.assertRaises(UpdateError):
                updates.fetch_feed()

    def test_download_verifies_size_sha_and_progress(self):
        payload = b"fixture bytes"
        package = self.feed(payload=payload)["package"]
        progress = []
        with mock.patch.object(updates, "_open_https", return_value=Response(payload)):
            updates.download_package(package, self.base / "ok.zip", lambda a, b: progress.append((a, b)))
        self.assertEqual(progress[-1], (len(payload), len(payload)))
        for index, bad in enumerate((payload + b"extra", payload[:-1], b"wrong payload")):
            with mock.patch.object(updates, "_open_https", return_value=Response(bad)):
                with self.assertRaises(UpdateError):
                    updates.download_package(package, self.base / f"bad{index}.zip")

    def test_valid_archive_optional_single_wrapper_and_never_execute_helper(self):
        for index, wrapper in enumerate(("", "PocketMemory-release/")):
            archive = self.zip_fixture(wrap=wrapper, extra=[(wrapper + "PocketMemoryUpdate.exe", b"ignored helper")])
            dest = self.base / f"extracted{index}"
            updates.extract_update_archive(archive, dest, "1.2.1")
            self.assertTrue((dest / "payload/PocketMemory.exe").is_file())
            self.assertFalse((dest / "PocketMemoryUpdate.exe").exists())

    def test_archive_traversal_ads_device_alias_extra_models_refused(self):
        for index, path in enumerate(("../outside", "payload/_internal/foo:ads", "payload/_internal/CON.txt",
                                      "payload/_internal/FOO~1/a", "models/weights.gguf", "payload/_internal/models/model.bin")):
            archive = self.zip_fixture(extra=[(path, b"bad")])
            with self.assertRaises(UpdateError):
                updates.extract_update_archive(archive, self.base / f"bad{index}", "1.2.1")
            self.assertFalse((self.base / f"bad{index}").exists())

    def test_archive_symlink_and_parent_case_alias_refused(self):
        link = zipfile.ZipInfo("payload/_internal/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        for index, extra in enumerate(([(link, b"../../outside")],
                                      [("payload/_internal/RUNTIME.bin/child", b"x")],
                                      [("payload/_internal/runtime.bin/child", b"x")])):
            archive = self.zip_fixture(extra=extra)
            with self.assertRaises(UpdateError):
                updates.extract_update_archive(archive, self.base / f"link{index}", "1.2.1")

    def test_archive_version_hash_and_gguf_payload_rejected(self):
        archive = self.zip_fixture()
        with self.assertRaises(UpdateError):
            updates.extract_update_archive(archive, self.base / "bad-version", "9.0.0")
        def tamper(manifest, files):
            files["PocketMemory.exe"] = b"fixture evil exe"
        with self.assertRaises(UpdateError):
            updates.extract_update_archive(self.zip_fixture(mutation=tamper), self.base / "bad-hash", "1.2.1")
        def model(manifest, files):
            files["_internal/weight.gguf"] = b"model data"
            manifest["files"].append(record("_internal/weight.gguf", b"model data"))
        with self.assertRaises(UpdateError):
            updates.extract_update_archive(self.zip_fixture(mutation=model), self.base / "bad-model", "1.2.1")

    def test_archive_resource_limit(self):
        archive = self.zip_fixture()
        with mock.patch.object(updates, "MAX_ARCHIVE_FILES", 2):
            with self.assertRaises(UpdateError):
                updates.extract_update_archive(archive, self.base / "too-many", "1.2.1")

    def test_source_preview_never_starts_install(self):
        config = types.SimpleNamespace(path=self.base / "config.json", data_dir=self.base / "data")
        service = updates.AppUpdateService(config, config.data_dir, server_port=1234, exit_callback=mock.Mock())
        self.assertTrue(service.status()["preview"])
        with self.assertRaisesRegex(UpdateError, "不支持安装更新"):
            service.start_update()
        service.exit_callback.assert_not_called()

    def test_frozen_app_handoff_reaches_worker_staging_after_real_preflight(self):
        install, _, _, request = self.installed_fixture()
        helper = install / updates.WORKER_RELATIVE_PATH
        put(helper, b"fixture worker, never executed")
        archive = self.zip_fixture()
        offered = self.feed(payload=archive.read_bytes())
        config = types.SimpleNamespace(path=Path(request["config_path"]), data_dir=Path(request["data_dir"]))
        work = self.base / "read-only-preflight"
        work.mkdir()
        original = {str(p.relative_to(install)): p.read_bytes() for p in install.rglob('*') if p.is_file()}
        with mock.patch.object(updates.sys, "frozen", True, create=True), \
                mock.patch.object(updates.sys, "executable", str(install / "PocketMemory.exe")), \
                mock.patch("pocket_memory.safe_update.running_install_processes", return_value=[os.getpid()]), \
                mock.patch.object(updates, "fetch_feed", return_value=offered), \
                mock.patch.object(updates.tempfile, "mkdtemp", return_value=str(work)), \
                mock.patch.object(updates, "download_package", side_effect=lambda package, destination, progress: destination.write_bytes(archive.read_bytes())), \
                mock.patch.object(updates.shutil, "copy2", side_effect=RuntimeError("stop after real preflight")) as copy_worker:
            service = updates.AppUpdateService(config, config.data_dir, server_port=1234,
                                              exit_callback=mock.Mock(), current_version="1.2.0")
            with self.assertRaisesRegex(RuntimeError, "stop after real preflight"):
                service._download_and_handoff(offered, (config.data_dir, config.path, config.data_dir))
            copy_worker.assert_called_once_with(helper, work / "PocketMemoryUpdateWorker.exe")
            service.exit_callback.assert_not_called()
        for relative, content in original.items():
            self.assertEqual((install / relative).read_bytes(), content)
        self.assertFalse((install / "models").exists())
        self.assertFalse((install / ".pocket-memory-update/journal.json").exists())

    def test_service_checks_unpublished_and_newer_with_consistent_version(self):
        config = types.SimpleNamespace(path=self.base / "config.json", data_dir=self.base / "data")
        service = updates.AppUpdateService(config, config.data_dir, server_port=1234, exit_callback=mock.Mock(), current_version="1.2.0-beta.2")
        for feed, state in (({"schema_version": 1, "app_id": "pocket-memory", "published": False}, "unpublished"),
                            (self.feed("1.2.0-beta.1"), "current"), (self.feed("1.2.0"), "available")):
            with mock.patch.object(updates, "fetch_feed", return_value=feed):
                service.check_async()
                deadline = time.monotonic() + 2
                while service._busy and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(service.status()["state"], state)
                self.assertFalse(service.status()["can_update"])

    def test_old_process_timeout_never_terminates(self):
        _, _, _, request = self.installed_fixture()
        with mock.patch.object(worker, "_parent_alive", return_value=True), mock.patch.object(worker, "running_install_processes", return_value=[22]):
            with self.assertRaisesRegex(UpdateError, "没有强制"):
                worker.wait_for_old_exit(request, timeout=0)

    def test_worker_request_and_restart_preserve_beta_config_and_data(self):
        _, _, path, request = self.installed_fixture()
        self.assertEqual(worker.load_request(path), request)
        command = worker.restart_command(request, probe=True)
        self.assertIn("--server-only", command)
        self.assertEqual(command[command.index("--data-dir") + 1], request["data_dir"])
        environment = worker.restart_environment(request, probe=True)
        self.assertEqual(environment["LOCALAPPDATA"], request["local_app_data"])
        self.assertEqual(environment["PM_UPDATE_CONFIG_PATH"], request["config_path"])
        self.assertEqual(environment["PM_UPDATE_HEALTH_TOKEN"], request["health_token"])
        self.assertNotIn("PM_UPDATE_HEALTH_TOKEN", worker.restart_environment(request))
        request["source_dir"] = str(self.base / "unrelated")
        path.write_text(json.dumps(request))
        with self.assertRaises(UpdateError):
            worker.load_request(path)

    def test_windows_executable_minimal_headers_and_corruption(self):
        # Header bytes only, deliberately never executed or sent to a loader.
        valid = bytearray(64 + 24 + 112)
        valid[:2] = b'MZ'
        valid[0x3C:0x40] = (64).to_bytes(4, 'little')
        valid[64:68] = b'PE\0\0'
        valid[68:70] = (0x8664).to_bytes(2, 'little')
        valid[84:86] = (112).to_bytes(2, 'little')
        valid[88:90] = (0x20B).to_bytes(2, 'little')
        path = self.base / 'header-fixture.exe'
        path.write_bytes(valid)
        worker.validate_windows_executable(path)

        def changed(offset, data):
            result = bytearray(valid)
            result[offset:offset + len(data)] = data
            return result

        variants = {
            'text_instead_of_PE': b'Intentional acceptance fixture: invalid executable, never publish.\r\n',
            'truncated_DOS': valid[:63],
            'bad_MZ': changed(0, b'NZ'),
            'offset_inside_DOS': changed(0x3C, (32).to_bytes(4, 'little')),
            'offset_past_EOF': changed(0x3C, (0xFFFFFFFF).to_bytes(4, 'little')),
            'truncated_COFF': valid[:64 + 23],
            'bad_PE_signature': changed(64, b'PX\0\0'),
            'x86_machine': changed(68, (0x14C).to_bytes(2, 'little')),
            'PE32_magic': changed(88, (0x10B).to_bytes(2, 'little')),
            'too_short_optional_header': changed(84, (111).to_bytes(2, 'little')),
            'optional_header_past_EOF': changed(84, (0xFFFF).to_bytes(2, 'little')),
            'truncated_optional_header': valid[:-1],
        }
        for name, data in variants.items():
            with self.subTest(name=name):
                path.write_bytes(data)
                with self.assertRaises(UpdateError):
                    worker.validate_windows_executable(path)
        with self.assertRaises(UpdateError):
            worker.validate_windows_executable(self.base / 'missing.exe')

    def test_invalid_windows_executable_never_reaches_create_process(self):
        _, _, path, request = self.installed_fixture()
        # Isolate the platform branch without changing pathlib's global platform.
        with mock.patch.object(worker, 'os', types.SimpleNamespace(name='nt', fstat=os.fstat)), \
                mock.patch.object(worker.subprocess, 'Popen') as create_process:
            with self.assertRaises(UpdateError):
                worker.spawn_application(request, path.parent, probe=True)
            create_process.assert_not_called()
        self.assertFalse((path.parent / 'health-probe.log').exists())

    def test_health_requires_token_version_pid_data_and_config(self):
        _, _, _, request = self.installed_fixture()
        process = mock.Mock(pid=123)
        process.poll.return_value = None
        good = {"ok": True, "pid": 123, "token": request["health_token"], "app_version": "1.2.1",
                "data_dir": request["data_dir"], "config_path": request["config_path"]}
        with mock.patch.object(worker, "_local_request", return_value=Response(json.dumps(good).encode())):
            self.assertEqual(worker.verify_health(request, process, timeout=.05), good)
        for field, value in (("token", "bad"), ("app_version", "1.2.0"), ("pid", 99),
                             ("data_dir", str(self.base)), ("config_path", str(self.base / "other.json"))):
            bad = dict(good, **{field: value})
            with mock.patch.object(worker, "_local_request", side_effect=lambda *a, **k: Response(json.dumps(bad).encode())), mock.patch.object(worker.time, "sleep"):
                with self.assertRaises(UpdateError):
                    worker.verify_health(request, process, timeout=.002)

    def test_failed_identity_never_shuts_down_unrelated_listener(self):
        _, _, _, request = self.installed_fixture()
        process = mock.Mock(pid=123)
        process.poll.return_value = None
        with mock.patch.object(worker, "_local_request", return_value=Response(b'{"pid":999,"token":"wrong"}')) as http:
            worker.stop_owned_probe(request, process)
        self.assertEqual([call.args[1] for call in http.call_args_list], ["/api/update-health"])
        process.terminate.assert_called_once()

    def test_worker_success_uses_real_transaction_with_fake_process_health(self):
        install, _, path, request = self.installed_fixture()
        process = mock.Mock(pid=123)
        with mock.patch.object(worker, "wait_for_old_exit"), mock.patch.object(worker, "spawn_application", return_value=process) as spawn, mock.patch.object(worker, "verify_health", return_value={}), mock.patch.object(worker, "stop_owned_probe"):
            result = worker.run_worker(path)
        self.assertEqual(result["state"], "complete")
        self.assertEqual((install / "PocketMemory.exe").read_bytes(), b"fixture new exe")
        self.assertEqual((install / "data-beta/note.txt").read_bytes(), b"user data remains")
        self.assertEqual(spawn.call_count, 2)
        self.assertTrue(spawn.call_args_list[0].kwargs["probe"])
        self.assertEqual(json.loads((install / ".pocket-memory-update/journal.json").read_text(encoding='utf-8'))["state"], "committed")

    def test_worker_failed_health_rolls_back_and_restarts_old_only(self):
        install, _, path, _ = self.installed_fixture()
        with mock.patch.object(worker, "wait_for_old_exit"), mock.patch.object(worker, "spawn_application", return_value=mock.Mock(pid=123)) as spawn, mock.patch.object(worker, "verify_health", side_effect=UpdateError("wrong new version")), mock.patch.object(worker, "stop_owned_probe"):
            result = worker.run_worker(path)
        self.assertEqual(result["state"], "rolled_back")
        self.assertEqual((install / "PocketMemory.exe").read_bytes(), b"fixture old exe")
        self.assertTrue((install / "_internal/models/bge/model.bin").is_file())
        self.assertFalse((install / "models").exists())
        self.assertEqual((install / "data-beta/note.txt").read_bytes(), b"user data remains")
        self.assertEqual(spawn.call_count, 2)

    def test_worker_old_exit_timeout_keeps_original_and_does_not_spawn(self):
        install, _, path, _ = self.installed_fixture()
        with mock.patch.object(worker, "wait_for_old_exit", side_effect=UpdateError("old still running")), mock.patch.object(worker, "spawn_application") as spawn:
            result = worker.run_worker(path)
        self.assertEqual(result["state"], "error")
        self.assertEqual((install / "PocketMemory.exe").read_bytes(), b"fixture old exe")
        spawn.assert_not_called()

    def test_unknown_pending_transaction_never_claims_rollback(self):
        install, source, path, _ = self.installed_fixture()
        worker.SafeUpdater(install).apply(source)
        with mock.patch.object(worker, "wait_for_old_exit"), mock.patch.object(worker, "spawn_application") as spawn:
            result = worker.run_worker(path)
        self.assertEqual(result["state"], "recovery_required")
        self.assertEqual(json.loads((install / ".pocket-memory-update/journal.json").read_text(encoding='utf-8'))["state"], "pending_health")
        self.assertEqual((install / "PocketMemory.exe").read_bytes(), b"fixture new exe")
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
