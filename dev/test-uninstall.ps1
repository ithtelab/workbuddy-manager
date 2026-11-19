# WorkBuddy 卸载流程自动化测试
# 参数：-KeepData 走「保留数据」卸载；默认走「全部删除」卸载。
# 前置：已完成安装（dev\test-installer.ps1）。
param([switch]$KeepData)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'ui-helpers.ps1')

$installDir = 'D:\ai-project\_wb_test_install'
$uninstallExe = Join-Path $installDir 'Uninstall.exe'
$desktopLink = 'C:\Users\Lenovo\Desktop\WorkBuddy Manager.lnk'
$menuDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\WorkBuddy Manager'
$regKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\WorkBuddyManager'
$script:failed = $false

function Check($ok, $label) {
    if ($ok) { Write-Host ('OK: ' + $label) } else { Write-Host ('FAIL: ' + $label); $script:failed = $true }
}

if (-not (Test-Path $uninstallExe)) { Write-Error "未找到卸载程序：$uninstallExe"; exit 1 }

if ($KeepData) {
    # 先准备出「可保留的数据」：prepare 会生成 .env 与 data/（幂等），否则目录里没有数据可验证
    $prepare = Join-Path $installDir 'windows\prepare.ps1'
    & powershell -NoProfile -ExecutionPolicy Bypass -File $prepare 2>&1 | Out-Null
    New-Item -ItemType Directory (Join-Path $installDir 'data\probe') -Force | Out-Null
    Set-Content (Join-Path $installDir 'data\probe\keep.txt') 'keep-test'
}

# 卸载前：.env 端口改为测试端口（17864），避免卸载停服时碰到机器上 7864 的其他实例
$envFile = Join-Path $installDir '.env'
if (Test-Path $envFile) {
    $lines = Get-Content $envFile
    $lines = $lines | ForEach-Object { if ($_ -match '^WB_MANAGER_PORT=') { 'WB_MANAGER_PORT=17864' } else { $_ } }
    Set-Content $envFile $lines -Encoding UTF8
}
# 让服务处于运行状态（覆盖「卸载时自动停服」路径）
& (Join-Path $installDir 'windows\service-tools.ps1') start 2>&1 | Out-Null
Start-Sleep -Seconds 3
$wasRunning = $false
try {
    $resp = Invoke-WebRequest -Uri 'http://127.0.0.1:17864/api/healthz' -UseBasicParsing -TimeoutSec 2
    $wasRunning = ($resp.StatusCode -eq 200)
} catch { }Write-Host ('卸载前服务运行中: ' + $wasRunning)

Get-Process -Name 'WorkBuddyManager' -ErrorAction SilentlyContinue | Stop-Process -Force
$proc = Start-Process $uninstallExe -PassThru

# 等卸载窗口（确认页）
$win = [IntPtr]::Zero
$deadline = (Get-Date).AddSeconds(15)
while ((Get-Date) -lt $deadline) {
    $proc.Refresh()
    if ($proc.MainWindowHandle -ne 0) { $win = $proc.MainWindowHandle; break }
    Start-Sleep -Milliseconds 500
}
if ($win -eq [IntPtr]::Zero) { Write-Host 'FAIL: 卸载窗口未出现'; $proc.Kill(); exit 1 }
Write-Host 'OK: 卸载窗口出现'

# 勾选/不勾选「保留数据」
$keepBox = [WbWin]::FindChildByPrefix($win, 'BUTTON', '保留数据')
if ($keepBox -ne [IntPtr]::Zero -and $KeepData) {
    [WbWin]::Click($keepBox)  # 勾选
    Write-Host 'OK: 已勾选「保留数据」'
}

# 点「卸载(U)」（按钮文本带加速键尾巴，用前缀匹配）
$uninstallBtn = [WbWin]::FindChildByPrefix($win, 'BUTTON', '卸载')
if ($uninstallBtn -eq [IntPtr]::Zero) { Write-Host 'FAIL: 未找到「卸载」按钮'; $proc.Kill(); exit 1 }
[WbWin]::Click($uninstallBtn)
Write-Host 'OK: 已点击「卸载」，等待确认框…'

# 确认 MessageBox → 点「是」
$mb = [IntPtr]::Zero
$deadline = (Get-Date).AddSeconds(10)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 400
    $mb = [WbTop]::FindDialog($proc.Id)
    if ($mb -ne [IntPtr]::Zero) { break }
}
if ($mb -eq [IntPtr]::Zero) { Write-Host 'FAIL: 未出现确认框'; $proc.Kill(); exit 1 }
$yesBtn = [WbWin]::FindChildByPrefix($mb, 'BUTTON', '是')
[WbWin]::Click($yesBtn)
Write-Host 'OK: 已确认卸载，等待清理完成…'

# 等完成页（标题含「卸载完成」，清理 5000+ 文件需要时间）
$doneShown = $false
$deadline = (Get-Date).AddSeconds(180)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    $proc.Refresh()
    if ($proc.HasExited) { break }
    if ([WbWin]::FindChildByPrefix($win, 'STATIC', '卸载完成') -ne [IntPtr]::Zero) { $doneShown = $true; break }
}
if (-not $doneShown) { Write-Host 'FAIL: 等待卸载完成页超时'; if (-not $proc.HasExited) { $proc.Kill() }; exit 1 }
Write-Host 'OK: 卸载完成页已出现'

# 点「完成(F)」→ 自删除
$finishBtn = [WbWin]::FindChildByPrefix($win, 'BUTTON', '完成')
[WbWin]::Click($finishBtn)
Start-Sleep -Seconds 4
$proc.Refresh()
Write-Host ('卸载进程已退出: ' + $proc.HasExited)

# 自删除延迟 1–2 秒 + 删除 5000 文件的目录需要时间，轮询目录消失
$dirGone = $false
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    if (-not (Test-Path $installDir)) { $dirGone = $true; break }
}
Write-Host ('安装目录已消失: ' + $dirGone)

# ── 核验 ────────────────────────────────────────────────
if ($KeepData) {
    # 保留模式：数据在、程序没了
    Check (Test-Path (Join-Path $installDir '.env')) '保留数据：.env 仍在'
    Check (Test-Path (Join-Path $installDir 'data')) '保留数据：data 目录仍在'
    Check (-not (Test-Path (Join-Path $installDir 'WorkBuddyManager.exe'))) '保留数据：主程序已删除'
    Check (-not (Test-Path (Join-Path $installDir 'server'))) '保留数据：程序目录已删除'
} else {
    Check $dirGone '全删模式：安装目录已完全删除'
}
Check (-not (Test-Path $desktopLink)) '桌面快捷方式已删除'
Check (-not (Test-Path $menuDir)) '开始菜单目录已删除'
Check (-not (Test-Path $regKey)) '注册表卸载项已删除'
Check (-not (Test-Path (Join-Path $installDir 'Uninstall.exe.old'))) '无 .old 残留'

# 收尾：清掉测试目录残留（保留数据模式的目录）
if (Test-Path $installDir) { Remove-Item $installDir -Recurse -Force }
if ($failed) { Write-Host 'RESULT: FAIL'; exit 1 }
Write-Host 'RESULT: PASS'
