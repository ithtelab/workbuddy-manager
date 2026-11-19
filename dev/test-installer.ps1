# WorkBuddy 安装器自动化测试：Win32 消息驱动（不依赖前台焦点，免疫弹窗抢焦）
# 定位控件用 UIAutomation，操作用 WM_SETTEXT / BM_CLICK。
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File dev\test-installer.ps1 [-SkipCleanup]
#   -SkipCleanup：保留已安装目录与数据，用于覆盖安装场景（此时会点「覆盖确认」）
param([switch]$SkipCleanup)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName System.Windows.Forms
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public class Win32Msg
{
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern IntPtr SendMessage(IntPtr hWnd, uint msg, IntPtr wParam, string lParam);

    [DllImport("user32.dll")]
    public static extern IntPtr SendMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern IntPtr PostMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int cmd);

    // powershell 不是前台进程时 SetForegroundWindow 会被系统拒绝；
    // 最小化再恢复可绕过前台锁定，让目标窗口拿到焦点
    public static void ForceForeground(IntPtr hWnd)
    {
        ShowWindow(hWnd, 6);   // SW_MINIMIZE
        ShowWindow(hWnd, 9);   // SW_RESTORE
        SetForegroundWindow(hWnd);
    }

    public const uint WM_SETTEXT = 0x000C;
    public const uint BM_CLICK = 0x00F5;

    public static void SetText(IntPtr hwnd, string text) { SendMessage(hwnd, WM_SETTEXT, IntPtr.Zero, text); }
    // 点击用 Post：按钮事件处理器里可能弹模态框（覆盖确认），同步 Send 会卡死测试脚本
    public static void Click(IntPtr hwnd) { PostMessage(hwnd, BM_CLICK, IntPtr.Zero, IntPtr.Zero); }
}
'@

$repoRoot = Split-Path -Parent $PSScriptRoot
$setupExe = Join-Path $repoRoot 'windows\launcher\dist\WorkBuddyManagerSetup.exe'
$targetDir = 'D:\ai-project\_wb_test_install'
$timeoutSec = 180

if (-not (Test-Path $setupExe)) { Write-Error "找不到安装包：$setupExe"; exit 1 }

# 安装进度诊断日志：完成页等状态改用「轮询该日志」判断（UIA 查询目标 UI 线程忙时会挂）
$diagLog = Join-Path $env:TEMP 'wb-install-diag.log'
Remove-Item $diagLog -Force -ErrorAction SilentlyContinue

$root = [System.Windows.Automation.AutomationElement]::RootElement

$proc = Start-Process $setupExe -PassThru

# 按进程 ID 精确定位主窗口（同名窗口可能来自其他残留实例）
$nameWinCond = New-Object System.Windows.Automation.AndCondition(
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty, '安装 WorkBuddy Manager')),
    (New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Window)))

function Find-InstallerWindow {
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline) {
        $cand = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $nameWinCond)
        if ($cand) {
            Write-Host ('  [diag] 找到窗口 PID=' + $cand.Current.ProcessId + '（新进程 PID=' + $proc.Id + '）')
            if ($cand.Current.ProcessId -eq $proc.Id) { return $cand }
        }
        Start-Sleep -Milliseconds 500
    }
    return $null
}

function Find-ChildByName($window, $namePattern, $classPattern) {
    $all = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($item in $all) {
        if ($item.Current.Name -like $namePattern) {
            # 正文 Label 的文字可能包含按钮名（如「单击下一步继续」），须按控件类过滤
            if ($classPattern -and ($item.Current.ClassName -notlike $classPattern)) { continue }
            return $item
        }
    }
    return $null
}

function Find-ChildByClass($window, $classPattern) {
    $all = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($item in $all) {
        if ($item.Current.ClassName -like $classPattern) { return $item }
    }
    return $null
}

function Test-PageMarker($window, $marker) {
    return ($null -ne (Find-ChildByName $window $marker))
}

