param(
    [string]$OutputPath = "third-party-licenses.csv"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtual environment not found: $python. Run setup_venv.bat first."
}

& $python -m pip install pip-licenses
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $python -m piplicenses --format=csv --with-urls --output-file (Join-Path $root $OutputPath)
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Third-party license inventory created: $(Join-Path $root $OutputPath)"
