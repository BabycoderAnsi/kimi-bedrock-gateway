<#
.SYNOPSIS
  Launch Claude Code through the local gateway with Kimi K3. The environment is set for
  the Claude Code process only; your user/machine environment is not modified.
.EXAMPLE
  .\claude-kimi.ps1
#>
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\lib.ps1')
Invoke-KgwManage (@('launch-claude', '--') + $args)
