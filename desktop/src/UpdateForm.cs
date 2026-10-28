using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace WorkBuddyDesktop;

internal sealed class UpdateForm : Form
{
    private readonly ServiceController _service;
    private readonly Updater _updater;
    private readonly Action _onUpdated;
    private readonly bool _canInstall;
    private readonly CoreWebView2Environment? _environment;
    private readonly WebView2 _web = new();
    private readonly CancellationTokenSource _cts = new();

    private ReleaseInfo? _release;
    private bool _webReady;
    private bool _busy;
    private bool _autoCheck;
    private bool _checkedOnce;
    private bool _updated;
    private string _lastJson = "";
    private object _state = new { type = "state", phase = "idle", current = "", latest = "", message = "" };

    public UpdateForm(ServiceController service, Updater updater, ReleaseInfo? release,
        bool canInstall, Action onUpdated, CoreWebView2Environment? environment)
    {
        _service = service;
        _updater = updater;
        _release = release;
        _canInstall = canInstall;
        _onUpdated = onUpdated;
        _environment = environment;

        Text = "检查更新";
        StartPosition = FormStartPosition.CenterParent;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        MinimizeBox = false;
        ShowInTaskbar = false;
        var scale = DpiUtil.Scale;
        ClientSize = new Size((int)(640 * scale), (int)(560 * scale));
        BackColor = ThemeInfo.WindowBackground;
        try
        {
            Icon = System.Drawing.Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        }
        catch
        {
        }

        _web.Dock = DockStyle.Fill;
        _web.BackColor = BackColor;
        Controls.Add(_web);

        Shown += async (_, _) => await InitializeAsync();
        FormClosing += OnFormClosing;
    }

    public void StartCheck()
    {
        _autoCheck = true;
        if (_webReady) _ = RunCheckAsync();
    }

