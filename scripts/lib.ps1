# Shared helpers for the Windows PowerShell wrappers. Dot-sourced, not executed.
# Works on Windows PowerShell 5.1 and PowerShell 7+.

$script:KgwRoot = Split-Path -Parent $PSScriptRoot
$script:SupportedPython = @('3.13', '3.12', '3.11', '3.14')

function Write-KgwFail([string]$Message, [string]$Hint) {
    Write-Host "[FAIL] $Message" -ForegroundColor Red
    if ($Hint) { Write-Host "       $Hint" }
}

function Test-KgwPython([string]$Exe, [string[]]$PreArgs) {
    try {
        $out = & $Exe @PreArgs -c "import sys, platform; print('%d.%d %s' % (sys.version_info[0], sys.version_info[1], platform.machine()))" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
        $parts = "$out".Trim().Split(' ')
        if ($script:SupportedPython -contains $parts[0]) { return $parts }
    } catch { }
    return $null
}

# Returns @{ Exe = <path>; Args = <array> } for a supported Python, or $null.
function Find-KgwPython {
    if ($env:KGW_PYTHON) {
        if (Test-KgwPython $env:KGW_PYTHON @()) { return @{ Exe = $env:KGW_PYTHON; Args = @() } }
    }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($v in $script:SupportedPython) {
            if (Test-KgwPython $launcher.Source @("-$v")) { return @{ Exe = $launcher.Source; Args = @("-$v") } }
        }
    }
    foreach ($name in @('python.exe', 'python3.exe')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        # Skip the Microsoft Store "App execution alias" stub, which opens the Store instead of running.
        if ($cmd -and $cmd.Source -notlike '*\WindowsApps\*') {
            if (Test-KgwPython $cmd.Source @()) { return @{ Exe = $cmd.Source; Args = @() } }
        }
    }
    return $null
}

function Get-KgwVenvPython {
    $py = Join-Path $script:KgwRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $py) { return $py }
    return $null
}

function Invoke-KgwManage([string[]]$Arguments) {
    $py = Get-KgwVenvPython
    if (-not $py) {
        Write-KgwFail 'The gateway is not installed yet.' 'Run .\setup.ps1 first.'
        exit 1
    }
    & $py (Join-Path $script:KgwRoot 'scripts\manage.py') @Arguments
    exit $LASTEXITCODE
}
