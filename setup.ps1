<#
.SYNOPSIS
  One-command installer for Windows 10/11 (PowerShell 5.1 or 7+). No Git Bash and no
  administrator rights needed.
.DESCRIPTION
  Checks Python/Claude Code, creates .venv, installs pinned dependencies, stores the
  Bedrock token and a generated proxy master key in Windows Credential Manager, and
  verifies the gateway. Safe to re-run.

  If script execution is blocked, run it for this process only (no machine-wide change):
    powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
.EXAMPLE
  .\setup.ps1
.EXAMPLE
  .\setup.ps1 --region us-east-1 --model-id us.moonshotai.kimi-k3
#>
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\lib.ps1')

Write-Host "==> Kimi Bedrock Gateway setup (PowerShell $($PSVersionTable.PSVersion), $([System.Environment]::OSVersion.VersionString), $env:PROCESSOR_ARCHITECTURE)"
if ($PSVersionTable.PSVersion.Major -lt 5) {
    Write-KgwFail 'PowerShell 5.1 or newer is required.' 'Install PowerShell 7: winget install Microsoft.PowerShell'
    exit 1
}
$py = Find-KgwPython
if (-not $py) {
    Write-KgwFail 'No supported Python (3.11 - 3.14) was found.' 'Install one: winget install Python.Python.3.12   (or https://www.python.org/downloads/windows/), then open a new terminal.'
    exit 1
}
if ($args -contains '--help' -or $args -contains '-h') {
    & $py.Exe @($py.Args) (Join-Path $PSScriptRoot 'scripts\manage.py') setup --help
    exit $LASTEXITCODE
}
& $py.Exe @($py.Args) (Join-Path $PSScriptRoot 'scripts\manage.py') bootstrap -- @args
exit $LASTEXITCODE
