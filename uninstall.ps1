<#
.SYNOPSIS
  Remove what setup created. Asks before deleting the venv, settings or credentials;
  credentials are kept unless you choose to delete them. The repository is never deleted.
.EXAMPLE
  .\uninstall.ps1 --remove-venv
#>
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\lib.ps1')
$exe = Get-KgwVenvPython
$pre = @()
if (-not $exe) {
    $found = Find-KgwPython
    if (-not $found) { Write-KgwFail 'No Python found to run the uninstaller.'; exit 1 }
    $exe = $found.Exe; $pre = $found.Args
}
$env:KGW_NO_REEXEC = '1'
& $exe @pre (Join-Path $PSScriptRoot 'scripts\manage.py') uninstall @args
$code = $LASTEXITCODE
$venv = Join-Path $PSScriptRoot '.venv'
if (Test-Path -LiteralPath (Join-Path $venv '.kgw-remove-pending')) {
    Start-Sleep -Seconds 1
    Remove-Item -LiteralPath $venv -Recurse -Force
    Write-Host "[ OK ] Removed $venv"
}
exit $code
