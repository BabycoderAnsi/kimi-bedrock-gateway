<#
.SYNOPSIS
  Configure THIS PowerShell session to run Claude Code through the gateway.
.DESCRIPTION
  Dot-source it:   . .\scripts\claude-code-env.ps1
  Sets process-scoped environment variables only (nothing is written to the user or
  machine environment), removes conflicting variables such as ANTHROPIC_API_KEY, and
  defines a session-local 'claude' function that adds the launcher's auto-mode guard.
  The master key is read from Windows Credential Manager and never printed.
  Prefer .\claude-kimi.ps1, which also checks gateway health before launching.
#>
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $py)) { Write-Host '[FAIL] Run .\setup.ps1 first.'; return }
$json = & $py (Join-Path $root 'scripts\manage.py') '_claude-env' | Out-String
if ($LASTEXITCODE -ne 0 -or -not $json) { Write-Host '[FAIL] Could not read the gateway configuration.'; return }
$cfg = $json | ConvertFrom-Json
foreach ($name in $cfg.unset) { Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue }
foreach ($p in $cfg.set.PSObject.Properties) { Set-Item -LiteralPath "Env:$($p.Name)" -Value $p.Value }
$script:KgwClaudeArgs = @($cfg.claude_args)
$claudeExe = (Get-Command claude -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if ($claudeExe) {
    $global:KgwClaudeExe = $claudeExe
    $global:KgwClaudeArgs = $script:KgwClaudeArgs
    function global:claude { & $global:KgwClaudeExe @global:KgwClaudeArgs @args }
}
Remove-Variable json, cfg -ErrorAction SilentlyContinue
Write-Host "[ OK ] This session now uses the gateway at $env:ANTHROPIC_BASE_URL with model $env:ANTHROPIC_MODEL."
if ($global:KgwClaudeArgs.Count -gt 0) { Write-Host '       Auto mode is disabled for claude in this session (no Claude classifier configured).' }
Write-Host '       Start the gateway first (.\start.ps1). Close this window to undo.'
