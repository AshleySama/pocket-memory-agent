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
if ($WithoutModels -and $OnlineModelBootstrap) {
    throw "-WithoutModels and -OnlineModelBootstrap cannot be used together."
}
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtual environment not found: $python. Run setup_venv.bat first."
}

# Keep caller-supplied relative paths stable while imports/spec assets use root.
if ($OutputDir) {
    $OutputDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputDir)
}
if ($SupplementDir) {
    $SupplementDir = (Resolve-Path -LiteralPath $SupplementDir).Path
}
Push-Location -LiteralPath $root
try {
& $python -m PyInstaller --version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed. Run: $python -m pip install -r requirements-dev.txt"
}

if (-not $SkipTests) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "run_regression.ps1")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$version = & $python -c "from pocket_memory.version import APP_VERSION; print(APP_VERSION)"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if (-not $OutputDir) { $OutputDir = Join-Path $root "release" }
$packageName = "PocketMemory-$version"
if ($WithoutModels) {
    $packageName += "-no-models"
} elseif ($OnlineModelBootstrap) {
    $packageName += "-portable"
}
$packageRoot = Join-Path $OutputDir $packageName
if (Test-Path -LiteralPath $packageRoot) {
    throw "Release directory already exists: $packageRoot. Archive or remove it before rebuilding."
}

$buildRoot = Join-Path $env:TEMP "PocketMemory-build-$version-$([guid]::NewGuid().ToString('N'))"
$distRoot = Join-Path $buildRoot "dist"
$workRoot = Join-Path $buildRoot "work"
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null

if ($WithoutModels) {
    $env:POCKET_MEMORY_NO_MODELS = "1"
    Remove-Item Env:POCKET_MEMORY_NO_LLM -ErrorAction SilentlyContinue
} elseif ($OnlineModelBootstrap) {
    Remove-Item Env:POCKET_MEMORY_NO_MODELS -ErrorAction SilentlyContinue
    $env:POCKET_MEMORY_NO_LLM = "1"
} else {
    Remove-Item Env:POCKET_MEMORY_NO_MODELS -ErrorAction SilentlyContinue
    Remove-Item Env:POCKET_MEMORY_NO_LLM -ErrorAction SilentlyContinue
}
& $python -m PyInstaller --noconfirm --clean --distpath $distRoot --workpath $workRoot (Join-Path $root "PocketMemory.spec")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$builtApp = Join-Path $distRoot "PocketMemory"
if (-not (Test-Path -LiteralPath (Join-Path $builtApp "PocketMemory.exe"))) {
    throw "Build output is incomplete: PocketMemory.exe was not found."
}

# The updater must survive replacement of the application's _internal tree.
# Build a separate onefile executable; never depend on the main app's DLLs.
# These build and byte-integrity checks are mandatory even with -SkipTests.
$workerDist = Join-Path $buildRoot "worker-dist"
$workerWork = Join-Path $buildRoot "worker-work"
$workerSpec = Join-Path $buildRoot "worker-spec"
New-Item -ItemType Directory -Path $workerSpec | Out-Null
& $python -m PyInstaller --noconfirm --clean --onefile --noconsole --noupx --name PocketMemoryUpdateWorker --paths $root --distpath $workerDist --workpath $workerWork --specpath $workerSpec (Join-Path $PSScriptRoot "update_worker.py")
if ($LASTEXITCODE -ne 0) {
    throw "Standalone update worker build failed with exit code $LASTEXITCODE."
}
$builtWorker = Join-Path $workerDist "PocketMemoryUpdateWorker.exe"
$workerCheck = Join-Path $PSScriptRoot "check_update_component.py"
& $python $workerCheck $builtWorker
if ($LASTEXITCODE -ne 0) { throw "Built update worker validation failed." }
$internalRoot = Join-Path $builtApp "_internal"
New-Item -ItemType Directory -Path $internalRoot -Force | Out-Null
Copy-Item -LiteralPath $builtWorker -Destination (Join-Path $internalRoot "PocketMemoryUpdateWorker.exe")
& $python $workerCheck $builtApp --expected-worker $builtWorker
if ($LASTEXITCODE -ne 0) { throw "Update worker copy into application failed validation." }

New-Item -ItemType Directory -Path $packageRoot | Out-Null
Copy-Item -Path (Join-Path $builtApp "*") -Destination $packageRoot -Recurse
$readmeName = if ($WithoutModels) {
    "PACKAGE_README_NO_MODELS.txt"
} elseif ($OnlineModelBootstrap) {
    "PACKAGE_README_ONLINE_MODEL.txt"
} else {
    "PACKAGE_README.txt"
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot $readmeName) -Destination (Join-Path $packageRoot "README.txt")
if ($version -match 'beta') {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Start-Beta.bat') -Destination $packageRoot
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Stop-Beta.bat') -Destination $packageRoot
}
if ($SupplementDir) {
    $supplementRoot = (Resolve-Path -LiteralPath $SupplementDir).Path
    foreach ($item in Get-ChildItem -LiteralPath $supplementRoot) {
        if (Test-Path -LiteralPath (Join-Path $packageRoot $item.Name)) {
            throw "Release supplement would overwrite a build output: $($item.Name)"
        }
        Copy-Item -LiteralPath $item.FullName -Destination $packageRoot -Recurse
    }
}
if ($WithoutModels) {
    $modelsRoot = Join-Path $packageRoot "models"
    New-Item -ItemType Directory -Path $modelsRoot | Out-Null
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "MODELS_README.txt") -Destination (Join-Path $modelsRoot "README.txt")
}
$archivePath = Join-Path $OutputDir "$packageName.zip"
& $python $workerCheck $packageRoot --expected-worker $builtWorker
if ($LASTEXITCODE -ne 0) { throw "Release layout update worker validation failed." }
& $python (Join-Path $PSScriptRoot "create_zip64.py") $packageRoot $archivePath --expected-worker $builtWorker
if ($LASTEXITCODE -ne 0) {
    throw "ZIP64 packaging failed with exit code $LASTEXITCODE."
}
& $python $workerCheck $archivePath --expected-worker $builtWorker
if ($LASTEXITCODE -ne 0) { throw "Release ZIP update worker validation failed." }
$hashManifest = Join-Path $OutputDir "$packageName-SHA256.txt"
@(
    "Pocket Memory $version release",
    "Generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
    "",
    "Use these SHA-256 values when submitting the package to enterprise security for review or allowlisting.",
    "",
    "PocketMemory.exe  $((Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $packageRoot 'PocketMemory.exe')).Hash)",
    "_internal/PocketMemoryUpdateWorker.exe  $((Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $packageRoot '_internal/PocketMemoryUpdateWorker.exe')).Hash)",
    "$(Split-Path -Leaf $archivePath)  $((Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath).Hash)"
) | Set-Content -LiteralPath $hashManifest -Encoding UTF8

Write-Host "Release package created: $packageRoot"
Write-Host "Release archive created: $archivePath"
Write-Host "SHA-256 manifest created: $hashManifest"
} finally {
    Pop-Location
}
