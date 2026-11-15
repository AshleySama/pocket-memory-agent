# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Focused worker repair checks. Only the designated batch runner executes these."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("worker_repair_fixture", ROOT / "scripts/repair_installed_worker.py")
repair = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repair)


def digest(value):
    return hashlib.sha256(value).hexdigest()


class WorkerRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pm-worker-repair-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        environment = dict(os.environ)
        environment.pop("PM_UPDATE_CONFIG_PATH", None)
        environment["LOCALAPPDATA"] = str(self.base / "profile")
        self.env = mock.patch.dict(os.environ, environment, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.processes = mock.patch.object(repair.SafeUpdater, "_not_running", return_value=None)
        self.not_running = self.processes.start()
        self.addCleanup(self.processes.stop)
        self.install = self.base / "installation"
        self.target = self.install / "_internal" / repair.WORKER_NAME
        self.target.parent.mkdir(parents=True)
        self.old, self.new = b"known old worker fixture", b"fixed worker fixture"
        self.target.write_bytes(self.old)
        self.resources = self.base / "embedded-resources"
        self.resources.mkdir()
        self.payload = self.resources / repair.WORKER_NAME
        self.payload.write_bytes(self.new)
        self.manifest = {"schema_version": 1, "worker_sha256": digest(self.new),
                         "allowed_previous_sha256": [digest(self.old), digest(b"other supported worker")]}
        (self.resources / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        self.originals = {
            "PocketMemory.exe": b"original main application",
            "_internal/runtime.dll": b"original runtime",
            "models/weights.bin": b"original model",
            "data/notes/note.md": b"original note",
            "config.json": json.dumps({"data_dir": str(self.install / "data")}).encode(),
        }
        for name, content in self.originals.items():
            path = self.install / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.state = self.install / ".pocket-memory-update"

    def run_repair(self, **kwargs):
        return repair.repair_installed_worker(self.install, resource_dir=self.resources, **kwargs)

    def assert_original_files(self):
        for name, content in self.originals.items():
            self.assertEqual((self.install / name).read_bytes(), content, name)

    def test_known_old_worker_is_backed_up_before_atomic_replacement(self):
        real_replace = os.replace
        observed = []
        def replace(source, target):
            if Path(target) == self.target:
                attempt = Path(source).parent
                self.assertEqual((attempt / "original-worker.exe").read_bytes(), self.old)
                self.assertEqual(json.loads((attempt / "repair.json").read_text("utf-8"))["state"], "prepared")
                observed.append(Path(source).name)
            return real_replace(source, target)
        with mock.patch.object(repair.os, "replace", side_effect=replace):
            result = self.run_repair()
        self.assertEqual(result["state"], "repaired")
        self.assertEqual(observed, ["replacement.tmp"])
        self.assertEqual(self.target.read_bytes(), self.new)
        self.assertEqual(Path(result["backup"]).read_bytes(), self.old)
        self.assert_original_files()

    def test_already_fixed_is_idempotent(self):
        first = self.run_repair()
        attempts = list((self.state / repair.RESOURCE_FOLDER).iterdir())
        second = self.run_repair()
        self.assertEqual(second["state"], "already_repaired")
        self.assertEqual(list((self.state / repair.RESOURCE_FOLDER).iterdir()), attempts)
        self.assertEqual(Path(first["backup"]).read_bytes(), self.old)
        self.assert_original_files()

    def test_unknown_worker_and_bad_embedded_payload_are_refused(self):
        self.target.write_bytes(b"unknown worker")
        with self.assertRaisesRegex(repair.UpdateError, "不属于"):
            self.run_repair()
        self.assertEqual(self.target.read_bytes(), b"unknown worker")
        self.target.write_bytes(self.old)
        self.payload.write_bytes(b"damaged embedded payload")
        with self.assertRaisesRegex(repair.UpdateError, "校验失败"):
            self.run_repair()
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertFalse((self.state / repair.RESOURCE_FOLDER).exists())
        self.assert_original_files()

    def test_manifest_cannot_supply_arbitrary_paths_or_invalid_digests(self):
        bad_values = [
            {**self.manifest, "worker_file": "../PocketMemory.exe"},
            {**self.manifest, "worker_sha256": "../not-a-digest"},
            {**self.manifest, "allowed_previous_sha256": []},
            {**self.manifest, "schema_version": True},
        ]
        for value in bad_values:
            with self.subTest(value=value), self.assertRaises(repair.UpdateError):
                self.run_repair(manifest=value)
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assert_original_files()

    def test_running_application_or_unreadable_process_blocks_even_idempotence(self):
        self.not_running.side_effect = repair.UpdateError("Cannot inspect a running PocketMemory.exe")
        for value in (self.old, self.new):
            self.target.write_bytes(value)
            with self.assertRaisesRegex(repair.UpdateError, "running"):
                self.run_repair()
            self.assertEqual(self.target.read_bytes(), value)

    def test_unfinished_or_malformed_update_transaction_blocks(self):
        self.state.mkdir()
        journal = self.state / "journal.json"
        for state in ("prepared", "applying", "pending_health", "rolling_back"):
            value = {"schema_version": 1, "transaction": "a" * 32, "state": state, "operations": []}
            journal.write_text(json.dumps(value), encoding="utf-8")
            with self.subTest(state=state), self.assertRaisesRegex(repair.UpdateError, "尚未完成"):
                self.run_repair()
        journal.write_text("{}", encoding="utf-8")
        with self.assertRaises(repair.UpdateError):
            self.run_repair()
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assert_original_files()

    def test_finished_update_transaction_is_preserved(self):
        self.state.mkdir()
        journal = self.state / "journal.json"
        original = json.dumps({"schema_version": 1, "transaction": "a" * 32,
                               "state": "committed", "operations": []}).encode()
        journal.write_bytes(original)
        self.run_repair()
        self.assertEqual(journal.read_bytes(), original)

    def test_configured_library_overlap_is_refused(self):
        config = self.install / "config.json"
        original = json.dumps({"data_dir": str(self.target.parent)}).encode()
        config.write_bytes(original)
        with self.assertRaisesRegex(repair.UpdateError, "configured data/config"):
            self.run_repair()
        self.assertEqual(config.read_bytes(), original)
        self.assertEqual(self.target.read_bytes(), self.old)

    def test_hardlinked_worker_is_refused(self):
        outside = self.base / "linked-worker.exe"
        try:
            os.link(self.target, outside)
        except OSError as exc:
            self.skipTest(f"hardlinks unavailable: {exc}")
        with self.assertRaisesRegex(repair.UpdateError, "Hardlinked"):
            self.run_repair()
        self.assertEqual(outside.read_bytes(), self.old)

    def test_linked_installation_is_refused_before_resolution(self):
        link = self.base / "installation-link"
        try:
            os.symlink(self.install, link, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        with self.assertRaisesRegex(repair.UpdateError, "Symlink/junction/reparse"):
            repair.repair_installed_worker(link, resource_dir=self.resources)
        self.assertEqual(self.target.read_bytes(), self.old)

    def test_replace_failure_before_or_after_rename_restores_original(self):
        real_replace = os.replace
        for fail_after in (False, True):
            with self.subTest(fail_after=fail_after):
                def replace(source, target):
                    if Path(target) == self.target and Path(source).name == "replacement.tmp":
                        if fail_after:
                            real_replace(source, target)
                        raise OSError("injected replacement failure")
                    return real_replace(source, target)
                with mock.patch.object(repair.os, "replace", side_effect=replace):
                    with self.assertRaisesRegex(repair.UpdateError, "未写入新组件或已恢复"):
                        self.run_repair()
                self.assertEqual(self.target.read_bytes(), self.old)
                self.assert_original_files()
        backups = list((self.state / repair.RESOURCE_FOLDER).glob("*/original-worker.exe"))
        self.assertEqual(len(backups), 2)
        self.assertTrue(all(path.read_bytes() == self.old for path in backups))

    def test_failed_restore_reports_durable_backup(self):
        real_replace = os.replace
        def replace(source, target):
            if Path(target) == self.target:
                if Path(source).name == "replacement.tmp":
                    real_replace(source, target)
                raise OSError("injected replacement and restoration failure")
            return real_replace(source, target)
        with mock.patch.object(repair.os, "replace", side_effect=replace):
            with self.assertRaisesRegex(repair.UpdateError, "自动恢复未完成.*原组件备份"):
                self.run_repair()
        backups = list((self.state / repair.RESOURCE_FOLDER).glob("*/original-worker.exe"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.old)
        self.assert_original_files()

    def test_concurrently_changed_worker_is_not_overwritten(self):
        real_copy = repair._copy_verified
        def copy(source, destination, expected):
            real_copy(source, destination, expected)
            if destination.name == "replacement.tmp":
                self.target.write_bytes(b"concurrent unknown worker")
        with mock.patch.object(repair, "_copy_verified", side_effect=copy):
            with self.assertRaises(repair.UpdateError):
                self.run_repair()
        self.assertEqual(self.target.read_bytes(), b"concurrent unknown worker")
        self.assert_original_files()

    def test_quiet_cli_emits_only_json_and_no_message_box(self):
        output = io.StringIO()
        with mock.patch.object(repair, "repair_installed_worker", return_value={"state": "repaired", "message": "完成"}) as run, \
             mock.patch.object(repair, "_show_message") as message, contextlib.redirect_stdout(output):
            status = repair.main(["--install-dir", str(self.install), "--quiet"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["state"], "repaired")
        run.assert_called_once_with(self.install)
        message.assert_not_called()

    def request(self, name="job", *, install=None, config=None, data=None):
        temp = self.base / "temporary"
        path = temp / ("PocketMemory-update-" + name) / "request.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        install = install or self.install
        path.write_text(json.dumps({"schema_version": 1, "install_dir": str(install),
                                   "config_path": str(config or install / "config.json"),
                                   "data_dir": str(data or install / "data"), "port": 6778}), encoding="utf-8")
        return path

    def discover(self, **kwargs):
        return repair.discover_installation(
            adjacent_dir=self.resources, roots=[self.base / "temporary"], manifest=self.manifest,
            allowed_main_sha256={digest(self.originals["PocketMemory.exe"])}, **kwargs)

    def test_discovery_uses_original_request_config_and_deduplicates_jobs(self):
        external = self.base / "selected-profile.json"
        contents = json.dumps({"data_dir": str(self.install / "data"), "theme": "dark"}).encode()
        external.write_bytes(contents)
        first = self.request("first", config=external)
        self.request("second", config=external)
        os.utime(first, (1, 1))  # Old request is not eligible.
        self.request("third", config=external)
        selected = self.discover()
        self.assertEqual(Path(selected["install_dir"]), self.install)
        self.assertEqual(Path(selected["config_path"]), external)
        self.assertEqual(Path(selected["data_dir"]), self.install / "data")
        self.assertEqual(external.read_bytes(), contents)
        self.assertFalse(self.state.exists())
        self.assert_original_files()

    def test_discovery_rejects_multiple_installations_but_explicit_location_disambiguates(self):
        self.request("first")
        other = self.base / "other-install"
        repair.shutil.copytree(self.install, other)
        (other / "config.json").write_text(json.dumps({"data_dir": str(other / "data")}), encoding="utf-8")
        self.request("other", install=other)
        with self.assertRaisesRegex(repair.UpdateError, "多个安装"):
            self.discover()
        selected = self.discover(install_dir=self.install)
        self.assertEqual(Path(selected["install_dir"]), self.install)

    def test_multiple_configs_for_same_install_are_not_guessed(self):
        self.request("first")
        config = self.base / "other-config.json"
        config.write_text(json.dumps({"data_dir": str(self.install / "data")}), encoding="utf-8")
        self.request("second", config=config)
        with self.assertRaisesRegex(repair.UpdateError, "多个安装目录或资料配置"):
            self.discover(install_dir=self.install)

    def test_invalid_request_unknown_main_and_oversized_request_never_select_install(self):
        path = self.request()
        original = path.read_bytes()
        path.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(repair.UpdateError, "没有找到"):
            self.discover()
        path.write_bytes(b" " * (64 * 1024 + 1))
        with self.assertRaisesRegex(repair.UpdateError, "没有找到"):
            self.discover()
        path.write_bytes(original)
        (self.install / "PocketMemory.exe").write_bytes(b"unknown executable")
        with self.assertRaisesRegex(repair.UpdateError, "没有找到"):
            self.discover()
        with mock.patch.dict(os.environ, {"PM_UPDATE_CONFIG_PATH": str(self.install / "config.json")}):
            with self.assertRaisesRegex(repair.UpdateError, "已核验版本"):
                self.discover(install_dir=self.install)
        self.assertEqual(self.target.read_bytes(), self.old)

    def test_discovery_rejects_unknown_worker_pending_transaction_and_network_paths(self):
        self.request()
        self.target.write_bytes(b"unknown worker")
        with self.assertRaisesRegex(repair.UpdateError, "不属于"):
            self.discover()
        self.target.write_bytes(self.old)
        self.state.mkdir()
        (self.state / "journal.json").write_text(json.dumps({"schema_version": 1,
            "transaction": "a" * 32, "state": "applying", "operations": []}), encoding="utf-8")
        with self.assertRaisesRegex(repair.UpdateError, "尚未完成"):
            self.discover()
        for value in (r"\\server\share\app", "relative/path", ""):
            with self.subTest(path=value), self.assertRaises(repair.UpdateError):
                repair._local_path(value)

    def test_discovery_limit_fails_closed_instead_of_picking_subset(self):
        self.request("first")
        self.request("second")
        with mock.patch.object(repair, "MAX_REQUESTS", 1):
            with self.assertRaisesRegex(repair.UpdateError, "过多原更新记录"):
                self.discover()

    @unittest.skipUnless(os.name == "nt", "Windows 8.3 canonical identity")
    def test_short_temp_alias_and_long_path_share_one_identity(self):
        import ctypes
        self.request("first")
        path = self.base / "temporary"
        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer))
        if not length or "~" not in buffer.value:
            self.skipTest("8.3 aliases unavailable on test volume")
        requests = repair._recent_requests([str(path), buffer.value])
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0], path / "PocketMemory-update-first" / "request.json")
        length = ctypes.windll.kernel32.GetShortPathNameW(str(self.install), buffer, len(buffer))
        if length and "~" in buffer.value:
            self.request("short-install", install=Path(buffer.value))
            self.assertEqual(Path(self.discover()["install_dir"]), self.install)

    def test_bootstrap_prompts_then_only_normal_quit_repairs_and_reopens_original(self):
        self.request()
        context = self.discover()
        process = mock.Mock(pid=1234)
        process.wait.return_value = 0
        with mock.patch.object(repair, "_confirm_close", return_value=True) as confirm, \
             mock.patch.object(repair, "running_install_processes", side_effect=[[91], []]), \
             mock.patch.object(repair.subprocess, "Popen", return_value=process) as launch:
            result = repair.bootstrap_original(context, resource_dir=self.resources,
                allowed_main_sha256={digest(self.originals["PocketMemory.exe"])})
        self.assertEqual(result["state"], "repaired")
        self.assertEqual(result["reopened_pid"], 1234)
        confirm.assert_called_once_with(context)
        self.assertEqual(launch.call_count, 2)
        stop, reopen = launch.call_args_list
        self.assertEqual(stop.args[0], [str(self.install / "PocketMemory.exe"), "--quit", "--data-dir", str(self.install / "data")])
        self.assertEqual(reopen.args[0][0], str(self.install / "PocketMemory.exe"))
        self.assertNotIn("--quit", reopen.args[0])
        for call in (stop, reopen):
            self.assertEqual(call.kwargs["env"]["PM_UPDATE_CONFIG_PATH"], str(self.install / "config.json"))
            self.assertEqual(call.kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        process.kill.assert_not_called()
        process.terminate.assert_not_called()
        self.assert_original_files()

    def test_cancelled_bootstrap_and_unknown_process_state_make_no_changes(self):
        self.request()
        context = self.discover()
        with mock.patch.object(repair, "_confirm_close", return_value=False), \
             mock.patch.object(repair.subprocess, "Popen") as launch:
            result = repair.bootstrap_original(context, resource_dir=self.resources,
                allowed_main_sha256={digest(self.originals["PocketMemory.exe"])})
        self.assertEqual(result["state"], "cancelled")
        launch.assert_not_called()
        with mock.patch.object(repair, "_confirm_close", return_value=True), \
             mock.patch.object(repair, "running_install_processes", side_effect=repair.UpdateError("unknown process identity")), \
             mock.patch.object(repair.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(repair.UpdateError, "unknown process"):
                repair.bootstrap_original(context, resource_dir=self.resources,
                    allowed_main_sha256={digest(self.originals["PocketMemory.exe"])})
        launch.assert_not_called()
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertFalse(self.state.exists())
        self.assert_original_files()

    def test_normal_quit_timeout_never_force_terminates_or_repairs(self):
        self.request()
        context = self.discover()
        process = mock.Mock()
        process.wait.side_effect = repair.subprocess.TimeoutExpired("PocketMemory --quit", 30)
        with mock.patch.object(repair, "_confirm_close", return_value=True), \
             mock.patch.object(repair, "running_install_processes", return_value=[91]), \
             mock.patch.object(repair.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(repair.UpdateError, "正常退出后台服务超时"):
                repair.bootstrap_original(context, resource_dir=self.resources,
                    allowed_main_sha256={digest(self.originals["PocketMemory.exe"])})
        process.kill.assert_not_called()
        process.terminate.assert_not_called()
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assert_original_files()

    def test_launch_environment_does_not_reuse_repair_extraction_directory(self):
        self.request()
        context = self.discover()
        with mock.patch.dict(os.environ, {"_PYI_APPLICATION_HOME_DIR": "repair-temp", "_PYI_ARCHIVE_FILE": "repair.exe",
                                          "PM_UPDATE_READY_TOKEN": "old-token", "PM_UPDATE_DESKTOP_TOKEN": "old-desktop-token",
                                          "PM_UPDATE_HEALTH_TOKEN": "old-health-token", "PM_UPDATE_PROBE": "1"}):
            env = repair._launch_environment(context)
        self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertNotIn("_PYI_APPLICATION_HOME_DIR", env)
        self.assertNotIn("_PYI_ARCHIVE_FILE", env)
        self.assertNotIn("PM_UPDATE_READY_TOKEN", env)
        self.assertNotIn("PM_UPDATE_DESKTOP_TOKEN", env)
        self.assertNotIn("PM_UPDATE_HEALTH_TOKEN", env)
        self.assertNotIn("PM_UPDATE_PROBE", env)


if __name__ == "__main__":
    unittest.main()
