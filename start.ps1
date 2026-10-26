<#
.SYNOPSIS
  Start the LiteLLM proxy. Foreground by default (Ctrl+C stops it).
  Add --background to detach; stop it with .\gateway.ps1 stop.
#>
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\lib.ps1')
Invoke-KgwManage (@('start') + $args)
