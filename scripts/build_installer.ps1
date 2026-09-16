param(
    [switch]$SkipTests,
    [switch]$UseExistingPayload,
    [string]$OutputDir = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtual environment not found: $python. Run setup_venv.bat first."
}
if (-not $OutputDir) { $OutputDir = Join-Path $root "release" }

$version = & $python -c "from pocket_memory.version import APP_VERSION; print(APP_VERSION)"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$packageRoot = Join-Path $OutputDir "PocketMemory-$version-portable"
if (-not $UseExistingPayload) {
    $releaseArgs = @{
        OnlineModelBootstrap = $true
        OutputDir = $OutputDir
    }
    if ($SkipTests) { $releaseArgs.SkipTests = $true }
    & (Join-Path $PSScriptRoot "build_release.ps1") @releaseArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
if (-not (Test-Path -LiteralPath (Join-Path $packageRoot "PocketMemory.exe"))) {
    throw "Online-model package is incomplete: $packageRoot"
}

$setupName = "PocketMemory-$version-setup"
$buildRoot = Join-Path $env:TEMP "PocketMemory-installer-$version"
$distRoot = Join-Path $buildRoot "dist"
$workRoot = Join-Path $buildRoot "work"
# A one-file PyInstaller setup has to unpack the complete application payload
# before its window can paint. The payload is ~200 MB even without Qwen, so use
# a directory package: setup.exe starts immediately after the ZIP is extracted.
& $python -m PyInstaller --noconfirm --clean --log-level ERROR --onedir --windowed --name $setupName `
    --distpath $distRoot --workpath $workRoot `
    --collect-all webview `
    --add-data "$packageRoot;payload" `
    (Join-Path $PSScriptRoot "installer.py")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$installerPath = Join-Path $distRoot "$setupName\$setupName.exe"
if (-not (Test-Path -LiteralPath $installerPath)) {
    throw "Installer output is incomplete: $installerPath"
}
$installerPackage = Join-Path $OutputDir $setupName
if (Test-Path -LiteralPath $installerPackage) {
    throw "Installer directory already exists: $installerPackage"
}
Copy-Item -LiteralPath (Split-Path -Parent $installerPath) -Destination $installerPackage -Recurse
Compress-Archive -LiteralPath $installerPackage -DestinationPath (Join-Path $OutputDir "$setupName.zip")
Write-Host "Installer directory created: $installerPackage"
Write-Host "Installer archive created: $(Join-Path $OutputDir "$setupName.zip")"
