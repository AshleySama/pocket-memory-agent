param(
    [switch]$SkipTests,
    [switch]$WithoutModels,
    [switch]$OnlineModelBootstrap,
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

$buildRoot = Join-Path $env:TEMP "PocketMemory-build-$version"
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
if ($WithoutModels) {
    $modelsRoot = Join-Path $packageRoot "models"
    New-Item -ItemType Directory -Path $modelsRoot | Out-Null
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "MODELS_README.txt") -Destination (Join-Path $modelsRoot "README.txt")
}
$archivePath = Join-Path $OutputDir "$packageName.zip"
& $python (Join-Path $PSScriptRoot "create_zip64.py") $packageRoot $archivePath
if ($LASTEXITCODE -ne 0) {
    throw "ZIP64 packaging failed with exit code $LASTEXITCODE."
}
$hashManifest = Join-Path $OutputDir "$packageName-SHA256.txt"
@(
    "Pocket Memory $version release",
    "Generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
    "",
    "Use these SHA-256 values to verify the release files after download or distribution.",
    "",
    "PocketMemory.exe  $((Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $packageRoot 'PocketMemory.exe')).Hash)",
    "$(Split-Path -Leaf $archivePath)  $((Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath).Hash)"
) | Set-Content -LiteralPath $hashManifest -Encoding UTF8

Write-Host "Release package created: $packageRoot"
Write-Host "Release archive created: $archivePath"
Write-Host "SHA-256 manifest created: $hashManifest"
