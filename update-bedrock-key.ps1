<#
.SYNOPSIS
  Replace the Amazon Bedrock bearer token stored in Windows Credential Manager.
  The token is read without echo and validated before it replaces the old one.
.EXAMPLE
  .\update-bedrock-key.ps1 --restart
#>
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\lib.ps1')
Invoke-KgwManage (@('update-key') + $args)