# 点击「下一步」并确认页面已切换（窗口刚出现时首次点击可能被吞，需重试）
function Invoke-NextUntilPage($window, $pageMarker) {
    for ($attempt = 1; $attempt -le 6; $attempt++) {
        $btn = Find-ChildByName $window '*下一步*' '*BUTTON*'
        if ($btn) {
            [Win32Msg]::Click($btn.Current.NativeWindowHandle)
        }
        Start-Sleep -Milliseconds 1200
        if (Test-PageMarker $window $pageMarker) { return $true }
    }
    return $false
}

$win = Find-InstallerWindow
if (-not $win) { Write-Host 'FAIL: 安装窗口未出现'; if (-not $proc.HasExited) { $proc.Kill() }; exit 1 }
Write-Host ('OK: 安装窗口出现（' + $win.Current.Name + '）')

# 欢迎页 → 位置页
if (-not (Invoke-NextUntilPage $win '选择安装位置')) {
    Write-Host 'FAIL: 未能进入「选择安装位置」页'
    $proc.Kill(); exit 1
}
Write-Host 'OK: 已进入位置页'

# 等位置页出现（EDIT 输入框），直接 WM_SETTEXT 写入目标目录
$edit = $null
$deadline = (Get-Date).AddSeconds(10)
while ((Get-Date) -lt $deadline) {
    $edit = Find-ChildByClass $win '*EDIT*'
    if ($edit) { break }
    Start-Sleep -Milliseconds 400
}
if (-not $edit) {
    Write-Host 'FAIL: 未找到目录输入框，当前窗口控件：'
    $dbg = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition)
    Write-Host ('  元素数: ' + $dbg.Count)
    foreach ($item in $dbg) {
        Write-Host ('  ' + $item.Current.ClassName + ' | "' + $item.Current.Name + '"')
    }
    $proc.Kill(); exit 1
}
if ($SkipCleanup) {
    # 已安装路径应被自动回填到目录框（注册表 InstallLocation），覆盖安装无需手敲
    Check ($edit.Current.Name -eq $targetDir) '已安装路径自动回填到安装目录'
}
[Win32Msg]::SetText($edit.Current.NativeWindowHandle, $targetDir)
Write-Host ('OK: 目录已写入 ' + $targetDir)

# 位置页 → 触发安装：发送 ENTER（AcceptButton = 「下一步」）并以诊断日志「开始安装」
# 确认生效；键盘事件偶尔被前台切换吞掉，未生效则重发。
# 覆盖确认框（-SkipCleanup）出现时点一次「是」并等它消失——期间绝不重发 ENTER：
# 被确认框禁用的主窗按钮收到 BM_CLICK 仍会触发 Click 事件，重试会滚雪球弹出几十个确认框。
$installed = $false
$clickDeadline = (Get-Date).AddSeconds(40)
$lastSend = (Get-Date).AddSeconds(-10)
$pendingDialog = [IntPtr]::Zero
while ((Get-Date) -lt $clickDeadline) {
    if ($SkipCleanup) {
        $mb = $root.FindFirst([System.Windows.Automation.TreeScope]::Children,
            (New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::NameProperty, '覆盖安装')))
        if ($mb -ne [IntPtr]::Zero) {
            if ($pendingDialog -ne $mb) {
                # 新弹出的确认框：只点一次「是」，然后等它消失
                $yes = Find-ChildByName $mb '*是*' '*BUTTON*'
                if ($yes) { [Win32Msg]::Click($yes.Current.NativeWindowHandle) }
                $pendingDialog = $mb
                Write-Host 'OK: 覆盖安装确认框已确认'
            }
            Start-Sleep -Milliseconds 300
            continue
        }
        $pendingDialog = [IntPtr]::Zero
    }
    $diag = Get-Content $diagLog -Encoding UTF8 -ErrorAction SilentlyContinue
    if (($diag | Select-Object -Last 5) -match '开始安装') { $installed = $true; break }
    if (((Get-Date) - $lastSend).TotalMilliseconds -gt 2000) {
        [Win32Msg]::ForceForeground($proc.MainWindowHandle)
        Start-Sleep -Milliseconds 250
        [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
        $lastSend = Get-Date
    }
    Start-Sleep -Milliseconds 300
}
if (-not $installed) { Write-Host 'FAIL: 未能触发安装（重试后仍无效）'; if (-not $proc.HasExited) { $proc.Kill() }; exit 1 }
Write-Host 'OK: 安装已开始（诊断日志确认）'

