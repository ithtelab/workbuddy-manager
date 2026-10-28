using System.Drawing.Drawing2D;
using System.Runtime.InteropServices;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace WorkBuddyDesktop;

/// <summary>
/// 托盘弹窗（仿 CC Switch 的「配置 / 用量」小面板）。
/// </summary>
internal sealed class TrayPopupForm : Form
{
    private readonly WebView2 _web = new();
    private readonly CoreWebView2Environment? _environment;
    private readonly Func<Task<object>> _config;
    private readonly Func<string, Task<object>> _usage;
    private readonly Action<string> _action;

    private bool _webReady;
    private string _range = "30";
    public bool Pinned { get; set; }
    private DateTime _shownAt;

    public TrayPopupForm(CoreWebView2Environment? environment,
        Func<Task<object>> config, Func<string, Task<object>> usage, Action<string> action,
        bool pinned = false)
    {
        Pinned = pinned;
        _environment = environment;
        _config = config;
        _usage = usage;
        _action = action;

        Text = "WorkBuddyTray";
        FormBorderStyle = FormBorderStyle.None;
        ShowInTaskbar = false;
        TopMost = true;
        StartPosition = FormStartPosition.Manual;
        var scale = DpiUtil.Scale;
        ClientSize = new Size((int)(360 * scale), (int)(620 * scale));
        BackColor = ThemeInfo.WindowBackground;
        DoubleBuffered = true;

        _web.Dock = DockStyle.Fill;
        _web.BackColor = BackColor;
        Controls.Add(_web);

        Shown += async (_, _) => await EnsureWebAsync();
        Deactivate += (_, _) => OnDeactivateWindow();
        Resize += (_, _) => ApplyRoundedRegion();
    }

    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool attach);

    [DllImport("kernel32.dll")]
    private static extern uint GetCurrentThreadId();

    private void ApplyContentHeight(int cssHeight)
    {
        if (cssHeight <= 0) return;
        var scale = DpiUtil.Scale;
        var area = (Screen.FromHandle(Handle) ?? Screen.PrimaryScreen)?.WorkingArea
                   ?? new Rectangle(0, 0, 1280, 800);
        var targetHeight = Math.Min((int)(cssHeight * scale), area.Height - 40);
        if (targetHeight < 240) targetHeight = 240;
        if (ClientSize.Height == targetHeight) return;
        ClientSize = new Size(ClientSize.Width, targetHeight);
        Location = new Point(Location.X, area.Bottom - targetHeight - 12);
        ApplyRoundedRegion();
    }

    private void OnDeactivateWindow()
    {
        if (Pinned) return;
        try
        {
            var foreground = GetForegroundWindow();
            if (foreground != IntPtr.Zero)
            {
                GetWindowThreadProcessId(foreground, out var pid);
                // 焦点切到本软件自己的窗口（主窗口/更新窗口）时不关闭
                if (pid == (uint)Environment.ProcessId) return;
            }
        }
        catch
        {
        }
        AppLog.Info("托盘弹窗 Deactivate");
        if ((DateTime.Now - _shownAt).TotalMilliseconds > 800) Hide();
    }

    private void ForceForeground()
    {
        try
        {
            var foreground = GetForegroundWindow();
            GetWindowThreadProcessId(foreground, out _);
            var foregroundThread = GetWindowThreadProcessId(foreground, out _);
            var currentThread = GetCurrentThreadId();
            if (foregroundThread != currentThread)
            {
                AttachThreadInput(currentThread, foregroundThread, true);
            }
            SetForegroundWindow(Handle);
            if (foregroundThread != currentThread)
            {
                AttachThreadInput(currentThread, foregroundThread, false);
            }
        }
        catch
        {
        }
    }

    public void ShowNearTray()
    {
        AppLog.Info($"托盘弹窗请求显示 visible={Visible} bounds={Bounds}");
        if (Visible)
        {
            Hide();
            return;
        }

        var area = Screen.PrimaryScreen?.WorkingArea ?? new Rectangle(0, 0, 1280, 800);
        Bounds = new Rectangle(area.Right - Width - 12, area.Bottom - Height - 12, Width, Height);
        _shownAt = DateTime.Now;
        ApplyRoundedRegion();
        Show();
        Activate();
        ForceForeground();
        AppLog.Info($"托盘弹窗已显示 Bounds={Bounds} Visible={Visible} Handle={Handle}");
        if (_webReady) _ = RefreshAsync();
    }

    public void Refresh()
    {
        if (Visible && _webReady) _ = RefreshAsync();
    }

    private void ApplyRoundedRegion()
    {
        try
        {
            using var path = new GraphicsPath();
            var r = ClientRectangle;
            const int radius = 14;
            var d = radius * 2;
            path.AddArc(r.X, r.Y, d, d, 180, 90);
            path.AddArc(r.Right - d, r.Y, d, d, 270, 90);
            path.AddArc(r.Right - d, r.Bottom - d, d, d, 0, 90);
            path.AddArc(r.X, r.Bottom - d, d, d, 90, 90);
            path.CloseFigure();
            Region = new Region(path);
        }
        catch
        {
        }
    }

    private async Task EnsureWebAsync()
    {
        if (_webReady) return;
        try
        {
            AppLog.Info("托盘弹窗初始化开始");
            var environment = _environment
                ?? await CoreWebView2Environment.CreateAsync(null, AppPaths.WebViewProfile);
            await _web.EnsureCoreWebView2Async(environment);
            AppLog.Info("托盘弹窗 WebView 就绪");
            var core = _web.CoreWebView2;
            core.Settings.AreDevToolsEnabled = false;
            core.Settings.IsStatusBarEnabled = false;
            core.Settings.AreDefaultContextMenusEnabled = false;
            core.WebMessageReceived += OnMessage;
            var html = File.ReadAllText(Path.Combine(WebUi.EnsureAssets(), "tray.html"));
            core.NavigateToString(html);
            AppLog.Info($"托盘弹窗页面已注入 {html.Length} 字节");
            _webReady = true;
        }
        catch (Exception ex)
        {
            AppLog.Error("托盘弹窗初始化失败", ex);
        }
    }

    private void OnMessage(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        string? type = null, name = null, value = null;
        var height = 0;
        try
        {
            using var doc = JsonDocument.Parse(e.WebMessageAsJson);
            if (doc.RootElement.TryGetProperty("type", out var t)) type = t.GetString();
            if (doc.RootElement.TryGetProperty("name", out var n)) name = n.GetString();
            if (doc.RootElement.TryGetProperty("value", out var v)) value = v.GetString();
            if (doc.RootElement.TryGetProperty("height", out var h) && h.ValueKind == JsonValueKind.Number)
            {
                height = h.GetInt32();
            }
        }
        catch
        {
            return;
        }

        switch (type)
        {
            case "ready":
                _ = RefreshAsync();
                break;
            case "range":
                _range = string.IsNullOrWhiteSpace(value) ? "30" : value!;
                _ = RefreshUsageAsync();
                break;
            case "refresh":
                _ = RefreshAsync();
                break;
            case "resize":
                ApplyContentHeight(height);
                break;
            case "action":
                if (string.IsNullOrEmpty(name)) return;
                if (name == "close")
                {
                    Hide();
                    return;
                }
                _action(name);
                break;
        }
    }

    private async Task RefreshAsync()
    {
        if (!_webReady) return;
        try
        {
            var config = await _config();
            WebUi.Post(_web, config);
        }
        catch (Exception ex)
        {
            AppLog.Error("读取托盘状态失败", ex);
        }
        await RefreshUsageAsync();
    }

    private async Task RefreshUsageAsync()
    {
        if (!_webReady) return;
        try
        {
            var usage = await _usage(_range);
            WebUi.Post(_web, usage);
        }
        catch (Exception ex)
        {
            WebUi.Post(_web, new { type = "usage", error = ex.Message });
        }
    }
}
