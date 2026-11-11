# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Conservative, offline program replacement. No network, extraction or package hooks.

The helper must run OUTSIDE the installation. A successful apply is pending_health,
not a healthy release. A person (or a separate trusted smoke runner) must start the
real application, check its normal local flows, stop it, and explicitly commit.
Manifests are integrity inventories, not publisher signatures: use a trusted source.
All mutation is confined to a whitelist, model relocation and our transaction area.
"""
from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import unicodedata
import uuid
from pathlib import Path


MANIFEST_NAME = "update-manifest.json"
STATE_DIR = ".pocket-memory-update"
PROGRAM_ROOT_FILES = frozenset({
    "PocketMemory.exe", "README.txt", "README.md", "Start-Beta.bat", "Stop-Beta.bat",
    "LICENSE", "NOTICE", "BETA_RELEASE_NOTES.md", "THIRD_PARTY_NOTICES.md",
    "RERANKER_NOTICE.md", "SOURCE_DISTRIBUTION.md", "SOURCE_INFORMATION.md",
    "python-environment-license-inventory.csv", "python-environment-license-inventory.json",
    "upstream-license-sources.json", "build-environment-freeze.txt",
})
PROGRAM_ROOT_DIRS = frozenset({"_internal", "licenses"})
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.I)
_SHA = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_INTERNAL = {"models", "data", "data-beta", "config", "config.json", "backups",
                       "beta-profile", "notes", "attachments", "logs", STATE_DIR}


class UpdateError(RuntimeError):
    pass


def _relative(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 240:
        raise UpdateError("Invalid or overlong relative path")
    for part in value.split("/"):
        if (not part or part in {".", ".."} or part[-1:] in {" ", "."}
                or any(ord(c) < 32 or c in '\\:<>"|?*~' for c in part)
                or unicodedata.normalize("NFC", part) != part or _RESERVED.match(part)):
            raise UpdateError(f"Unsafe Windows path: {value!r}")
    return value


def is_program_path(value: str) -> bool:
    parts = _relative(value).split("/")
    if len(parts) == 1:
        return value in PROGRAM_ROOT_FILES
    if parts[0] not in PROGRAM_ROOT_DIRS:
        return False
    if parts[0] == "_internal":
        if parts[1].casefold() in _FORBIDDEN_INTERNAL:
            return False
        if any(p.casefold() in {"pocket_memory.db", "pocket_memory.db-wal", "pocket_memory.db-shm"}
               for p in parts):
            return False
    return True


def _no_links(path: Path) -> None:
    """Check existing ancestors too; Path.resolve() alone hides junctions."""
    for item in reversed((path, *path.parents)):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise UpdateError(f"Symlink/junction/reparse point is not allowed: {item}")
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise UpdateError(f"Hardlinked file is not allowed: {item}")


def _absolute(path: os.PathLike | str) -> Path:
    raw = Path(os.path.abspath(os.fspath(path)))
    _no_links(raw)
    # Do not accept Win32 spelling aliases even for a caller-selected root.
    for part in raw.parts[1:]:
        _relative(part)
    return raw


def _overlap(left: Path, right: Path) -> bool:
    a, b = str(left).casefold(), str(right).casefold()
    sep = os.sep
    return a == b or a.startswith(b.rstrip(sep) + sep) or b.startswith(a.rstrip(sep) + sep)


def _hash(path: Path) -> str:
    _no_links(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inventory(root: Path) -> dict[str, Path]:
    _no_links(root)
    if not root.is_dir():
        raise UpdateError(f"Directory missing: {root}")
    files, seen = {}, set()
    for base, directories, names in os.walk(root, followlinks=False):
        for name in directories + names:
            path = Path(base) / name
            _no_links(path)
            rel = _relative(path.relative_to(root).as_posix())
            key = rel.casefold()
            if key in seen:
                raise UpdateError(f"Case-insensitive path collision: {rel}")
            seen.add(key)
            if path.is_file():
                files[rel] = path
            elif not path.is_dir():
                raise UpdateError(f"Unsupported file type: {path}")
    return files


def _read_json(path: Path) -> dict:
    _no_links(path)
    if path.stat().st_size > 16 * 1024 * 1024:
        raise UpdateError(f"JSON is too large: {path}")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise UpdateError(f"Duplicate JSON field: {key}")
            result[key] = value
        return result
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=pairs)
    except (ValueError, UnicodeError) as exc:
        raise UpdateError(f"Invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise UpdateError(f"JSON root must be an object: {path}")
    return value


def _atomic_json(path: Path, value: dict) -> None:
    _no_links(path)
    temporary = path.with_name(path.name + ".tmp")
    _no_links(temporary)
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    if os.name == "nt":
        move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        move.restype = ctypes.c_int
        if not move(str(temporary), str(path), 1 | 8):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(temporary, path)
        descriptor = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _entries(values, program: bool) -> list[dict]:
    if not isinstance(values, list) or not values:
        raise UpdateError("A nonempty file/model inventory is required")
    seen, results = set(), []
    for value in values:
        if not isinstance(value, dict) or set(value) != {"path", "size", "sha256"}:
            raise UpdateError("File entries require exactly path, size and sha256")
        rel = _relative(value["path"])
        if program and not is_program_path(rel):
            raise UpdateError(f"Not a permitted program file: {rel}")
        if type(value["size"]) is not int or value["size"] < 0:
            raise UpdateError(f"Invalid file size: {rel}")
        if not isinstance(value["sha256"], str) or not _SHA.fullmatch(value["sha256"]):
            raise UpdateError(f"Invalid SHA-256: {rel}")
        if rel.casefold() in seen:
            raise UpdateError(f"Duplicate inventory path: {rel}")
        seen.add(rel.casefold())
        results.append(dict(value))
    # A file cannot simultaneously be the parent directory of another entry.
    for key in seen:
        if any("/".join(key.split("/")[:i]) in seen for i in range(1, len(key.split("/")))):
            raise UpdateError("File/directory inventory collision")
    return results


def validate_manifest(manifest: dict) -> dict:
    required = {"schema_version", "app_id", "kind", "version", "files",
                "model_requirements", "data_compat"}
    if set(manifest) != required:
        raise UpdateError("Unsupported manifest fields; hooks and migrations are not supported")
    if (type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or manifest["app_id"] != "pocket-memory" or manifest["kind"] != "program-update"):
        raise UpdateError("Unsupported application/package/schema")
    if not isinstance(manifest["version"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,79}", manifest["version"]):
        raise UpdateError("Invalid version")
    manifest = dict(manifest)
    manifest["files"] = _entries(manifest["files"], True)
    manifest["model_requirements"] = _entries(manifest["model_requirements"], False)
    paths = {entry["path"] for entry in manifest["files"]}
    if "PocketMemory.exe" not in paths or not any(p.startswith("_internal/") for p in paths):
        raise UpdateError("A complete PocketMemory.exe and _internal program inventory is required")
    compat = manifest["data_compat"]
    if (not isinstance(compat, dict)
            or set(compat) != {"accepted_schema_ids", "target_schema_id", "migration"}
            or compat["migration"] != "none"
            or not isinstance(compat["accepted_schema_ids"], list)
            or not compat["accepted_schema_ids"]):
        raise UpdateError("Only explicitly declared no-migration compatibility is supported")
    for schema in [*compat["accepted_schema_ids"], compat["target_schema_id"]]:
        if not isinstance(schema, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", schema):
            raise UpdateError("Invalid data schema identifier")
    return manifest


def _verify(path: Path, entry: dict) -> None:
    _no_links(path)
    if not path.is_file() or path.stat().st_size != entry["size"] or _hash(path) != entry["sha256"]:
        raise UpdateError(f"Missing or damaged file: {path}")


def running_install_processes(install: Path) -> list[int]:
    """Windows-only path-specific check; never stop a process."""
    if os.name != "nt":
        return []
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise UpdateError("Unable to enumerate running applications")
    matches = []
    expected = str(install / "PocketMemory.exe").casefold()
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        if not more:
            raise UpdateError("Unable to inspect process list")
        while more:
            if entry.szExeFile.casefold() == "pocketmemory.exe":
                handle = kernel.OpenProcess(0x1000, False, entry.th32ProcessID)
                if not handle:
                    # Process may have exited. Access denied cannot prove it is safe.
                    if ctypes.get_last_error() == 5:
                        raise UpdateError("Cannot inspect a running PocketMemory.exe; close it before updating")
                else:
                    try:
                        size = wintypes.DWORD(32768)
                        buffer = ctypes.create_unicode_buffer(size.value)
                        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                            raise UpdateError("Cannot determine the running PocketMemory.exe path")
                        image_path = os.path.abspath(buffer.value).casefold()
                        backup_prefix = str(install / STATE_DIR).casefold() + os.sep
                        if (image_path == expected or (image_path.startswith(backup_prefix)
                                and image_path.endswith(os.sep + "backup" + os.sep + "pocketmemory.exe"))):
                            matches.append(int(entry.th32ProcessID))
                    finally:
                        kernel.CloseHandle(handle)
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return matches


class SafeUpdater:
    def __init__(self, install_dir, protected_data_dirs=(), config_paths=()):
        self.install = _absolute(install_dir)
        if not self.install.is_dir():
            raise UpdateError("Select an existing installation directory")
        self.state = self.install / STATE_DIR
        self.journal_path = self.state / "journal.json"
        self.receipt_path = self.state / "installed.json"
        self.extra_data = tuple(protected_data_dirs)
        configs = [self.install / "config.json", self.install / "beta-profile" / "PocketMemory" / "config.json"]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            configs.append(Path(local) / "PocketMemory" / "config.json")
        configs.extend(Path(path) for path in config_paths)
        self.configs = tuple(configs)

    @contextlib.contextmanager
    def _locked(self):
        # Even the lock/journal directory must not be created inside a configured
        # data location. Resolve protection before the first filesystem write.
        self._protect([self.state])
        _no_links(self.state)
        self.state.mkdir(exist_ok=True)
        lock = self.state / "update.lock"
        _no_links(lock)
        with lock.open("a+b") as handle:
            handle.seek(0, 2)
            if not handle.tell():
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise UpdateError("Another updater is using this installation") from exc
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _protected(self) -> list[Path]:
        paths = [_absolute(p) for p in self.extra_data]
        for config in self.configs:
            config = _absolute(config)
            paths.append(config)
            if config.exists():
                value = _read_json(config).get("data_dir")
                if value is not None:
                    if not isinstance(value, str) or not value:
                        raise UpdateError(f"Invalid data_dir in {config}")
                    paths.append(_absolute(value))
        return paths

    def _protect(self, targets: list[Path]) -> None:
        for data in self._protected():
            for target in targets:
                if _overlap(data, target):
                    raise UpdateError(f"Refusing to replace a configured data/config path: {data}")

    def _require_external_helper(self):
        if getattr(sys, "frozen", False) and _overlap(_absolute(sys.executable), self.install):
            raise UpdateError("Run the standalone update helper from outside the installation")

    def _not_running(self):
        pids = running_install_processes(self.install)
        if pids:
            raise UpdateError(f"Close this installation before updating or rolling back (PID {pids})")

    def _schema(self) -> str:
        if not self.receipt_path.exists():
            return "legacy-unversioned"
        receipt = _read_json(self.receipt_path)
        if receipt.get("schema_version") != 1 or receipt.get("app_id") != "pocket-memory":
            raise UpdateError("Unknown installed receipt schema")
        schema = receipt.get("data_schema_id")
        if not isinstance(schema, str):
            raise UpdateError("Installed receipt has no data schema identifier")
        return schema

    def _journal(self) -> dict | None:
        if not self.journal_path.exists():
            return None
        journal = _read_json(self.journal_path)
        if (journal.get("schema_version") != 1
                or not re.fullmatch(r"[0-9a-f]{32}", str(journal.get("transaction", "")))
                or journal.get("state") not in {"prepared", "applying", "pending_health", "rolling_back", "rolled_back", "committed"}
                or not isinstance(journal.get("operations"), list)):
            raise UpdateError("Invalid transaction journal; no automatic filesystem repair is safe")
        for operation in journal["operations"]:
            self._op_paths(journal, operation)  # Validate paths before any recovery.
        return journal

    def _save(self, journal):
        _atomic_json(self.journal_path, journal)

    def _op_paths(self, journal, operation):
        tx = self.state / journal["transaction"]
        kind, rel = operation.get("kind"), operation.get("path")
        if kind == "mkdir_model":
            if rel != "":
                _relative(rel)
            return None, self.install / "models" / rel
        if kind == "model_move":
            _relative(rel)
            if not _SHA.fullmatch(str(operation.get("sha256", ""))):
                raise UpdateError("Invalid model operation digest")
            return self.install / "_internal" / "models" / rel, self.install / "models" / rel
        if kind in {"backup", "install"}:
            if rel not in PROGRAM_ROOT_FILES | PROGRAM_ROOT_DIRS:
                raise UpdateError("Journal attempted to modify a non-program path")
            if kind == "backup":
                return self.install / rel, tx / "backup" / rel
            return tx / "stage" / rel, self.install / rel
        if kind == "receipt_backup" and rel == "installed.json":
            return self.receipt_path, tx / "receipt-backup.json"
        if kind == "receipt_install" and rel == "installed.json":
            return tx / "receipt-new.json", self.receipt_path
        raise UpdateError("Unsupported journal operation")

    def _move(self, source: Path, target: Path):
        _no_links(source)
        _no_links(target)
        if target.exists() or not source.exists():
            raise UpdateError(f"Unexpected rename state: {source} -> {target}")
        # Deliberately never copy across volumes or overwrite an occupied target.
        os.rename(source, target)

    def _perform(self, journal, operation):
        source, target = self._op_paths(journal, operation)
        self._protect([p for p in (source, target) if p is not None])
        _no_links(target)
        journal["operations"].append(operation)
        self._save(journal)  # Write-ahead: a crash after rename is recoverable.
        if operation["kind"] == "mkdir_model":
            target.mkdir()
        else:
            self._move(source, target)

    def _rollback(self, journal) -> dict:
        if journal["state"] == "rolled_back":
            return self._status(journal)
        self._not_running()
        journal["state"] = "rolling_back"
        self._save(journal)
        for operation in reversed(journal["operations"]):
            if operation.get("undone"):
                continue
            source, target = self._op_paths(journal, operation)
            self._protect([p for p in (source, target) if p is not None])
            _no_links(target)
            if operation["kind"] == "mkdir_model":
                if target.exists():
                    try:
                        target.rmdir()  # Leave new, unrelated user content intact.
                    except OSError:
                        if not target.is_dir():
                            raise
            else:
                _no_links(source)
                if target.exists() and not source.exists():
                    if operation["kind"] == "model_move" and _hash(target) != operation["sha256"]:
                        raise UpdateError("A relocated model changed; automatic rollback will not overwrite it")
                    self._move(target, source)
                elif not source.exists() or target.exists():
                    raise UpdateError(f"Ambiguous recovery state, preserving both paths: {source}, {target}")
            operation["undone"] = True
            self._save(journal)  # Persist each undo before starting the preceding one.
        journal["state"] = "rolled_back"
        self._save(journal)
        return self._status(journal)

    def _recover(self) -> dict:
        journal = self._journal()
        if journal and journal["state"] in {"prepared", "applying", "rolling_back"}:
            return self._rollback(journal)
        return self._status(journal)

    def _status(self, journal) -> dict:
        if not journal:
            return {"state": "no_transaction"}
        return {key: journal[key] for key in ("state", "transaction", "version")}

    def recover(self) -> dict:
        self._require_external_helper()
        with self._locked():
            return self._recover()

    def rollback(self) -> dict:
        self._require_external_helper()
        with self._locked():
            journal = self._journal()
            return self._rollback(journal) if journal else self._status(None)

    def _check(self, source, *, read_only=False) -> tuple[dict, list[dict], list[str]]:
        source = _absolute(source)
        if _overlap(source, self.install):
            raise UpdateError("The unpacked update source must be outside the installation")
        if not read_only:
            self._require_external_helper()
        manifest = validate_manifest(_read_json(source / MANIFEST_NAME))
        inventory = _inventory(source / "payload")
        if set(inventory) != {entry["path"] for entry in manifest["files"]}:
            raise UpdateError("Payload differs from its exact manifest whitelist")
        for entry in manifest["files"]:
            _verify(inventory[entry["path"]], entry)
        schema = self._schema()
        compat = manifest["data_compat"]
        if schema not in compat["accepted_schema_ids"] or compat["target_schema_id"] != schema:
            raise UpdateError("Data schema compatibility is undeclared or requires migration; update refused")
        roots = sorted({entry["path"].split("/")[0] for entry in manifest["files"]})
        self._protect([self.install / root for root in roots] + [self.state])
        _no_links(self.install / "PocketMemory.exe")
        if not (self.install / "PocketMemory.exe").is_file():
            raise UpdateError("Not an existing Pocket Memory installation")
        for root in roots:
            path = self.install / root
            _no_links(path)
            if path.is_dir():
                old_files = _inventory(path)
                if root == "_internal":
                    for rel in old_files:
                        if not rel.startswith("models/") and not is_program_path("_internal/" + rel):
                            raise UpdateError(f"Possible user data inside runtime directory: {rel}")
        root_models = self.install / "models"
        old_models = self.install / "_internal" / "models"
        current = _inventory(root_models) if root_models.exists() else {}
        legacy = _inventory(old_models) if old_models.exists() else {}
        # Include directories: models/qwen/a and legacy/Qwen/b alias the same
        # parent on Windows even though no full filename collides.
        spellings = {}
        if root_models.exists():
            for base, directories, names in os.walk(root_models, followlinks=False):
                for name in directories + names:
                    rel = (Path(base) / name).relative_to(root_models).as_posix()
                    spellings[rel.casefold()] = rel
        migration = []
        for rel, old in sorted(legacy.items()):
            parts = rel.split("/")
            for length in range(1, len(parts) + 1):
                prefix = "/".join(parts[:length])
                alias = spellings.get(prefix.casefold())
                if alias is not None and alias != prefix:
                    raise UpdateError(f"Model spelling collision: {prefix} / {alias}")
                if length < len(parts):
                    parent = root_models / prefix
                    if parent.exists() and not parent.is_dir():
                        raise UpdateError(f"Model parent is not a directory: {prefix}")
            if rel in current:
                if old.stat().st_size != current[rel].stat().st_size or _hash(old) != _hash(current[rel]):
                    raise UpdateError(f"Different external and bundled models: {rel}")
            else:
                target = root_models / rel
                _no_links(target)
                if target.exists():
                    raise UpdateError(f"Model target collision: {rel}")
                migration.append({"kind": "model_move", "path": rel, "sha256": _hash(old)})
        for entry in manifest["model_requirements"]:
            rel = entry["path"]
            path = current.get(rel) or legacy.get(rel)
            if path is None:
                raise UpdateError(f"Required model resource missing: models/{rel}")
            _verify(path, entry)
        self._protect([root_models / item["path"] for item in migration])
        if not read_only:
            self._not_running()
        return manifest, migration, roots

    def check(self, source, *, read_only=False) -> dict:
        """Check inventories without replacing files; lock metadata may be created.

        The running app can opt into read_only preflight. This is a per-call
        exception, never an instance flag or a bypass inherited by mutations.
        """
        with self._locked():
            status = self._status(self._journal())
            if status["state"] in {"prepared", "applying", "rolling_back"}:
                raise UpdateError("Interrupted transaction: run recover first")
            manifest, migration, roots = self._check(source, read_only=read_only)
            return {"state": "checked", "version": manifest["version"], "program_roots": roots,
                    "files": len(manifest["files"]), "models": len(manifest["model_requirements"]),
                    "models_to_relocate": len(migration), "previous_transaction": status["state"],
                    "data_schema_id": self._schema(), "health_verified": False}

    def apply(self, source) -> dict:
        self._require_external_helper()
        with self._locked():
            status = self._recover()
            if status["state"] == "pending_health":
                raise UpdateError("Confirm health or roll back the pending update before another update")
            manifest, migration, roots = self._check(source)
            source = _absolute(source)
            total = sum(entry["size"] for entry in manifest["files"])
            if shutil.disk_usage(self.install).free < total + 1024 * 1024:
                raise UpdateError("Insufficient disk space for same-volume staging")
            txid = uuid.uuid4().hex
            tx = self.state / txid
            stage = tx / "stage"
            stage.mkdir(parents=True)
            (tx / "backup").mkdir()
            for entry in manifest["files"]:
                src, dst = source / "payload" / entry["path"], stage / entry["path"]
                _no_links(src)
                dst.parent.mkdir(parents=True, exist_ok=True)
                with src.open("rb") as reader, dst.open("xb") as writer:
                    shutil.copyfileobj(reader, writer, 1024 * 1024)
                    writer.flush()
                    os.fsync(writer.fileno())
                _verify(dst, entry)
            receipt = {"schema_version": 1, "app_id": "pocket-memory", "version": manifest["version"],
                       "data_schema_id": manifest["data_compat"]["target_schema_id"], "manifest": manifest}
            _atomic_json(tx / "receipt-new.json", receipt)
            journal = {"schema_version": 1, "transaction": txid, "version": manifest["version"],
                       "state": "prepared", "operations": []}
            self._save(journal)
            try:
                journal["state"] = "applying"
                self._save(journal)
                # Remove the launch entry first. Keep it absent until all runtime
                # resources and the receipt are installed, avoiding new-EXE/old-DLL
                # startup if a user clicks the usual shortcut during replacement.
                self._not_running()
                self._perform(journal, {"kind": "backup", "path": "PocketMemory.exe"})
                for item in migration:
                    destination = self.install / "models" / item["path"]
                    needed = []
                    parent = destination.parent
                    while not parent.exists():
                        needed.append(parent)
                        parent = parent.parent
                    for parent in reversed(needed):
                        relative = "" if parent == self.install / "models" else parent.relative_to(self.install / "models").as_posix()
                        self._perform(journal, {"kind": "mkdir_model", "path": relative})
                    old = self.install / "_internal" / "models" / item["path"]
                    if _hash(old) != item["sha256"]:
                        raise UpdateError("Bundled model changed during preparation")
                    self._not_running()
                    self._perform(journal, item)
                for root in roots:
                    if root == "PocketMemory.exe":
                        continue
                    self._not_running()
                    if (self.install / root).exists():
                        self._perform(journal, {"kind": "backup", "path": root})
                    self._perform(journal, {"kind": "install", "path": root})
                if self.receipt_path.exists():
                    self._perform(journal, {"kind": "receipt_backup", "path": "installed.json"})
                self._perform(journal, {"kind": "receipt_install", "path": "installed.json"})
                self._not_running()
                self._perform(journal, {"kind": "install", "path": "PocketMemory.exe"})
                journal["state"] = "pending_health"
                self._save(journal)
                return {**self._status(journal), "health_verified": False,
                        "message": "Program replaced; old program retained. Start normally, verify, close it, then commit or rollback."}
            except Exception as exc:
                try:
                    self._rollback(journal)
                except Exception as recovery:
                    raise UpdateError(f"Update failed ({exc}); recovery needs attention ({recovery}). Close the app and run recover. Backups retained.") from exc
                raise UpdateError(f"Update failed and was rolled back: {exc}") from exc

    def commit(self, health_note: str) -> dict:
        self._require_external_helper()
        if not isinstance(health_note, str) or len(health_note.strip()) < 8:
            raise UpdateError("Provide an explicit real-application health verification note")
        with self._locked():
            journal = self._journal()
            if not journal or journal["state"] not in {"pending_health", "committed"}:
                raise UpdateError("No pending update to confirm")
            self._not_running()
            receipt = _read_json(self.receipt_path)
            manifest = validate_manifest(receipt["manifest"])
            for entry in manifest["files"]:
                _verify(self.install / entry["path"], entry)
            journal["state"] = "committed"
            journal["health_note"] = health_note.strip()
            journal["health_verification"] = "caller_attestation"
            self._save(journal)
            return {**self._status(journal), "health_verification": "caller_attestation", "backups_retained": True}