    private async Task InitializeAsync()
    {
        try
        {
            // 必须与主窗口共用同一个 WebView2 环境：两个环境（不同数据目录）在同一个
            // 进程里各自映射 app.local 时，第二个环境的虚拟域名会报 ERR_ACCESS_DENIED。
            var environment = _environment
                ?? await CoreWebView2Environment.CreateAsync(null, AppPaths.WebViewProfile);
            await _web.EnsureCoreWebView2Async(environment);
            var core = _web.CoreWebView2;
            core.Settings.AreDevToolsEnabled = false;
            core.Settings.IsStatusBarEnabled = false;
            core.Settings.AreDefaultContextMenusEnabled = false;
            core.WebMessageReceived += OnWebMessage;
            core.NewWindowRequested += (_, args) =>
            {
                args.Handled = true;
                try
                {
                    System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
                    {
                        FileName = args.Uri,
                        UseShellExecute = true,
                    });
                }
                catch
                {
                }
            };
            // 更新页完全自包含（内联 CSS/JS），直接注入文档，避免第二个 WebView2
            // 控件在虚拟域名映射上拿到 ERR_ACCESS_DENIED。
            var htmlPath = Path.Combine(WebUi.EnsureAssets(), "update.html");
            core.NavigateToString(File.ReadAllText(htmlPath));
            _webReady = true;
        }
        catch (Exception ex)
        {
            AppLog.Error("更新窗口初始化失败", ex);
            MessageBox.Show(this, "更新窗口初始化失败：\n" + ex.Message,
                "WorkBuddy Manager", MessageBoxButtons.OK, MessageBoxIcon.Error);
            Close();
        }
    }

    private void OnWebMessage(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        string? type;
        bool allowUnverified = false;
        try
        {
            using var doc = JsonDocument.Parse(e.WebMessageAsJson);
            type = doc.RootElement.TryGetProperty("type", out var t) ? t.GetString() : null;
            allowUnverified = doc.RootElement.TryGetProperty("allowUnverified", out var a)
                              && a.ValueKind == JsonValueKind.True;
        }
        catch
        {
            return;
        }

        AppLog.Info("更新窗口消息：" + type);
        switch (type)
        {
            case "ready":
                PostState();
                if (_autoCheck && !_checkedOnce) _ = RunCheckAsync();
                break;
            case "check":
                _ = RunCheckAsync();
                break;
            case "install":
                _ = RunInstallAsync(allowUnverified);
                break;
            case "rollback":
                _ = RunRollbackAsync();
                break;
            case "releases":
                if (_release is not null)
                {
                    try
                    {
                        System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
                        {
                            FileName = _release.HtmlUrl,
                            UseShellExecute = true,
                        });
                    }
                    catch (Exception ex)
                    {
                        AppLog.Error(ex);
                    }
                }
                break;
            case "close":
                if (!_busy) Close();
                break;
        }
    }

    private async Task RunCheckAsync()
    {
        if (_busy) return;
        _busy = true;
        _autoCheck = false;
        try
        {
            SetState(new
            {
                type = "state",
                phase = "checking",
                current = _updater.CurrentVersion,
                message = "正在查询 GitHub 最新版本…",
            });

            var release = await _updater.CheckAsync(_cts.Token);
            _release = release;
            _checkedOnce = true;

            if (!Updater.IsNewer(release, _updater.CurrentVersion))
            {
                SetState(new
                {
                    type = "state",
                    phase = "uptodate",
                    current = _updater.CurrentVersion,
                    latest = release.Tag,
                    message = $"当前已是最新版本（{_updater.CurrentVersion}）。",
                });
                return;
            }

            SetState(new
            {
                type = "state",
                phase = "available",
                current = _updater.CurrentVersion,
                latest = release.Tag,
                notes = release.Notes,
                sha = release.Sha256 ?? "",
                canInstall = _canInstall,
                message = _canInstall
                    ? $"发现新版本 {release.Tag}。"
                    : $"发现新版本 {release.Tag}（当前连接的是另一个目录的实例，只能查看，不能在这里安装）。",
            });
        }
        catch (Exception ex)
        {
            AppLog.Info("检查更新失败：" + ex.Message);
            SetState(new
            {
                type = "state",
                phase = "error",
                current = _updater.CurrentVersion,
                message = "检查更新失败：" + ex.Message,
            });
        }
        finally
        {
            _busy = false;
        }
    }

    private async Task RunInstallAsync(bool allowUnverified)
    {
        AppLog.Info($"安装更新请求：canInstall={_canInstall} busy={_busy} release={_release?.Tag ?? "(null)"}");
        if (_busy) return;
        if (!_canInstall)
        {
            SetState(new
            {
                type = "state",
                phase = "error",
                current = _updater.CurrentVersion,
                message = "当前连接的是另一个目录的实例，不能在这里安装更新。\n" +
                          "请先停止那个实例，再用本目录的软件启动服务后再更新。",
                canInstall = false,
            });
            return;
        }
        if (_release is null)
        {
            await RunCheckAsync();
            if (_release is null || !Updater.IsNewer(_release, _updater.CurrentVersion)) return;
        }

        if (!Updater.TryBegin())
        {
            SetState(new
            {
                type = "state",
                phase = "error",
                current = _updater.CurrentVersion,
                message = "已有更新任务正在进行，请稍后再试。",
            });
            return;
        }

        _busy = true;
        try
        {
            var progress = new Progress<UpdateProgress>(p =>
            {
                SetState(new
                {
                    type = "state",
                    phase = p.Phase.ToString().ToLowerInvariant(),
                    current = _updater.CurrentVersion,
                    latest = _release?.Tag ?? "",
                    message = p.Message,
                    percent = Math.Round(p.Percent, 1),
                    canRollback = _updater.CanRollback,
                });
            });

            await _updater.UpdateAsync(_release!, allowUnverified, progress, _cts.Token);
            _updated = true;
            SetState(new
            {
                type = "state",
                phase = "done",
                current = _updater.CurrentVersion,
                latest = _release?.Tag ?? "",
                message = $"已更新到 {_release?.Tag}，服务已重新启动。",
                percent = 100,
            });
            _onUpdated();
        }
        catch (OperationCanceledException)
        {
            SetState(new { type = "state", phase = "error", message = "更新已取消。" });
        }
        catch (UpdateException ex)
        {
            AppLog.Error("更新失败", ex);
            SetState(new
            {
                type = "state",
                phase = "error",
                current = _updater.CurrentVersion,
                message = ex.Message,
                canRollback = ex.CanRollback && _updater.CanRollback,
            });
        }
        catch (Exception ex)
        {
            AppLog.Error("更新失败", ex);
            SetState(new
            {
                type = "state",
                phase = "error",
                current = _updater.CurrentVersion,
                message = "更新失败：" + ex.Message,
                canRollback = _updater.CanRollback,
            });
        }
        finally
        {
            _busy = false;
            Updater.End();
        }
    }

    private async Task RunRollbackAsync()
    {
        if (_busy) return;
        if (!Updater.TryBegin())
        {
            SetState(new
            {
                type = "state",
                phase = "error",
                current = _updater.CurrentVersion,
                message = "已有更新任务正在进行，请稍后再试。",
            });
            return;
        }
        _busy = true;
        try
        {
            var progress = new Progress<UpdateProgress>(p => SetState(new
            {
                type = "state",
                phase = p.Phase.ToString().ToLowerInvariant(),
                current = _updater.CurrentVersion,
                message = p.Message,
                percent = Math.Round(p.Percent, 1),
                canRollback = _updater.CanRollback,
            }));

            await _updater.RollbackAsync(progress, _cts.Token);
            _updated = true;
            SetState(new
            {
                type = "state",
                phase = "done",
                current = _updater.CurrentVersion,
                message = "已回滚到更新前的版本，服务已重新启动。",
                percent = 100,
            });
            _onUpdated();
        }
        catch (Exception ex)
        {
            AppLog.Error("回滚失败", ex);
            SetState(new
            {
                type = "state",
                phase = "error",
                current = _updater.CurrentVersion,
                message = "回滚失败：" + ex.Message,
                canRollback = _updater.CanRollback,
            });
        }
        finally
        {
            _busy = false;
            Updater.End();
        }
    }

    private void SetState(object state)
    {
        _state = state;
        if (!IsHandleCreated) return;
        try
        {
            BeginInvoke(() => PostState());
        }
        catch
        {
        }
    }

    private void PostState()
    {
        if (!_webReady) return;
        var json = JsonSerializer.Serialize(_state);
        if (json == _lastJson) return;
        _lastJson = json;
        WebUi.Post(_web, _state);
    }

    private void OnFormClosing(object? sender, FormClosingEventArgs e)
    {
        if (_busy)
        {
            e.Cancel = true;
            MessageBox.Show(this, "正在更新中，请等当前步骤完成后再关闭。",
                "WorkBuddy Manager", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }

        if (!_updated && e.CloseReason == CloseReason.UserClosing)
        {
            // 「稍后」：不打扰，直接关闭
        }
        _cts.Cancel();
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            _cts.Cancel();
            _cts.Dispose();
            _web.Dispose();
        }
        base.Dispose(disposing);
    }
}
