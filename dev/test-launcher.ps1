# WorkBuddy 启动器自动化测试：控制窗口启动/关闭项目 + 状态 + 配置
# 定位全部走 Win32（EnumChildWindows），不依赖 UI 线程配合，免疫浏览器启动/弹窗导致的 UIA 挂起。
# 前置：已完成安装（dev\test-installer.ps1 PASS）。测试用独立端口 17864，不碰常见默认端口。
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public class WbWin
{
    public delegate bool EnumProc(IntPtr hwnd, IntPtr lp);

    [DllImport("user32.dll")]
    public static extern bool EnumChildWindows(IntPtr parent, EnumProc cb, IntPtr lp);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowText(IntPtr h, StringBuilder sb, int max);
    [DllImport("user32.dll")]
    public static extern int GetClassName(IntPtr h, StringBuilder sb, int max);
    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern IntPtr SendMessage(IntPtr hWnd, uint msg, IntPtr wParam, string lParam);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern IntPtr SendMessage(IntPtr hWnd, uint msg, IntPtr wParam, StringBuilder lParam);
    [DllImport("user32.dll")]
    public static extern IntPtr SendMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    public struct RECT { public int Left, Top, Right, Bottom; }
    public const uint WM_SETTEXT = 0x000C;
    public const uint WM_GETTEXT = 0x000D;
    public const uint BM_CLICK = 0x00F5;

    // EDIT 控件的文本读取需要目标 UI 线程处理 WM_GETTEXT（忙时长时间阻塞），
    // 一律跳过（输入框改按序号定位）；BUTTON/STATIC 的标题走 win32k 缓存，安全。
    public static List<object[]> Children(IntPtr parent)
    {
        var list = new List<object[]>();
        EnumProc cb = delegate(IntPtr hwnd, IntPtr lp)
        {
            var cls = new StringBuilder(256);
            GetClassName(hwnd, cls, 256);
            string clsName = cls.ToString();
            string text = string.Empty;
            if (clsName.IndexOf("EDIT", StringComparison.OrdinalIgnoreCase) < 0)
            {
                var txt = new StringBuilder(256);
                GetWindowText(hwnd, txt, 256);
                text = txt.ToString();
            }
            list.Add(new object[] { hwnd, clsName, text });
            return true;
        };
        EnumChildWindows(parent, cb, IntPtr.Zero);
        return list;
    }

    public static IntPtr FindChild(IntPtr parent, string classPart, string textExact)
    {
        foreach (object[] item in Children(parent))
        {
            IntPtr hwnd = (IntPtr)item[0];
            string cls = (string)item[1];
            string txt = (string)item[2];
            if (cls.IndexOf(classPart, StringComparison.OrdinalIgnoreCase) < 0) { continue; }
            if (textExact != null && txt != textExact) { continue; }
            return hwnd;
        }
        return IntPtr.Zero;
    }

    public static void Click(IntPtr hwnd) { SendMessage(hwnd, BM_CLICK, IntPtr.Zero, IntPtr.Zero); }
    public static void SetText(IntPtr hwnd, string text) { SendMessage(hwnd, WM_SETTEXT, IntPtr.Zero, text); }
}

public class WbTop
{
    public delegate bool EnumProc(IntPtr hwnd, IntPtr lp);
    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumProc cb, IntPtr lp);
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")]
    public static extern int GetClassName(IntPtr h, StringBuilder sb, int max);

    // 找某进程的模态对话框（#32770）
    public static IntPtr FindDialog(int pid)
    {
        IntPtr found = IntPtr.Zero;
        EnumProc cb = delegate(IntPtr h, IntPtr lp)
        {
            uint wpid;
            GetWindowThreadProcessId(h, out wpid);
            if (wpid == (uint)pid)
            {
                var cls = new StringBuilder(64);
                GetClassName(h, cls, 64);
                if (cls.ToString() == "#32770") { found = h; return false; }
            }
            return true;
        };
        EnumWindows(cb, IntPtr.Zero);
        return found;
    }
}
'@

$repoRoot = Split-Path -Parent $PSScriptRoot
$installDir = 'D:\ai-project\_wb_test_install'
$launcherExe = Join-Path $installDir 'WorkBuddyManager.exe'
$testPort = 17864          # 独立测试端口，避开机器上可能存在的 7864 生产实例

