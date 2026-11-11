# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Focused safety tests. Uses temporary tiny fixtures, never a real installation.

Run only by the batch's designated runner:
  python -m unittest discover -s tests/release_current -p test_safe_update.py -v
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "pocket_memory/safe_update.py"
SPEC = importlib.util.spec_from_file_location("pocket_memory_safe_update_fixture", MODULE)
up = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(up)


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def entry(path, value):
    return {"path": path, "size": len(value), "sha256": hashlib.sha256(value).hexdigest()}


class Interrupted(BaseException):
    pass


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pm-update-fixture-")
        self.base = Path(self.temporary.name)
        self.env = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.base / "profile")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temporary.cleanup()

    def fixture(self, name="fixture", external=False, both=False):
        base = self.base / name
        install, source = base / "install", base / "update"
        put(install / "PocketMemory.exe", b"old exe")
        put(install / "_internal/runtime.bin", b"old runtime")
        put(install / "README.txt", b"old readme")
        put(install / "data/notes/keep.txt", b"user note")
        put(install / "custom-user-file.txt", b"unknown root must stay")
        put(install / "data/pocket_memory.db", b"not touched by updater")
        put(install / "config.json", json.dumps({"data_dir": str(install / "data")}).encode())
        model = b"tiny fixture model, not model weights"
        if external or both:
            put(install / "models/bge/model.bin", model)
        if not external or both:
            put(install / "_internal/models/bge/model.bin", model)
        files = {"PocketMemory.exe": b"new exe", "_internal/runtime.bin": b"new runtime",
                 "_internal/frontend/app.js": b"new frontend", "README.txt": b"new readme",
                 "licenses/example.txt": b"example attribution"}
        for path, value in files.items():
            put(source / "payload" / path, value)
        manifest = {"schema_version": 1, "app_id": "pocket-memory", "kind": "program-update",
                    "version": "1.3.0-fixture", "files": [entry(path, value) for path, value in files.items()],
                    "model_requirements": [entry("bge/model.bin", model)],
                    "data_compat": {"accepted_schema_ids": ["legacy-unversioned"],
                                    "target_schema_id": "legacy-unversioned", "migration": "none"}}
        self.write_manifest(source, manifest)
        return install, source, manifest

    def write_manifest(self, source, manifest):
        put(source / up.MANIFEST_NAME, json.dumps(manifest).encode())

    def assert_original(self, install, legacy=True):
        self.assertEqual((install / "PocketMemory.exe").read_bytes(), b"old exe")
        self.assertEqual((install / "_internal/runtime.bin").read_bytes(), b"old runtime")
        self.assertEqual((install / "README.txt").read_bytes(), b"old readme")
        if legacy:
            self.assertTrue((install / "_internal/models/bge/model.bin").is_file())
        self.assertEqual((install / "data/notes/keep.txt").read_bytes(), b"user note")
        self.assertEqual((install / "custom-user-file.txt").read_bytes(), b"unknown root must stay")
        self.assertEqual((install / "data/pocket_memory.db").read_bytes(), b"not touched by updater")

    def test_success_pending_health_and_idempotent_rollback(self):
        install, source, _ = self.fixture()
        updater = up.SafeUpdater(install)
        config = (install / "config.json").read_bytes()
        self.assertEqual(updater.check(source)["models_to_relocate"], 1)
        result = updater.apply(source)
        self.assertEqual(result["state"], "pending_health")
        self.assertFalse(result["health_verified"])
        self.assertTrue((install / "models/bge/model.bin").is_file())
        self.assertEqual((install / "_internal/runtime.bin").read_bytes(), b"new runtime")
        self.assertEqual(updater.recover()["state"], "pending_health")
        with self.assertRaises(up.UpdateError):
            updater.apply(source)
        self.assertEqual(updater.rollback()["state"], "rolled_back")
        self.assertEqual(updater.rollback()["state"], "rolled_back")
        self.assert_original(install)
        self.assertFalse((install / "models").exists())
        self.assertEqual((install / "config.json").read_bytes(), config)

    def test_external_models_commit_and_backup_retention(self):
        install, source, _ = self.fixture(external=True)
        updater = up.SafeUpdater(install)
        result = updater.apply(source)
        with self.assertRaises(up.UpdateError):
            updater.commit("")
        committed = updater.commit("Fixture caller attestation only; no real EXE executed")
        self.assertEqual(committed["health_verification"], "caller_attestation")
        self.assertTrue((install / up.STATE_DIR / result["transaction"] / "backup/PocketMemory.exe").is_file())
        updater.rollback()
        self.assert_original(install, legacy=False)
        self.assertTrue((install / "models/bge/model.bin").is_file())

    def test_executable_is_launch_barrier_and_restored_last(self):
        install, source, _ = self.fixture()
        updater = up.SafeUpdater(install)
        moves = []
        original = updater._move
        def observe(source_path, target_path):
            original(source_path, target_path)
            moves.append((source_path, target_path))
            # Between removing the old EXE and installing the new EXE, the
            # standard launch path must be absent throughout every other move.
            if source_path.name != "PocketMemory.exe" and target_path.name != "PocketMemory.exe":
                self.assertFalse((install / "PocketMemory.exe").exists())
        with mock.patch.object(updater, "_move", side_effect=observe):
            updater.apply(source)
        self.assertEqual(moves[0][0], install / "PocketMemory.exe")
        self.assertEqual(moves[-1][1], install / "PocketMemory.exe")
        moves.clear()
        with mock.patch.object(updater, "_move", side_effect=observe):
            updater.rollback()
        self.assertEqual(moves[0][0], install / "PocketMemory.exe")
        self.assertEqual(moves[-1][1], install / "PocketMemory.exe")
        self.assert_original(install)

    def test_identical_models_overlap_retained_and_different_models_refused(self):
        install, source, _ = self.fixture(both=True)
        updater = up.SafeUpdater(install)
        self.assertEqual(updater.check(source)["models_to_relocate"], 0)
        updater.apply(source)
        updater.rollback()
        self.assert_original(install)
        put(install / "models/bge/model.bin", b"different model")
        with self.assertRaisesRegex(up.UpdateError, "Different external"):
            updater.apply(source)
        self.assert_original(install)

    def test_integrity_and_exact_whitelist(self):
        for variant in ("payload_hash", "model_hash", "missing_model", "extra_file", "file_size"):
            with self.subTest(variant=variant):
                install, source, manifest = self.fixture(variant)
                if variant == "payload_hash":
                    put(source / "payload/PocketMemory.exe", b"bad exe")
                elif variant == "model_hash":
                    put(install / "_internal/models/bge/model.bin", b"broken model")
                elif variant == "missing_model":
                    (install / "_internal/models/bge/model.bin").unlink()
                elif variant == "extra_file":
                    put(source / "payload/extra.exe", b"not listed")
                else:
                    manifest["files"][0]["size"] += 1
                    self.write_manifest(source, manifest)
                with self.assertRaises(up.UpdateError):
                    up.SafeUpdater(install).apply(source)
                self.assertEqual((install / "PocketMemory.exe").read_bytes(), b"old exe")

    def test_forbidden_paths_and_windows_aliases(self):
        install, source, manifest = self.fixture()
        for path in ("../data/x", "/tmp/x", "_internal/../data/x", "_internal\\x",
                     "_internal/x:stream", "_internal/CON.txt", "_internal/NUL",
                     "_internal/COM¹.txt", "_internal/x.", "_internal/x ",
                     "_internal/FOO~1/x", "_internal//x", "models/a", "data/a",
                     "config.json", "_internal/models/a", "_internal/data/a",
                     "_internal/sub/pocket_memory.db"):
            with self.subTest(path=path):
                bad = copy.deepcopy(manifest)
                bad["files"].append(entry(path, b"bad"))
                with self.assertRaises(up.UpdateError):
                    up.validate_manifest(bad)
        self.assert_original(install)

    def test_case_duplicate_manifest_and_file_directory_collision(self):
        _, _, manifest = self.fixture()
        for path in ("_internal/RUNTIME.bin", "_internal/runtime.bin/child"):
            bad = copy.deepcopy(manifest)
            bad["files"].append(entry(path, b"bad"))
            with self.assertRaises(up.UpdateError):
                up.validate_manifest(bad)

    def test_model_parent_spelling_and_file_directory_conflicts(self):
        for name in ("spelling", "file"):
            install, source, _ = self.fixture(name)
            if name == "spelling":
                put(install / "models/BGE/unrelated.bin", b"existing")
            else:
                put(install / "models/bge", b"a file cannot become a directory")
            with self.assertRaisesRegex(up.UpdateError, "Model spelling|Model parent"):
                up.SafeUpdater(install).apply(source)
            self.assert_original(install)

    def test_hooks_and_data_schema_changes_refused(self):
        install, source, manifest = self.fixture()
        variants = []
        bad = copy.deepcopy(manifest)
        bad["post_install"] = "malicious.exe"
        variants.append(bad)
        for key, value in (("migration", "automatic"), ("target_schema_id", "new-schema"),
                           ("accepted_schema_ids", ["unknown-schema"])):
            bad = copy.deepcopy(manifest)
            bad["data_compat"][key] = value
            variants.append(bad)
        for bad in variants:
            self.write_manifest(source, bad)
            with self.assertRaises(up.UpdateError):
                up.SafeUpdater(install).apply(source)
        self.assert_original(install)

    def test_custom_data_and_beta_config_protection(self):
        for kind in ("explicit", "config", "beta", "profile", "state"):
            with self.subTest(kind=kind):
                install, source, _ = self.fixture(kind)
                data = install / (up.STATE_DIR if kind == "state" else "_internal/custom-data")
                options = {}
                if kind in {"explicit", "state"}:
                    options["protected_data_dirs"] = [data]
                else:
                    config = {"config": install / "config.json",
                              "beta": install / "beta-profile/PocketMemory/config.json",
                              "profile": Path(os.environ["LOCALAPPDATA"]) / "PocketMemory/config.json"}[kind]
                    put(config, json.dumps({"data_dir": str(data)}).encode())
                with self.assertRaisesRegex(up.UpdateError, "data/config"):
                    up.SafeUpdater(install, **options).apply(source)
                if kind == "state":
                    self.assertFalse((install / up.STATE_DIR).exists())
                self.assert_original(install)

    def test_unconfigured_database_inside_runtime_refused(self):
        install, source, _ = self.fixture()
        put(install / "_internal/arbitrary/pocket_memory.db", b"user data")
        with self.assertRaisesRegex(up.UpdateError, "Possible user data"):
            up.SafeUpdater(install).apply(source)
        self.assert_original(install)

    def test_running_process_blocks_without_killing(self):
        install, source, _ = self.fixture()
        with mock.patch.object(up, "running_install_processes", return_value=[123]):
            with self.assertRaisesRegex(up.UpdateError, "Close this installation"):
                up.SafeUpdater(install).apply(source)
        self.assert_original(install)

    def test_frozen_app_can_only_opt_into_read_only_preflight(self):
        install, source, _ = self.fixture()
        updater = up.SafeUpdater(install)
        with mock.patch.object(up.sys, "frozen", True, create=True), \
                mock.patch.object(up.sys, "executable", str(install / "PocketMemory.exe")), \
                mock.patch.object(up, "running_install_processes", return_value=[123]):
            with self.assertRaisesRegex(up.UpdateError, "standalone update helper"):
                updater.check(source)
            checked = updater.check(source, read_only=True)
            self.assertEqual(checked["state"], "checked")
            self.assertEqual(checked["models_to_relocate"], 1)
            for operation in (
                    lambda: updater.apply(source), updater.recover, updater.rollback,
                    lambda: updater.commit("This note must not authorize an in-app commit")):
                with self.assertRaisesRegex(up.UpdateError, "standalone update helper"):
                    operation()
        self.assertFalse(updater.journal_path.exists())
        self.assertFalse((install / "models").exists())
        self.assert_original(install)

    def test_read_only_preflight_still_checks_integrity_and_protected_paths(self):
        install, source, _ = self.fixture()
        with mock.patch.object(up.sys, "frozen", True, create=True), \
                mock.patch.object(up.sys, "executable", str(install / "PocketMemory.exe")), \
                mock.patch.object(up, "running_install_processes", return_value=[123]):
            with self.assertRaisesRegex(up.UpdateError, "configured data/config"):
                up.SafeUpdater(install, protected_data_dirs=[install / "_internal"]).check(source, read_only=True)
            put(source / "payload/PocketMemory.exe", b"corrupt payload")
            with self.assertRaisesRegex(up.UpdateError, "Missing or damaged"):
                up.SafeUpdater(install).check(source, read_only=True)
        self.assert_original(install)

    def test_read_only_preflight_does_not_disable_external_worker_process_guards(self):
        install, source, _ = self.fixture()
        updater = up.SafeUpdater(install)
        with mock.patch.object(up.sys, "frozen", True, create=True), \
                mock.patch.object(up.sys, "executable", str(self.base / "external-worker.exe")), \
                mock.patch.object(up, "running_install_processes", return_value=[123]) as running:
            updater.check(source, read_only=True)
            with self.assertRaisesRegex(up.UpdateError, "Close this installation"):
                updater.apply(source)
            running.return_value = []
            updater.apply(source)
            running.return_value = [123]
            for operation in (updater.rollback, lambda: updater.commit("Real process guard must still apply")):
                with self.assertRaisesRegex(up.UpdateError, "Close this installation"):
                    operation()
            journal = json.loads(updater.journal_path.read_text())
            journal["state"] = "applying"
            updater.journal_path.write_text(json.dumps(journal))
            with self.assertRaisesRegex(up.UpdateError, "Close this installation"):
                updater.recover()
        updater.recover()
        self.assert_original(install)

    def test_concurrent_updater_lock(self):
        install, source, _ = self.fixture()
        first, second = up.SafeUpdater(install), up.SafeUpdater(install)
        with first._locked():
            with self.assertRaisesRegex(up.UpdateError, "Another updater"):
                second.apply(source)
        self.assert_original(install)

    def test_locked_program_file_rolls_back(self):
        install, source, _ = self.fixture()
        updater = up.SafeUpdater(install)
        original = updater._move
        def locked(source_path, target_path):
            if target_path == install / "_internal" and source_path.parent.name == "stage":
                raise PermissionError("simulated Windows runtime lock")
            original(source_path, target_path)
        with mock.patch.object(updater, "_move", side_effect=locked):
            with self.assertRaisesRegex(up.UpdateError, "was rolled back"):
                updater.apply(source)
        self.assert_original(install)
        self.assertEqual(updater.recover()["state"], "rolled_back")

    def test_crash_after_each_forward_rename_recovers_twice(self):
        # Fixture has model move, three replacements, new licenses, and receipt.
        for point in range(1, 10):
            with self.subTest(point=point):
                install, source, _ = self.fixture("rename-" + str(point))
                updater = up.SafeUpdater(install)
                original, count = updater._move, [0]
                def interrupt(source_path, target_path):
                    original(source_path, target_path)
                    count[0] += 1
                    if count[0] == point:
                        raise Interrupted()
                with mock.patch.object(updater, "_move", side_effect=interrupt):
                    try:
                        updater.apply(source)
                    except Interrupted:
                        pass
                restored = up.SafeUpdater(install)
                if restored.recover()["state"] == "pending_health":
                    restored.rollback()
                restored.recover()
                self.assert_original(install)

    def test_crash_after_write_ahead_records(self):
        # Crash after persisted intent, before filesystem mutation (including mkdir).
        for point in range(1, 15):
            with self.subTest(point=point):
                install, source, _ = self.fixture("save-" + str(point))
                updater = up.SafeUpdater(install)
                original, count = updater._save, [0]
                def interrupt(journal):
                    original(journal)
                    count[0] += 1
                    if count[0] == point:
                        raise Interrupted()
                with mock.patch.object(updater, "_save", side_effect=interrupt):
                    try:
                        updater.apply(source)
                    except Interrupted:
                        pass
                restored = up.SafeUpdater(install)
                if restored.recover()["state"] == "pending_health":
                    restored.rollback()
                restored.recover()
                self.assert_original(install)

    def test_crash_during_rollback_is_resumable(self):
        for point in range(1, 10):
            with self.subTest(point=point):
                install, source, _ = self.fixture("undo-" + str(point))
                updater = up.SafeUpdater(install)
                updater.apply(source)
                original, count = updater._move, [0]
                def interrupt(source_path, target_path):
                    original(source_path, target_path)
                    count[0] += 1
                    if count[0] == point:
                        raise Interrupted()
                with mock.patch.object(updater, "_move", side_effect=interrupt):
                    try:
                        updater.rollback()
                    except Interrupted:
                        pass
                restored = up.SafeUpdater(install)
                restored.recover()
                restored.rollback()
                self.assert_original(install)

    def test_rollback_does_not_overwrite_changed_model(self):
        install, source, _ = self.fixture()
        updater = up.SafeUpdater(install)
        updater.apply(source)
        put(install / "models/bge/model.bin", b"user changed model")
        with self.assertRaisesRegex(up.UpdateError, "relocated model changed"):
            updater.rollback()
        self.assertEqual((install / "models/bge/model.bin").read_bytes(), b"user changed model")
        self.assertEqual(json.loads(updater.journal_path.read_text())["state"], "rolling_back")

    def test_symlink_or_junction_refused(self):
        install, source, _ = self.fixture()
        outside = self.base / "outside"
        put(outside / "untouched.txt", b"keep")
        link = source / "payload/_internal/link"
        if os.name == "nt":
            # Explicit tiny fixture paths only. mklink /J creates no real-install link.
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
            if result.returncode:
                self.skipTest("Junction creation unavailable in this environment")
        else:
            link.symlink_to(outside, target_is_directory=True)
        try:
            with self.assertRaisesRegex(up.UpdateError, "reparse|Symlink|junction"):
                up.SafeUpdater(install).apply(source)
        finally:
            if os.name == "nt":
                link.rmdir()
            else:
                link.unlink()
        self.assertEqual((outside / "untouched.txt").read_bytes(), b"keep")
        self.assert_original(install)

    def test_hardlink_payload_refused(self):
        install, source, _ = self.fixture()
        target = self.base / "alias.exe"
        os.link(source / "payload/PocketMemory.exe", target)
        with self.assertRaisesRegex(up.UpdateError, "Hardlinked"):
            up.SafeUpdater(install).apply(source)
        self.assert_original(install)

    def test_malicious_journal_cannot_rewrite_data(self):
        install, source, _ = self.fixture()
        updater = up.SafeUpdater(install)
        updater.apply(source)
        journal = json.loads(updater.journal_path.read_text())
        journal["state"] = "applying"
        journal["operations"].append({"kind": "backup", "path": "data"})
        updater.journal_path.write_text(json.dumps(journal))
        with self.assertRaisesRegex(up.UpdateError, "non-program"):
            updater.recover()
        self.assertEqual((install / "data/notes/keep.txt").read_bytes(), b"user note")

    def test_source_under_install_refused_and_cli_check(self):
        install, source, _ = self.fixture()
        with self.assertRaisesRegex(up.UpdateError, "outside"):
            up.SafeUpdater(install).apply(install / "updates")
        script = MODULE.parents[1] / "scripts/update_entry.py"
        result = subprocess.run([sys.executable, str(script), "check", "--install", str(install), "--source", str(source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"], "checked")
        self.assert_original(install)


if __name__ == "__main__":
    unittest.main()
