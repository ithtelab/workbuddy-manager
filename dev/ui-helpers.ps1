# WorkBuddy 测试公共 Win32 助手（dot-source 引用）：
#   . (Join-Path $PSScriptRoot 'ui-helpers.ps1')
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
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern IntPtr SendMessage(IntPtr hWnd, uint msg, IntPtr wParam, string lParam);
    [DllImport("user32.dll")]
    public static extern IntPtr SendMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")]
    public static extern IntPtr PostMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr h);

    public const uint WM_SETTEXT = 0x000C;
    public const uint WM_GETTEXT = 0x000D;
    public const uint WM_CLOSE = 0x0010;
    public const uint BM_CLICK = 0x00F5;
    public const uint SMTO_ABORTIFHUNG = 0x0002;

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint msg, IntPtr wParam, StringBuilder lParam, uint flags, uint timeout, out IntPtr result);

    /// 带超时的文本读取：目标窗口的 UI 线程挂起时 400ms 放弃（GetWindowText 无超时保护，可能挂死测试）。
    public static string SafeText(IntPtr hwnd)
    {
        var sb = new StringBuilder(256);
        IntPtr result;
        SendMessageTimeout(hwnd, WM_GETTEXT, (IntPtr)256, sb, SMTO_ABORTIFHUNG, 400, out result);
        return sb.ToString();
    }

    public static List<object[]> Children(IntPtr parent)
    {
        var list = new List<object[]>();
        EnumProc cb = delegate(IntPtr hwnd, IntPtr lp)
        {
            var cls = new StringBuilder(256);
            GetClassName(hwnd, cls, 256);
            list.Add(new object[] { hwnd, cls.ToString(), SafeText(hwnd) });
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

    public static IntPtr FindChildByPrefix(IntPtr parent, string classPart, string textPrefix)
    {
        foreach (object[] item in Children(parent))
        {
            IntPtr hwnd = (IntPtr)item[0];
            string cls = (string)item[1];
            string txt = (string)item[2];
            if (cls.IndexOf(classPart, StringComparison.OrdinalIgnoreCase) < 0) { continue; }
            if (textPrefix != null && !txt.StartsWith(textPrefix, StringComparison.Ordinal)) { continue; }
            return hwnd;
        }
        return IntPtr.Zero;
    }

    public static IntPtr FindChildByClassPrefix(IntPtr parent, string classPrefix)
    {
        foreach (object[] item in Children(parent))
        {
            if (((string)item[1]).StartsWith(classPrefix, StringComparison.OrdinalIgnoreCase))
            {
                return (IntPtr)item[0];
            }
        }
        return IntPtr.Zero;
    }

    // 点击用 PostMessage：按钮事件处理器里若弹出模态框（确认/提示），
    // 同步 SendMessage 会一直阻塞到模态框关闭，测试脚本将被卡死。
    public static void Click(IntPtr hwnd) { PostMessage(hwnd, BM_CLICK, IntPtr.Zero, IntPtr.Zero); }
    public static void SetText(IntPtr hwnd, string text) { SendMessage(hwnd, WM_SETTEXT, IntPtr.Zero, text); }
    public static void Close(IntPtr hwnd) { PostMessage(hwnd, WM_CLOSE, IntPtr.Zero, IntPtr.Zero); }

    [DllImport("user32.dll")]
    public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")]
    public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extra);
    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr h);

    public struct RECT { public int Left, Top, Right, Bottom; }

    /// 模拟真实用户点击标题栏关闭按钮（右上角）：PostMessage WM_CLOSE 的 CloseReason
    /// 不是 UserClosing，会绕过「点 X 询问」逻辑，只有真实点击才走完整路径。
    public static void ClickCloseButton(IntPtr hwnd)
    {
        SetForegroundWindow(hwnd);
        System.Threading.Thread.Sleep(200);
        RECT r;
        GetWindowRect(hwnd, out r);
        int x = r.Right - 23;
        int y = r.Top + 16;
        SetCursorPos(x, y);
        System.Threading.Thread.Sleep(120);
        mouse_event(0x02, 0, 0, 0, UIntPtr.Zero);
        System.Threading.Thread.Sleep(60);
        mouse_event(0x04, 0, 0, 0, UIntPtr.Zero);
    }
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
    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr h);

    /// 找某进程的模态系统对话框（MessageBox，类 #32770）。
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

    /// 找某进程除指定窗口外的第一个可见 WinForms 顶层窗口（自绘对话框如关闭选择框）。
    /// 必须过滤隐藏窗口：NotifyIcon 的消息窗口类名同为 WindowsForms10 且无子控件。
    public static IntPtr FindProcessWindow(int pid, IntPtr exclude)
    {
        IntPtr found = IntPtr.Zero;
        EnumProc cb = delegate(IntPtr h, IntPtr lp)
        {
            uint wpid;
            GetWindowThreadProcessId(h, out wpid);
            if (wpid == (uint)pid && h != exclude && IsWindowVisible(h))
            {
                var cls = new StringBuilder(128);
                GetClassName(h, cls, 128);
                if (cls.ToString().StartsWith("WindowsForms10")) { found = h; return false; }
            }
            return true;
        };
        EnumWindows(cb, IntPtr.Zero);
        return found;
    }
}
'@
