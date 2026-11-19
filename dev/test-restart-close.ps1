# WorkBuddy 启动器测试：重启按钮 + X 关闭行为（取消/最小化+记住/记住生效/退出）+ 托盘间接验证
# 定位走 Win32（dev\ui-helpers.ps1），不依赖目标 UI 线程配合。
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'ui-helpers.ps1')

$installDir = 'D:\ai-project\_wb_test_install'
$launcherExe = Join-Path $installDir 'WorkBuddyManager.exe'
$testPort = 17864
$settingsFile = Join-Path $env:APPDATA 'WorkBuddyManager\launcher.conf'
$script:failed = $false

function Check($ok, $label) {
    if ($ok) { Write-Host ('OK: ' + $label) } else { Write-Host ('FAIL: ' + $label); $script:failed = $true }
}

function Wait-Healthy($seconds) {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $seconds) {
        try {
            $resp = Invoke-WebRequest -Uri ('http://127.0.0.1:' + $testPort + '/api/healthz') -UseBasicParsing -TimeoutSec 2
            if ($resp.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Wait-CloseDialog($proc, $mainHwnd, $seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    $lastClick = (Get-Date).AddSeconds(-10)
    while ((Get-Date) -lt $deadline) {
        $dlg = [WbTop]::FindProcessWindow($proc.Id, $mainHwnd)
        if ($dlg -ne [IntPtr]::Zero) { return $dlg }
        # 前台切换可能吞掉首次点击：未弹出时重试点击 X（重复点击落在窗口空白区，无副作用）
        if (((Get-Date) - $lastClick).TotalMilliseconds -gt 1500) {
            [WbWin]::ClickCloseButton($mainHwnd)
            $lastClick = Get-Date
        }
        Start-Sleep -Milliseconds 400
    }
    return [IntPtr]::Zero
}

function Wait-Gone($seconds) {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $seconds) {
        try {
            Invoke-WebRequest -Uri ('http://127.0.0.1:' + $testPort + '/api/healthz') -UseBasicParsing -TimeoutSec 2 | Out-Null
        } catch { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Start-Launcher {
    # 清理残留 + 确保 .env 存在且端口为测试端口 + 启动，返回 (proc, mainHwnd)
    Get-Process -Name 'WorkBuddyManager' -ErrorAction SilentlyContinue | Stop-Process -Force
    foreach ($p in @(7864, 17864, 17865)) {
        Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
            $l = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
            if ($l -and $l.Path -like ($installDir + '*')) { Stop-Process -Id $l.Id -Force }
        }
    }
    Start-Sleep -Seconds 1
    $envFile = Join-Path $installDir '.env'
    if (-not (Test-Path $envFile)) {
        # 首次启动前先让 prepare 生成 .env（默认 7864），再替换为测试端口
        $prepare = Join-Path $installDir 'windows\prepare.ps1'
        & powershell -NoProfile -ExecutionPolicy Bypass -File $prepare 2>&1 | Out-Null
    }
    if (Test-Path $envFile) {
        $lines = Get-Content $envFile
        $lines = $lines | ForEach-Object { if ($_ -match '^WB_MANAGER_PORT=') { 'WB_MANAGER_PORT=' + $testPort } else { $_ } }
        Set-Content $envFile $lines -Encoding UTF8
    }
    $p = Start-Process $launcherExe -PassThru
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline) {
        $p.Refresh()
        if ($p.MainWindowHandle -ne 0) { return @($p, $p.MainWindowHandle) }
        Start-Sleep -Milliseconds 500
    }
    return @($p, [IntPtr]::Zero)
}

# ── 准备 ────────────────────────────────────────────────
Remove-Item $settingsFile -Force -ErrorAction SilentlyContinue
$pair = Start-Launcher
$proc = $pair[0]
$win = $pair[1]
if ($win -eq [IntPtr]::Zero) { Write-Host 'FAIL: 控制窗口未出现'; $proc.Kill(); exit 1 }
Write-Host 'OK: 控制窗口出现'

# ── 1. 重启按钮 ─────────────────────────────────────────
$startBtn = [WbWin]::FindChild($win, 'BUTTON', '启动项目')
[WbWin]::Click($startBtn)
if (Wait-Healthy 150) { Write-Host 'OK: 启动项目 → 服务就绪' } else { Check $false '启动项目'; $proc.Kill(); exit 1 }

$restartBtn = [WbWin]::FindChild($win, 'BUTTON', '重启项目')
if ($restartBtn -eq [IntPtr]::Zero) { Check $false '找到重启按钮'; $proc.Kill(); exit 1 }
[WbWin]::Click($restartBtn)
Write-Host 'OK: 已点击「重启项目」，等待服务恢复…'
$restarted = $false
$sw = [System.Diagnostics.Stopwatch]::StartNew()
# 重启经历「停止→启动」：等待健康检查在 90 秒内恢复
$restarted = Wait-Healthy 90
Check $restarted '重启项目后服务恢复运行中'

# ── 2. X → 取消 ─────────────────────────────────────────
[WbWin]::ClickCloseButton($win)
$dlg = Wait-CloseDialog $proc $win 4
Check ($dlg -ne [IntPtr]::Zero) '点 X 弹出关闭选择对话框'
$cancelBtn = [WbWin]::FindChild($dlg, 'BUTTON', '取消')
[WbWin]::Click($cancelBtn)
Start-Sleep -Seconds 1
Check ([WbWin]::IsWindowVisible($win) -and [WbTop]::FindProcessWindow($proc.Id, $win) -eq [IntPtr]::Zero) '点「取消」后窗口保留'

# ── 3. X → 最小化到托盘（勾选记住）─────────────────────
[WbWin]::ClickCloseButton($win)
$dlg = Wait-CloseDialog $proc $win 4
Check ($dlg -ne [IntPtr]::Zero) '再次点 X 弹出对话框'
$remember = [WbWin]::FindChildByPrefix($dlg, 'BUTTON', '记住我的选择')
if ($remember -ne [IntPtr]::Zero) { [WbWin]::Click($remember) }
$minBtn = [WbWin]::FindChild($dlg, 'BUTTON', '最小化到托盘')
[WbWin]::Click($minBtn)
Start-Sleep -Seconds 1
$proc.Refresh()
Check (-not [WbWin]::IsWindowVisible($win) -and $proc.MainWindowHandle -eq 0) '最小化到托盘：主窗口隐藏且进程存活'
Check ((Test-Path $settingsFile) -and ((Get-Content $settingsFile -ErrorAction SilentlyContinue) -match 'close_action=minimize')) '记住选择已写入配置'

# ── 4. 记住生效：重启启动器后点 X 直接最小化 ────────────
if (-not $proc.HasExited) { $proc.Kill() }
Start-Sleep -Seconds 1
$pair = Start-Launcher
$proc = $pair[0]
$win = $pair[1]
if ($win -eq [IntPtr]::Zero) { Check $false '重启启动器窗口出现'; exit 1 }
[WbWin]::ClickCloseButton($win)
Start-Sleep -Seconds 2
$proc.Refresh()
# 记住选择生效：点击 X 后既不弹对话框也不退出，直接最小化（主窗口隐藏）
Check ($proc.MainWindowHandle -eq 0 -and -not $proc.HasExited -and [WbTop]::FindProcessWindow($proc.Id, $win) -eq [IntPtr]::Zero) '记住选择生效：点 X 不再询问、直接最小化到托盘'

# ── 5. 退出程序路径（还原询问后）────────────────────────
if (-not $proc.HasExited) { $proc.Kill() }
Remove-Item $settingsFile -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
$pair = Start-Launcher
$proc = $pair[0]
$win = $pair[1]
# 先把服务启动起来，再验证「退出启动器不影响后台服务」
$startBtn = [WbWin]::FindChild($win, 'BUTTON', '启动项目')
[WbWin]::Click($startBtn)
if (-not (Wait-Healthy 150)) { Check $false '退出测试前启动服务'; $proc.Kill(); exit 1 }
Write-Host 'OK: 服务已启动（为退出测试准备）'
[WbWin]::ClickCloseButton($win)
$dlg = Wait-CloseDialog $proc $win 4
$exitBtn = [WbWin]::FindChild($dlg, 'BUTTON', '退出程序')
[WbWin]::Click($exitBtn)
Start-Sleep -Seconds 2
$proc.Refresh()
Check $proc.HasExited '点「退出程序」后启动器进程退出'
Check (Wait-Healthy 10) '退出启动器后项目服务继续在后台运行'

# ── 收尾 ────────────────────────────────────────────────
& (Join-Path $installDir 'windows\service-tools.ps1') stop 2>&1 | Out-Null
Remove-Item $settingsFile -Force -ErrorAction SilentlyContinue
if ($failed) { Write-Host 'RESULT: FAIL'; exit 1 }
Write-Host 'RESULT: PASS'
