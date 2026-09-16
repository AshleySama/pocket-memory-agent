$ErrorActionPreference = "Stop"
$env:PYTHONDONTWRITEBYTECODE = "1"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtual environment not found: $python. Run setup_venv.bat first."
}

Write-Host "== Pocket Memory regression suite =="
& $python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "== Local model smoke checks =="
& $python .\scripts\model_smoke.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python .\scripts\intelligence_smoke.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "Regression suite passed."
