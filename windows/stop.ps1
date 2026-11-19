# WorkBuddy Manager —— 停止本机服务（Windows / PowerShell）
# 项目根在上一级（server/、web/out、.env 所在处）；兄弟脚本用 $scriptDir 找
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
& (Join-Path $scriptDir 'service-tools.ps1') stop

$upstreamStop = Join-Path $root 'upstream\stop-workbuddy2api.cmd'
if (Test-Path $upstreamStop) {
    & cmd.exe /c $upstreamStop
}
Write-Host "已停止所有 WorkBuddy 服务。"