# 等待安装完成（轮询诊断日志：最后一步「注册表写入完成」= 向导进入完成页），
# 不点「完成」避免启动应用。UIA 查询在目标 UI 线程忙时会挂，这里一律用文件判断。
$deadline = (Get-Date).AddSeconds($timeoutSec)
$doneShown = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 1
    $diag = Get-Content $diagLog -Encoding UTF8 -ErrorAction SilentlyContinue
    if (($diag | Select-Object -Last 1) -match '注册表写入完成') { $doneShown = $true; break }
}
if (-not $doneShown) { Write-Host 'FAIL: 等待安装完成页超时'; if (-not $proc.HasExited) { $proc.Kill() }; exit 1 }
Write-Host 'OK: 安装完成页已出现'

# ── 产物核验 ────────────────────────────────────────────────
function Check($ok, $label) { if ($ok) { Write-Host ('OK: ' + $label) } else { Write-Host ('FAIL: ' + $label); $script:failed = $true } }
$failed = $false

Check (Test-Path (Join-Path $targetDir 'server\main.py')) 'server\main.py 已解压'
Check (Test-Path (Join-Path $targetDir '.runtime\python\python.exe')) '预置 Python 运行时存在'
Check (Test-Path (Join-Path $targetDir 'upstream\wb2api.exe')) '上游 wb2api.exe 存在'
Check (Test-Path (Join-Path $targetDir 'web\out\index.html')) '前端产物存在'
Check (Test-Path (Join-Path $targetDir 'windows\service-tools.ps1')) 'windows 脚本已就位'
Check (Test-Path (Join-Path $targetDir 'WorkBuddyManager.exe')) '主程序已复制'
Check (Test-Path (Join-Path $targetDir 'Uninstall.exe')) '卸载程序已复制'
Check (Test-Path (Join-Path $targetDir '.version')) '版本标记存在'
Check (Test-Path 'C:\Users\Lenovo\Desktop\WorkBuddy Manager.lnk') '桌面快捷方式已创建'
Check (Test-Path (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\WorkBuddy Manager\WorkBuddy Manager.lnk')) '开始菜单快捷方式已创建'
Check (Test-Path (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\WorkBuddy Manager\卸载 WorkBuddy Manager.lnk')) '开始菜单卸载入口已创建'
Check (Test-Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\WorkBuddyManager') '注册表卸载项已注册'
if ($SkipCleanup) {
    # 覆盖安装：个人数据必须保留
    Check (Test-Path (Join-Path $targetDir '.env')) '覆盖安装：.env 保留'
    Check (Test-Path (Join-Path $targetDir 'data')) '覆盖安装：data 目录保留'
}

# 点「完成」→ 安装窗口必须关闭（Application.Run 模式下需显式 Close 的回归验证）；
# 完成页默认勾选「立即启动」，随后顺带清理被启动的应用
[Win32Msg]::ForceForeground($proc.MainWindowHandle)
Start-Sleep -Milliseconds 300
[System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
Start-Sleep -Seconds 3
$proc.Refresh()
Check ($proc.HasExited -or $proc.MainWindowHandle -eq 0) '点「完成」后安装窗口关闭'
Get-Process -Name 'WorkBuddyManager' -ErrorAction SilentlyContinue | Stop-Process -Force

if (-not $proc.HasExited) { $proc.Kill() }
if ($failed) { Write-Host 'RESULT: FAIL'; exit 1 }
Write-Host 'RESULT: PASS'
