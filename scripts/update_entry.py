# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Maintainer-only transaction diagnostics. Users update inside Pocket Memory.

This CLI is not shipped as a user-facing directory-picker application.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pocket_memory.safe_update import SafeUpdater, UpdateError


def make_parser():
    parser = argparse.ArgumentParser(description="Pocket Memory offline program updater")
    parser.add_argument("action", choices=("check", "apply", "rollback", "recover", "commit"))
    parser.add_argument("--install", type=Path, help="Existing PocketMemory.exe installation directory")
    parser.add_argument("--source", type=Path, help="Unpacked update directory containing update-manifest.json and payload/")
    parser.add_argument("--data-dir", type=Path, action="append", default=[], help="Additional custom data directory to protect (repeatable)")
    parser.add_argument("--config", type=Path, action="append", default=[], help="Additional config JSON to read and protect (repeatable)")
    parser.add_argument("--health-note", default="", help="Your actual application health smoke observations, required for commit")
    return parser


def _execute(args, action=None):
    if args.install is None:
        raise UpdateError("Select the existing installation directory")
    updater = SafeUpdater(args.install, protected_data_dirs=args.data_dir, config_paths=args.config)
    action = action or args.action
    if action in {"check", "apply"}:
        if args.source is None:
            raise UpdateError("Select the unpacked update source directory")
        return getattr(updater, action)(args.source)
    if action == "commit":
        return updater.commit(args.health_note)
    return getattr(updater, action)()


def main(argv=None):
    args = make_parser().parse_args(argv)
    try:
        print(json.dumps(_execute(args), ensure_ascii=False, indent=2))
        return 0
    except (UpdateError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"state": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
