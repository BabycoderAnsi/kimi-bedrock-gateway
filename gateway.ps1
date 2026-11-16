<#
.SYNOPSIS
  Management commands: status | stop | restart | health [--live] | logs | version | doctor
  | config show|validate | set-region REGION | classifier status|enable|disable
  | rotate-master-key | scan-secrets | upgrade [--litellm X.Y.Z] | uninstall
  | check-updates | update [litellm|claude] | verify litellm|claude
#>
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\lib.ps1')
if ($args.Count -eq 0) { Invoke-KgwManage @('--help') }
Invoke-KgwManage $args
