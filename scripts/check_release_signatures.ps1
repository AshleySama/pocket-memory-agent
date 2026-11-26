# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

param(
    [Parameter(Mandatory=$true)][string]$ProgramExe,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-fA-F0-9]{40}$')][string]$ExpectedSignerThumbprint,
    [string]$RecoveryExe = '',
    [string]$LegacyWorker = '',
    [Parameter(Mandatory=$true)][string]$OutputPath
)

# Release-side gate only: never signs files, changes certificate trust, disables
# endpoint protection, or launches an application. Run before manifest creation.
$ErrorActionPreference = 'Stop'
$targets = @($ProgramExe)
if ($RecoveryExe) { $targets += $RecoveryExe }
if ($LegacyWorker) { $targets += $LegacyWorker }
$records = foreach ($target in $targets) {
    $item = Get-Item -LiteralPath $target
    if ($item.PSIsContainer -or $item.Extension -ne '.exe') { throw 'Expected an explicit EXE file.' }
    $signature = Get-AuthenticodeSignature -LiteralPath $item.FullName
    $thumbprint = if ($signature.SignerCertificate) { $signature.SignerCertificate.Thumbprint } else { '' }
    $valid = $signature.Status.ToString() -eq 'Valid' -and
        $thumbprint -ieq $ExpectedSignerThumbprint -and
        $null -ne $signature.TimeStamperCertificate
    [pscustomobject]@{
        file = $item.FullName
        sha256 = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        signature_status = $signature.Status.ToString()
        signer_thumbprint = $thumbprint
        timestamped = $null -ne $signature.TimeStamperCertificate
        passed = $valid
    }
}
$passed = @($records | Where-Object { -not $_.passed }).Count -eq 0
$report = [pscustomobject]@{
    check = 'authenticode-release-preflight'
    passed = $passed
    expected_signer_thumbprint = $ExpectedSignerThumbprint.ToUpperInvariant()
    endpoint_policy_verified = $false
    files = @($records)
    limitation = 'Valid signatures identify the publisher; they do not prove enterprise endpoint approval.'
}
$parent = Split-Path -Parent $OutputPath
if ($parent -and -not (Test-Path -LiteralPath $parent -PathType Container)) { throw 'Output parent must already exist.' }
$report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $OutputPath -Encoding UTF8
if (-not $passed) {
    Write-Error 'Release signature gate failed. Do not label this build as trusted enterprise deployment. Files were not modified.' -ErrorAction Continue
    exit 2
}
Write-Output 'Signature gate passed. Enterprise policy acceptance is still a separate release check.'