if (-not (Test-Path $launcherExe)) { Write-Error "未找到启动器：$launcherExe"; exit 1 }

# 清理上一次运行残留：先按 .env 停服务，再杀安装目录下的监听进程与启动器窗口
& (Join-Path $installDir 'windows\service-tools.ps1') stop 2>&1 | Out-Null
Get-Process -Name 'WorkBuddyManager' -ErrorAction SilentlyContinue | Stop-Process -Force
foreach ($p in @(7864, 17864)) {
    Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
        $listener = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
        if ($listener -and $listener.Path -like ($installDir + '*')) {
            Stop-Process -Id $listener.Id -Force
            Write-Host ('清理残留进程: ' + $listener.Id + ' (端口 ' + $p + ')')
        }
    }
}
Start-Sleep -Seconds 2
# 测试端口写入 .env
$envFile = Join-Path $installDir '.env'
if (Test-Path $envFile) {
    $lines = Get-Content $envFile
    $lines = $lines | ForEach-Object { if ($_ -match '^WB_MANAGER_PORT=') { 'WB_MANAGER_PORT=' + $testPort } else { $_ } }
    Set-Content $envFile $lines -Encoding UTF8
}

$proc = Start-Process $launcherExe -PassThru

function Wait-MainWindow {
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline) {
        $proc.Refresh()
        if ($proc.MainWindowHandle -ne 0) { return $proc.MainWindowHandle }
        Start-Sleep -Milliseconds 500
    }
    return [IntPtr]::Zero
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

function Wait-Gone {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt 60) {
        try {
            $resp = Invoke-WebRequest -Uri ('http://127.0.0.1:' + $testPort + '/api/healthz') -UseBasicParsing -TimeoutSec 2
        } catch { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

$win = Wait-MainWindow
if ($win -eq [IntPtr]::Zero) { Write-Host 'FAIL: 控制窗口未出现'; $proc.Kill(); exit 1 }
[WbWin]::SetForegroundWindow($win) | Out-Null
Write-Host 'OK: 控制窗口出现'
Start-Sleep -Seconds 1

# 截图初始状态（应显示「未运行」与端口 17864）
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Drawing;

public class WinCap6
{
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hwnd, IntPtr hdc, uint flags);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);
    public struct RECT { public int Left, Top, Right, Bottom; }

    public static void Capture(IntPtr hwnd, string path)
    {
        RECT r;
        GetWindowRect(hwnd, out r);
        int w = r.Right - r.Left;
        int h = r.Bottom - r.Top;
        if (w < 10) { w = 400; }
        if (h < 10) { h = 200; }
        using (var bmp = new Bitmap(w, h))
        using (var g = Graphics.FromImage(bmp))
        {
            IntPtr hdc = g.GetHdc();
            PrintWindow(hwnd, hdc, 2);
            g.ReleaseHdc(hdc);
            bmp.Save(path, System.Drawing.Imaging.ImageFormat.Png);
        }
    }
}
'@ -ReferencedAssemblies System.Drawing
[WinCap6]::Capture($win, (Join-Path $env:TEMP 'wb-ctrl-initial.png'))

# 点击「启动项目」
$startBtn = [WbWin]::FindChild($win, 'BUTTON', '启动项目')
if ($startBtn -eq [IntPtr]::Zero) { Write-Host 'FAIL: 未找到「启动项目」按钮'; $proc.Kill(); exit 1 }
[WbWin]::Click($startBtn)
Write-Host 'OK: 已点击「启动项目」，等待服务就绪…'

$sw = [System.Diagnostics.Stopwatch]::StartNew()
$healthy = Wait-Healthy 150
Write-Host ('健康检查: ' + $healthy + '（用时 ' + [int]$sw.Elapsed.TotalSeconds + ' 秒）')
if (-not $healthy) { Write-Host 'FAIL: 服务未就绪'; $proc.Kill(); exit 1 }

Start-Sleep -Seconds 3
[WinCap6]::Capture($win, (Join-Path $env:TEMP 'wb-ctrl-running.png'))
$stateText = [WbWin]::FindChild($win, 'STATIC', '运行中')
Write-Host ('状态文本「运行中」: ' + ($stateText -ne [IntPtr]::Zero))

