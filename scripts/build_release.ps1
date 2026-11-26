# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

param(
    [switch]$SkipTests,
    [switch]$WithoutModels,
    [switch]$OnlineModelBootstrap,
    [string]$SupplementDir = "",
    [string]$OutputDir = ""
)

$ErrorActionPreference = "Stop"

# This former entry copied a raw PyInstaller directory and produced a full ZIP
# without an installed receipt. Such a package cannot authenticate the embedded
# executor. Refuse before tests, builds, directory creation, or artifact writes.
# The accepted arguments are retained solely to give old callers an explicit
# failure instead of silently producing a legacy or incomplete installation.
throw @"
This legacy build_release.ps1 entry is disabled: embedded-update releases require
an explicit schema-v2 manifest and a complete installed receipt.
At the authorized release point, build the main application separately and use:
  python scripts/prepare_offline_delivery.py --built-app <built-app> --model-source <models> --destination <new-layout> --version <version> --mode full --schema-id <verified-schema> --executor main-exe-v1
For a signed release, add --expected-signer-thumbprint <authorized-publisher-thumbprint>
to require publisher identity, signature and timestamp validation. Exact manifest,
file integrity and installed-receipt checks remain required in either case.
Then verify and archive that prepared layout with:
  python scripts/check_update_component.py <new-layout>
  python scripts/create_zip64.py <new-layout> <new-archive.zip>
Use --mode update with a separate destination for the program-only layout.
Do not ZIP the raw PyInstaller output or restore a missing standalone updater.
No tests, build, packaging, or output writes were started by this entry.
"@