# 「打开面板」按钮存在
$openBtn = [WbWin]::FindChild($win, 'BUTTON', '打开面板')
Write-Host ('「打开面板」按钮存在: ' + ($openBtn -ne [IntPtr]::Zero))
$browser = Get-Process -Name msedge,chrome,firefox -ErrorAction SilentlyContinue
Write-Host ('浏览器进程存在（面板自动打开）: ' + ($null -ne $browser -and @($browser).Count -gt 0))

# 修改端口并保存 → 校验 .env
# 端口框按控件序号定位（EDIT#1 = 端口，创建顺序稳定），避免跨进程读大文本
Write-Host '[diag] 定位端口框…'
$editItems = @([WbWin]::Children($win) | Where-Object { $_[1] -like '*EDIT*' })
if ($editItems.Count -lt 2) { Write-Host ('FAIL: 输入框不足（' + $editItems.Count + '）'); $proc.Kill(); exit 1 }
$portBox = [IntPtr]$editItems[1][0]
Write-Host ('[diag] 端口框 hwnd=' + $portBox)
[WbWin]::SetText($portBox, '17865')
$saveBtn = [WbWin]::FindChild($win, 'BUTTON', '保存配置')
if ($saveBtn -eq [IntPtr]::Zero) { Write-Host 'FAIL: 未找到保存按钮'; $proc.Kill(); exit 1 }
[WbWin]::Click($saveBtn)

function Wait-DialogAndConfirm($seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 400
        $dialog = [WbTop]::FindDialog($proc.Id)
        if ($dialog -ne [IntPtr]::Zero) {
            $okBtn = [WbWin]::FindChild($dialog, 'BUTTON', '确定')
            if ($okBtn -ne [IntPtr]::Zero) { [WbWin]::Click($okBtn); return $true }
        }
    }
    return $false
}

$tipSeen = Wait-DialogAndConfirm 10
Write-Host ('保存提示框已处理: ' + $tipSeen)
Start-Sleep -Seconds 1

$portLine = (Get-Content $envFile) | Where-Object { $_ -match '^WB_MANAGER_PORT=' }
Write-Host ('.env 端口行: ' + $portLine)
if ($portLine -match '17865') { Write-Host 'OK: 端口修改已保存到 .env' } else { Write-Host 'FAIL: 端口修改未生效' }

# 还原端口
[WbWin]::SetText($portBox, [string]$testPort)
[WbWin]::Click($saveBtn)
$tipSeen = Wait-DialogAndConfirm 10
Start-Sleep -Seconds 1

$portLine = (Get-Content $envFile) | Where-Object { $_ -match '^WB_MANAGER_PORT=' }
Write-Host ('.env 端口行: ' + $portLine)
if ($portLine -match '17865') { Write-Host 'OK: 端口修改已保存到 .env' } else { Write-Host 'FAIL: 端口修改未生效' }

# 还原端口
[WbWin]::SetText($portBox, [string]$testPort)
[WbWin]::Click($saveBtn)
$tipDeadline = (Get-Date).AddSeconds(10)
while ((Get-Date) -lt $tipDeadline) {
    Start-Sleep -Milliseconds 400
    $dialog = (Get-Process -Id $proc.Id).MainWindowHandle
    $okBtn = [WbWin]::FindChild($dialog, 'BUTTON', '确定')
    if ($okBtn -ne [IntPtr]::Zero) { [WbWin]::Click($okBtn); break }
}
Start-Sleep -Seconds 1

# 点击「关闭项目」→ 等端口释放
$stopBtn = [WbWin]::FindChild($win, 'BUTTON', '关闭项目')
if ($stopBtn -eq [IntPtr]::Zero) { Write-Host 'FAIL: 未找到「关闭项目」按钮'; $proc.Kill(); exit 1 }
[WbWin]::Click($stopBtn)
Write-Host 'OK: 已点击「关闭项目」，等待端口释放…'
$stopped = Wait-Gone
Write-Host ('服务已停止: ' + $stopped)
if (-not $stopped) { Write-Host 'FAIL: 服务未停止' }

# 截图关闭后的状态（应回到「未运行」）
Start-Sleep -Seconds 3
[WinCap6]::Capture($win, (Join-Path $env:TEMP 'wb-ctrl-stopped.png'))

# 收尾
if (-not $proc.HasExited) { $proc.Kill() }
& (Join-Path $installDir 'windows\service-tools.ps1') stop 2>&1 | Out-Null
Write-Host 'DONE'
