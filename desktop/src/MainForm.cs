using System.Net;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace WorkBuddyDesktop;

internal sealed class MainForm : Form
{
    private readonly string _root;
    private readonly AppSettings _settings;
    private readonly EventWaitHandle _showEvent;
    private readonly EnvFile _env;
    private readonly ServiceController _service;
    private readonly Updater _updater;
    private readonly CredentialStore _credentials;
    private readonly HttpClient _localApi;
    private readonly CancellationTokenSource _cts = new();
    private readonly bool _autoUpdate;
    private readonly bool _showUpdate;
    private readonly bool _showTrayPopup;

    private readonly WebView2 _web = new();
    private readonly NotifyIcon _tray = new();
    private readonly ToolStripMenuItem _miShow = new("打开面板");
    private readonly ToolStripMenuItem _miRestart = new("重启服务");
    private readonly ToolStripMenuItem _miUpdate = new("检查更新…");
    private readonly ToolStripMenuItem _miFolder = new("打开项目文件夹");
    private readonly ToolStripMenuItem _miExit = new("退出");
    private readonly System.Windows.Forms.Timer _updateTimer = new();

    private ServiceSnapshot _snapshot = new() { State = ServiceState.Starting, Message = "正在启动…" };
    private string _splashMessage = "正在准备…";
    private string _currentPage = "splash";
    private string _lastStateJson = "";
    private bool _webReady;
    private CoreWebView2Environment? _webEnvironment;
    private bool _reallyExit;
    private bool _updating;
    private bool _updateBannerDismissed;
    private ReleaseInfo? _pendingRelease;
    private UpdateForm? _updateForm;
    private TrayPopupForm? _trayPopup;
    private string? _sessionToken;
    private bool _rememberHintShown;
    private RegisteredWaitHandle? _showWait;

    public int ExitCode { get; private set; }

    public MainForm(string root, AppSettings settings, EventWaitHandle showEvent,
        bool autoUpdate = false, bool showUpdate = false, bool showTrayPopup = false)
    {
        _root = root;
        _settings = settings;
        _showEvent = showEvent;
        _autoUpdate = autoUpdate;
        _showUpdate = showUpdate;
        _showTrayPopup = showTrayPopup;
        _env = new EnvFile(Path.Combine(root, ".env"));
        if (_env.NormalizePathsToRoot(root))
        {
            _env.Save();
            AppLog.Info("已校正 .env 中指向旧目录的路径");
        }
        _service = new ServiceController(root, _env);
        _updater = new Updater(root, _service);
        _credentials = new CredentialStore(settings);
        _localApi = new HttpClient(new SocketsHttpHandler { UseProxy = false })
        {
            BaseAddress = new Uri(_service.BaseUrl),
            Timeout = TimeSpan.FromSeconds(15),
        };

        BuildWindow();
        BuildTray();
        BuildTimer();
    }

    // ── 窗口 ────────────────────────────────────────────────

    private void BuildWindow()
    {
        Text = "WorkBuddy Manager";
        BackColor = ThemeInfo.WindowBackground;
        MinimumSize = new Size(900, 620);
        StartPosition = FormStartPosition.Manual;
        try
        {
            Icon = System.Drawing.Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        }
        catch
        {
        }

        var bounds = new Rectangle(_settings.WindowX, _settings.WindowY,
            _settings.WindowWidth, _settings.WindowHeight);
        if (_settings.WindowX < 0 || _settings.WindowY < 0 || !IsOnAnyScreen(bounds))
        {
            Size = new Size(1280, 840);
            StartPosition = FormStartPosition.CenterScreen;
            WindowState = FormWindowState.Maximized;
        }
        else
        {
            Bounds = bounds;
            if (_settings.WindowMaximized) WindowState = FormWindowState.Maximized;
        }

        _web.Dock = DockStyle.Fill;
        _web.BackColor = BackColor;
        Controls.Add(_web);

        KeyPreview = true;
        KeyDown += (_, e) =>
        {
            if (e.Control && e.KeyCode == Keys.U)
            {
                OpenUpdateForm();
                e.Handled = true;
                e.SuppressKeyPress = true;
            }
        };

        Shown += async (_, _) =>
        {
            await InitializeWebViewAsync();
            await StartServiceAsync();
        };

        FormClosing += OnFormClosing;
        Resize += (_, _) => SaveBounds();
        Move += (_, _) => SaveBounds();

        _showWait = ThreadPool.RegisterWaitForSingleObject(_showEvent,
            (_, _) => BeginInvoke(ShowFromTray), null, Timeout.Infinite, executeOnlyOnce: false);
    }

    private static bool IsOnAnyScreen(Rectangle bounds)
    {
        return Screen.AllScreens.Any(screen =>
            screen.WorkingArea.IntersectsWith(bounds) &&
            bounds.Width >= 400 && bounds.Height >= 300);
    }

    private void SaveBounds()
    {
        if (WindowState == FormWindowState.Minimized) return;
        _settings.WindowMaximized = WindowState == FormWindowState.Maximized;
        if (WindowState == FormWindowState.Normal)
        {
            _settings.WindowX = Bounds.X;
            _settings.WindowY = Bounds.Y;
            _settings.WindowWidth = Bounds.Width;
            _settings.WindowHeight = Bounds.Height;
        }
    }

    // ── 托盘 ────────────────────────────────────────────────

    private void BuildTray()
    {
        var menu = new ContextMenuStrip();
        _miShow.Font = new Font(_miShow.Font, FontStyle.Bold);
        _miShow.Click += (_, _) => ShowFromTray();
        _miRestart.Click += (_, _) => _ = RestartServiceAsync();
        _miUpdate.Click += (_, _) => OpenUpdateForm();
        _miFolder.Click += (_, _) => ProcessUtil.OpenFolder(_root);
        _miExit.Click += (_, _) => RequestExit();

        menu.Items.Add(_miShow);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(_miRestart);
        menu.Items.Add(_miUpdate);
        menu.Items.Add(_miFolder);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(_miExit);

        _tray.Text = "WorkBuddy Manager";
        _tray.Visible = true;
        try
        {
            _tray.Icon = Icon;
        }
        catch
        {
            _tray.Icon = SystemIcons.Application;
        }
        _tray.MouseUp += (_, e) =>
        {
            if (e.Button is MouseButtons.Left or MouseButtons.Right) ShowTrayPopup();
        };
        _tray.DoubleClick += (_, _) => ShowFromTray();
        _tray.BalloonTipClicked += (_, _) =>
        {
            if (_pendingRelease is not null) OpenUpdateForm();
            else ShowFromTray();
        };
    }

    private void BuildTimer()
    {
        _updateTimer.Interval = (int)TimeSpan.FromHours(6).TotalMilliseconds;
        _updateTimer.Tick += (_, _) => _ = CheckForUpdatesAsync(silent: true);
        _updateTimer.Start();
    }

    private void ShowFromTray()
    {
        Show();
        if (WindowState == FormWindowState.Minimized)
        {
            WindowState = _settings.WindowMaximized ? FormWindowState.Maximized : FormWindowState.Normal;
        }
        Activate();
        BringToFront();
    }

    private void SetTrayEnabled(bool restart, bool update)
    {
        _miRestart.Enabled = restart;
        _miUpdate.Enabled = update;
    }

    // ── WebView ─────────────────────────────────────────────

    private async Task InitializeWebViewAsync()
    {
        try
        {
            var version = CoreWebView2Environment.GetAvailableBrowserVersionString();
            if (string.IsNullOrEmpty(version))
            {
                MessageBox.Show(this,
                    "未检测到 Microsoft Edge WebView2 运行时。\n请先安装 WebView2 Runtime 后再启动。",
                    "WorkBuddy Manager", MessageBoxButtons.OK, MessageBoxIcon.Error);
                RequestExit();
                return;
            }

            var environment = await CoreWebView2Environment.CreateAsync(null, AppPaths.WebViewProfile);
            _webEnvironment = environment;
            await _web.EnsureCoreWebView2Async(environment);

            var core = _web.CoreWebView2;
            core.Settings.AreDevToolsEnabled = false;
            core.Settings.IsStatusBarEnabled = false;
            core.Settings.AreDefaultContextMenusEnabled = true;
            core.Settings.IsZoomControlEnabled = true;
            core.WebMessageReceived += OnWebMessage;
            core.NavigationCompleted += OnNavigationCompleted;
            core.NewWindowRequested += (_, args) =>
            {
                args.Handled = true;
                ProcessUtil.OpenUrl(args.Uri);
            };
            core.PermissionRequested += (_, args) =>
            {
                args.State = args.PermissionKind == CoreWebView2PermissionKind.ClipboardRead
                    ? CoreWebView2PermissionState.Allow
                    : CoreWebView2PermissionState.Deny;
            };
            core.DownloadStarting += (_, args) =>
            {
                try
                {
                    var downloads = Path.Combine(
                        Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "Downloads");
                    Directory.CreateDirectory(downloads);
                    var name = string.IsNullOrWhiteSpace(args.ResultFilePath)
                        ? "workbuddy-download"
                        : Path.GetFileName(args.ResultFilePath);
                    var target = UniqueDownloadPath(downloads, name);
                    args.ResultFilePath = target;
                    _tray.ShowBalloonTip(3000, "开始下载", target, ToolTipIcon.Info);
                }
                catch (Exception ex)
                {
                    AppLog.Error("设置下载路径失败", ex);
                }
            };
            core.SetVirtualHostNameToFolderMapping("app.local", WebUi.EnsureAssets(),
                CoreWebView2HostResourceAccessKind.Allow);
            core.Navigate("https://app.local/splash.html");
            _currentPage = "splash";
            _webReady = true;
        }
        catch (Exception ex)
        {
            AppLog.Error("WebView2 初始化失败", ex);
            MessageBox.Show(this, "WebView2 初始化失败：\n" + ex.Message,
                "WorkBuddy Manager", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    private static string UniqueDownloadPath(string folder, string fileName)
    {
        var target = Path.Combine(folder, fileName);
        if (!File.Exists(target)) return target;
        var extension = Path.GetExtension(fileName);
        var stem = Path.GetFileNameWithoutExtension(fileName);
        for (var i = 1; i < 1000; i++)
        {
            target = Path.Combine(folder, $"{stem} ({i}){extension}");
            if (!File.Exists(target)) return target;
        }
        return Path.Combine(folder, $"{stem} ({Guid.NewGuid():N}){extension}");
    }

    private void OnNavigationCompleted(object? sender, CoreWebView2NavigationCompletedEventArgs e)
    {
        if (!e.IsSuccess) return;
        _lastStateJson = "";
        PostCurrentState();
        if (_currentPage == "panel") InjectPanelShell();
    }

    private void OnWebMessage(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        var type = WebUi.ExtractMessageType(e.WebMessageAsJson);
        AppLog.Info("主窗口消息：" + type);
        switch (type)
        {
            case "ready":
                PostCurrentState();
                break;
            case "retry":
                _ = StartServiceAsync();
                break;
            case "update":
                OpenUpdateForm();
                break;
            case "installUpdate":
                _ = StartPanelUpdateAsync(false);
                break;
            case "rollbackUpdate":
                _ = RollbackFromPanelAsync();
                break;
            case "dismissUpdate":
                _updateBannerDismissed = true;
                break;
            case "saveCreds":
                SaveCapturedCredentials(e.WebMessageAsJson);
                break;
            case "folder":
                ProcessUtil.OpenFolder(_root);
                break;
            case "quit":
                RequestExit();
                break;
            case "panel":
                NavigateToPanel();
                break;
        }
    }

    private void Navigate(string url)
    {
        if (!_webReady) return;
        try
        {
            _web.CoreWebView2.Navigate(url);
        }
        catch (Exception ex)
        {
            AppLog.Error("导航失败", ex);
        }
    }

    private void NavigateToPanel(bool loggedIn = true)
    {
        if (_snapshot.State is not (ServiceState.Ready or ServiceState.External)) return;
        _currentPage = "panel";
        Navigate(_service.BaseUrl + (loggedIn ? "/dashboard" : "/login"));
    }

    private void ShowSplash(string message)
    {
        _splashMessage = message;
        if (_currentPage != "splash")
        {
            _currentPage = "splash";
            Navigate("https://app.local/splash.html");
        }
        else
        {
            PostCurrentState();
        }
    }

    private void ShowErrorPage(string message)
    {
        _snapshot = new ServiceSnapshot
        {
            State = ServiceState.Error,
            Message = message,
            Port = _service.Port,
        };
        _currentPage = "error";
        Navigate("https://app.local/error.html");
        PostCurrentState();
    }

    private void PostCurrentState()
    {
        if (!_webReady) return;
        object payload = _currentPage switch
        {
            "splash" => new
            {
                type = "state",
                state = _snapshot.State.ToString().ToLowerInvariant(),
                version = _updater.CurrentVersion,
                message = _splashMessage,
                address = _service.BaseUrl,
                external = _snapshot.InstanceRoot,
            },
            "error" => new
            {
                type = "state",
                state = "error",
                version = _updater.CurrentVersion,
                message = _snapshot.Message,
                log = TailOf(Path.Combine(_root, "data", "manager.err.log")) + AppLog.Tail(2000),
                address = _service.BaseUrl,
            },
            _ => new
            {
                type = "state",
                state = "panel",
                version = _updater.CurrentVersion,
                address = _service.BaseUrl,
            },
        };
        var json = JsonSerializer.Serialize(payload);
        if (json == _lastStateJson) return;
        _lastStateJson = json;
        WebUi.Post(_web, payload);
    }

    private static string TailOf(string path)
    {
        try
        {
            if (!File.Exists(path)) return "";
            using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            var start = Math.Max(0, fs.Length - 4000);
            fs.Seek(start, SeekOrigin.Begin);
            using var reader = new StreamReader(fs);
            return reader.ReadToEnd();
        }
        catch
        {
            return "";
        }
    }

    // ── 服务 ────────────────────────────────────────────────

    private async Task StartServiceAsync()
    {
        SetTrayEnabled(false, false);
        ShowSplash("正在检查本地服务…");
        try
        {
            var progress = new Progress<string>(message =>
            {
                _splashMessage = message;
                if (_currentPage == "splash") PostCurrentState();
            });

            var snapshot = await Task.Run(async () =>
                await _service.EnsureStartedAsync(progress, _cts.Token).ConfigureAwait(false));

            _snapshot = snapshot;
            switch (snapshot.State)
            {
                case ServiceState.Ready:
                    Text = "WorkBuddy Manager";
                    SetTrayEnabled(true, true);
                    var loggedIn = await RestoreLoginAsync();
                    NavigateToPanel(loggedIn);
                    if (_autoUpdate) _ = RunAutoUpdateAsync();
                    else _ = CheckForUpdatesAsync(silent: true);
                    if (_showUpdate) OpenUpdateForm();
                    if (_showTrayPopup) ShowTrayPopup(pin: true);
                    break;
                case ServiceState.External:
                    Text = "WorkBuddy Manager — 外部实例（只读）";
                    SetTrayEnabled(false, true);
                    _tray.Text = "WorkBuddy Manager — 外部实例";
                    NavigateToPanel();
                    _tray.ShowBalloonTip(5000, "当前连接的是另一个目录的实例",
                        "为避免误操作，停止/重启/安装更新已禁用。\n" + snapshot.InstanceRoot, ToolTipIcon.Warning);
                    if (_autoUpdate)
                    {
                        AppLog.Error("自动更新中止：当前连接的是外部实例 " + snapshot.InstanceRoot);
                        ExitCode = 2;
                        _reallyExit = true;
                        Close();
                    }
                    else
                    {
                        _ = CheckForUpdatesAsync(silent: true);
                    }
                    if (_showTrayPopup) ShowTrayPopup(pin: true);
                    break;
                default:
                    Text = "WorkBuddy Manager — 启动失败";
                    SetTrayEnabled(false, false);
                    ShowErrorPage(snapshot.Message);
                    break;
            }
        }
        catch (Exception ex)
        {
            AppLog.Error("启动服务失败", ex);
            ShowErrorPage(ex.Message);
        }
    }

    private async Task RestartServiceAsync()
    {
        if (_snapshot.State == ServiceState.External) return;
        SetTrayEnabled(false, false);
        ShowSplash("正在重启服务…");
        try
        {
            var progress = new Progress<string>(message =>
            {
                _splashMessage = message;
                if (_currentPage == "splash") PostCurrentState();
            });
            var snapshot = await Task.Run(async () =>
                await _service.RestartAsync(progress, _cts.Token).ConfigureAwait(false));
            _snapshot = snapshot;
            if (snapshot.State == ServiceState.Ready)
            {
                _splashMessage = "服务已重启";
                Text = "WorkBuddy Manager";
                SetTrayEnabled(true, true);
                NavigateToPanel();
            }
            else
            {
                ShowErrorPage(snapshot.Message);
            }
        }
        catch (Exception ex)
        {
            AppLog.Error("重启服务失败", ex);
            ShowErrorPage(ex.Message);
        }
    }

    public void ReloadPanel()
    {
        _lastStateJson = "";
        if (_pendingRelease is not null && !Updater.IsNewer(_pendingRelease, _updater.CurrentVersion))
        {
            _pendingRelease = null;
            _updateBannerDismissed = false;
            _miUpdate.Text = "检查更新…";
        }
        if (_snapshot.State is ServiceState.Ready or ServiceState.External)
        {
            NavigateToPanel();
        }
        else
        {
            _ = StartServiceAsync();
        }
        _trayPopup?.Refresh();
    }

    // ── 自动登录 ────────────────────────────────────────────

    private async Task<string?> GetWebViewSessionCookieAsync()
    {
        try
        {
            if (!_webReady || _web.CoreWebView2 is null) return null;
            var cookies = await _web.CoreWebView2.CookieManager.GetCookiesAsync(_service.BaseUrl + "/");
            return cookies.FirstOrDefault(c => string.Equals(c.Name, "wb_session", StringComparison.Ordinal))?.Value;
        }
        catch
        {
            return null;
        }
    }

    private void ApplySessionCookie(string token)
    {
        try
        {
            var manager = _web.CoreWebView2!.CookieManager;
            var cookie = manager.CreateCookie("wb_session", token, _service.ConnectHost, "/");
            cookie.IsHttpOnly = true;
            cookie.IsSecure = false;
            cookie.SameSite = CoreWebView2CookieSameSiteKind.Lax;
            cookie.Expires = DateTime.Now.AddDays(1);
            manager.AddOrUpdateCookie(cookie);
            _sessionToken = token;
        }
        catch (Exception ex)
        {
            AppLog.Error("写入登录 Cookie 失败", ex);
        }
    }

    private async Task<bool> IsSessionValidAsync(string token)
    {
        try
        {
            using var request = new HttpRequestMessage(HttpMethod.Get, "/api/me");
            request.Headers.TryAddWithoutValidation("Cookie", "wb_session=" + token);
            using var response = await _localApi.SendAsync(request, _cts.Token);
            return response.IsSuccessStatusCode;
        }
        catch
        {
            return false;
        }
    }

    private async Task<(bool ok, bool unauthorized)> LoginAsync(string user, string password)
    {
        try
        {
            using var request = new HttpRequestMessage(HttpMethod.Post, "/api/login")
            {
                Content = new StringContent(
                    JsonSerializer.Serialize(new { username = user, password = password }),
                    Encoding.UTF8, "application/json"),
            };
            using var response = await _localApi.SendAsync(request, _cts.Token);
            if (response.StatusCode == HttpStatusCode.Unauthorized) return (false, true);
            if (!response.IsSuccessStatusCode)
            {
                AppLog.Info($"自动登录失败：HTTP {(int)response.StatusCode}");
                return (false, false);
            }
            if (response.Headers.TryGetValues("Set-Cookie", out var values))
            {
                foreach (var value in values)
                {
                    var match = Regex.Match(value, @"wb_session=([^;]+)");
                    if (!match.Success) continue;
                    ApplySessionCookie(match.Groups[1].Value);
                    return (true, false);
                }
            }
            return (false, false);
        }
        catch (Exception ex)
        {
            AppLog.Info("自动登录请求失败：" + ex.Message);
            return (false, false);
        }
    }

    private (string User, string Password)? ReadFirstRunLogin()
    {
        try
        {
            var path = Path.Combine(_root, "data", "desktop-firstrun.json");
            if (!File.Exists(path)) return null;
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            var user = doc.RootElement.TryGetProperty("username", out var u) ? u.GetString() : null;
            var pass = doc.RootElement.TryGetProperty("password", out var p) ? p.GetString() : null;
            if (string.IsNullOrWhiteSpace(user) || string.IsNullOrEmpty(pass)) return null;
            return (user!, pass!);
        }
        catch
        {
            return null;
        }
    }

    private void DeleteFirstRunLogin()
    {
        try
        {
            var path = Path.Combine(_root, "data", "desktop-firstrun.json");
            if (File.Exists(path)) File.Delete(path);
        }
        catch
        {
        }
    }

    private async Task<bool> RestoreLoginAsync()
    {
        if (!_webReady) return false;
        try
        {
            var existing = await GetWebViewSessionCookieAsync();
            if (!string.IsNullOrEmpty(existing) && await IsSessionValidAsync(existing))
            {
                _sessionToken = existing;
                return true;
            }

            if (!_settings.AutoLogin) return false;

            var saved = _credentials.Load();
            if (saved is { } creds)
            {
                var (ok, unauthorized) = await LoginAsync(creds.User, creds.Password);
                if (ok) return true;
                if (unauthorized)
                {
                    AppLog.Info("已保存的登录信息已失效，已清除");
                    _credentials.Clear();
                }
            }

            var firstRun = ReadFirstRunLogin();
            if (firstRun is { } first)
            {
                var (ok, _) = await LoginAsync(first.User, first.Password);
                DeleteFirstRunLogin();
                if (ok)
                {
                    _credentials.Save(first.User, first.Password);
                    AppLog.Info("已使用首次安装的默认账号登录");
                    return true;
                }
            }
        }
        catch (Exception ex)
        {
            AppLog.Error("自动登录失败", ex);
        }
        return false;
    }

    private void SaveCapturedCredentials(string json)
    {
        try
        {
            if (!_settings.AutoLogin) return;
            using var doc = JsonDocument.Parse(json);
            var user = doc.RootElement.TryGetProperty("username", out var u) ? u.GetString() : null;
            var pass = doc.RootElement.TryGetProperty("password", out var p) ? p.GetString() : null;
            if (string.IsNullOrWhiteSpace(user) || string.IsNullOrEmpty(pass)) return;
            _credentials.Save(user!, pass!);
            DeleteFirstRunLogin();
            if (!_rememberHintShown)
            {
                _rememberHintShown = true;
                _tray.ShowBalloonTip(3000, "已记住登录信息", "下次启动会自动登录。", ToolTipIcon.Info);
            }
        }
        catch (Exception ex)
        {
            AppLog.Error("保存登录信息失败", ex);
        }
    }

    // ── 托盘弹窗 ────────────────────────────────────────────

    private void ShowTrayPopup(bool pin = false)
    {
        _trayPopup ??= new TrayPopupForm(_webEnvironment, BuildTrayConfigAsync, LoadUsageAsync, OnTrayAction, pin);
        _trayPopup.Pinned = pin;
        _trayPopup.ShowNearTray();
    }

    private async Task<object> BuildTrayConfigAsync()
    {
        var upstream = await ProbeUpstreamAsync();
        return new
        {
            type = "config",
            version = _updater.CurrentVersion,
            state = _snapshot.State.ToString().ToLowerInvariant(),
            host = _service.ConnectHost,
            port = _service.Port,
            upstream,
            autoLogin = _settings.AutoLogin,
            autoStart = AutoStartManager.IsEnabled(),
            pendingUpdate = _pendingRelease?.Tag,
            user = _settings.LoginUser,
            external = _snapshot.State == ServiceState.External,
            root = _root,
        };
    }

    private async Task<bool> ProbeUpstreamAsync()
    {
        try
        {
            var raw = _env.Get("WB2API_BASE") ?? "http://127.0.0.1:7863";
            if (!Uri.TryCreate(raw, UriKind.Absolute, out var uri)) return false;
            return await ServiceController.IsPortOpenAsync(uri.Host, uri.Port, 600);
        }
        catch
        {
            return false;
        }
    }

    private void OnTrayAction(string name)
    {
        switch (name)
        {
            case "open":
                _trayPopup?.Hide();
                ShowFromTray();
                break;
            case "settings":
                _trayPopup?.Hide();
                ShowFromTray();
                Navigate(_service.BaseUrl + "/settings");
                break;
            case "restart":
                _ = RestartServiceAsync();
                break;
            case "update":
                _trayPopup?.Hide();
                OpenUpdateForm();
                break;
            case "folder":
                ProcessUtil.OpenFolder(_root);
                break;
            case "toggleAutoLogin":
                _settings.AutoLogin = !_settings.AutoLogin;
                _settings.Save();
                _trayPopup?.Refresh();
                break;
            case "toggleAutoStart":
                _settings.AutoStart = !AutoStartManager.IsEnabled();
                AutoStartManager.Set(_settings.AutoStart, Application.ExecutablePath);
                _settings.Save();
                _trayPopup?.Refresh();
                break;
            case "quit":
                _trayPopup?.Hide();
                RequestExit();
                break;
            case "refresh":
                _trayPopup?.Refresh();
                break;
        }
    }

    private async Task<object> LoadUsageAsync(string range)
    {
        try
        {
            var cookie = _sessionToken ?? await GetWebViewSessionCookieAsync();
            if (string.IsNullOrEmpty(cookie))
            {
                return new { type = "usage", error = "未登录", range };
            }

            var days = range switch
            {
                "today" => 1,
                "7" => 7,
                "30" => 30,
                _ => 30,
            };

            using var summary = await GetJsonAsync("/api/stats/summary", cookie);
            using var daily = await GetJsonAsync($"/api/stats/daily?days={days}", cookie);

            double cache = -1;
            try
            {
                using var upstream = await GetJsonAsync("/api/stats/upstream", cookie);
                var root = upstream.RootElement;
                if (root.TryGetProperty("available", out var available) && available.ValueKind == JsonValueKind.True
                    && root.TryGetProperty("total", out var total)
                    && total.TryGetProperty("cache_hit_rate", out var rate)
                    && rate.ValueKind == JsonValueKind.Number)
                {
                    cache = rate.GetDouble();
                }
            }
            catch
            {
                // 上游统计拿不到时不影响其它卡片
            }

            long requests = 0, tokens = 0, failed = 0;
            double credit = 0;
            var points = new List<object>();
            foreach (var row in daily.RootElement.EnumerateArray())
            {
                var r = row.TryGetProperty("requests", out var rv) ? rv.GetInt64() : 0;
                var p = row.TryGetProperty("prompt_tokens", out var pv) ? pv.GetInt64() : 0;
                var c = row.TryGetProperty("completion_tokens", out var cv) ? cv.GetInt64() : 0;
                var cr = row.TryGetProperty("credit", out var crv) ? crv.GetDouble() : 0;
                var f = row.TryGetProperty("failed", out var fv) ? fv.GetInt64() : 0;
                requests += r;
                tokens += p + c;
                credit += cr;
                failed += f;
                points.Add(new
                {
                    day = row.TryGetProperty("day", out var dv) ? dv.GetString() : "",
                    requests = r,
                    credit = cr,
                });
            }

            if (range == "today")
            {
                try
                {
                    using var hourly = await GetJsonAsync("/api/stats/hourly", cookie);
                    points.Clear();
                    foreach (var row in hourly.RootElement.EnumerateArray())
                    {
                        var hour = row.TryGetProperty("hour", out var hv) ? hv.GetInt32() : 0;
                        var r = row.TryGetProperty("requests", out var rv) ? rv.GetInt64() : 0;
                        var cr = row.TryGetProperty("credit", out var cv) ? cv.GetDouble() : 0;
                        points.Add(new
                        {
                            day = hour.ToString("00") + ":00",
                            requests = r,
                            credit = cr,
                        });
                    }
                }
                catch
                {
                    // 拿不到小时数据就退回按天
                }
            }

            if (range == "all")
            {
                var s = summary.RootElement;
                requests = s.TryGetProperty("total_requests", out var tr) ? tr.GetInt64() : requests;
                tokens = s.TryGetProperty("total_tokens", out var tt) ? tt.GetInt64() : tokens;
                credit = s.TryGetProperty("total_credit", out var tc) ? tc.GetDouble() : credit;
            }

            return new { type = "usage", range, requests, tokens, credit, failed, cache, points };
        }
        catch (Exception ex)
        {
            AppLog.Error("读取用量失败", ex);
            return new { type = "usage", error = ex.Message, range };
        }
    }

    private async Task<JsonDocument> GetJsonAsync(string path, string cookie)
    {
        using var request = new HttpRequestMessage(HttpMethod.Get, path);
        request.Headers.TryAddWithoutValidation("Cookie", "wb_session=" + cookie);
        using var response = await _localApi.SendAsync(request, _cts.Token);
        response.EnsureSuccessStatusCode();
        var text = await response.Content.ReadAsStringAsync(_cts.Token);
        return JsonDocument.Parse(text);
    }

    // ── 更新 ────────────────────────────────────────────────

    private async Task RunAutoUpdateAsync()
    {
        try
        {
            AppLog.Info("自动更新：开始检查");
            var release = await _updater.CheckAsync(_cts.Token);
            if (!Updater.IsNewer(release, _updater.CurrentVersion))
            {
                AppLog.Info("自动更新：已是最新版本 " + _updater.CurrentVersion);
                ExitCode = 0;
            }
            else
            {
                AppLog.Info($"自动更新：{_updater.CurrentVersion} -> {release.Tag}");
                var progress = new Progress<UpdateProgress>(p =>
                    AppLog.Info($"[UPDATE] {p.Phase} {p.Percent:F0}% {p.Message}"));
                await _updater.UpdateAsync(release, false, progress, _cts.Token);
                AppLog.Info("自动更新：完成，当前版本 " + _updater.CurrentVersion);
                ExitCode = 0;
            }
        }
        catch (Exception ex)
        {
            AppLog.Error("自动更新失败", ex);
            ExitCode = 1;
        }
        finally
        {
            _reallyExit = true;
            Close();
        }
    }

    private async Task CheckForUpdatesAsync(bool silent)
    {
        try
        {
            var release = await _updater.CheckAsync(_cts.Token);
            if (!Updater.IsNewer(release, _updater.CurrentVersion))
            {
                if (!silent)
                {
                    _miUpdate.Text = "检查更新…";
                    MessageBox.Show(this, $"当前已是最新版本（{_updater.CurrentVersion}）。",
                        "检查更新", MessageBoxButtons.OK, MessageBoxIcon.Information);
                }
                return;
            }

            _pendingRelease = release;
            _updateBannerDismissed = false;
            _miUpdate.Text = $"检查更新（发现 {release.Tag}）";
            _tray.ShowBalloonTip(5000, "发现新版本 " + release.Tag,
                "在面板右下角可以直接更新，更新后会自动重启服务。", ToolTipIcon.Info);

            if (_currentPage == "panel") InjectPanelShell();
        }
        catch (Exception ex)
        {
            AppLog.Info("检查更新失败：" + ex.Message);
            if (!silent)
            {
                MessageBox.Show(this, "检查更新失败：\n" + ex.Message,
                    "检查更新", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
        }
    }

    private async void InjectPanelShell()
    {
        try
        {
            if (!_webReady || _web.CoreWebView2 is null) return;
            await _web.CoreWebView2.ExecuteScriptAsync(PanelShell.Script);
            if (_pendingRelease is not null && !_updateBannerDismissed
                && Updater.IsNewer(_pendingRelease, _updater.CurrentVersion))
            {
                var json = JsonSerializer.Serialize(new
                {
                    current = _updater.CurrentVersion,
                    latest = _pendingRelease.Tag,
                });
                await _web.CoreWebView2.ExecuteScriptAsync(
                    $"window.__wbShellUpdate&&window.__wbShellUpdate.show({json})");
            }
        }
        catch (Exception ex)
        {
            AppLog.Error("注入更新条失败", ex);
        }
    }

    private async Task ExecutePanelScript(string script)
    {
        try
        {
            if (_webReady && _web.CoreWebView2 is not null)
            {
                await _web.CoreWebView2.ExecuteScriptAsync(script);
            }
        }
        catch (Exception ex)
        {
            AppLog.Error("执行页面脚本失败", ex);
        }
    }

    private async Task StartPanelUpdateAsync(bool allowUnverified)
    {
        if (_updating || _pendingRelease is null) return;
        if (!Updater.TryBegin())
        {
            await ExecutePanelScript(
                "window.__wbShellUpdate&&window.__wbShellUpdate.error('已有更新任务正在进行，请稍后再试。',false)");
            return;
        }
        if (_snapshot.State == ServiceState.External)
        {
            Updater.End();
            await ExecutePanelScript(
                "window.__wbShellUpdate&&window.__wbShellUpdate.error('当前连接的是另一个目录的实例，不能在这里安装更新。',false)");
            return;
        }

        var release = _pendingRelease;
        _updating = true;
        SetTrayEnabled(false, false);
        try
        {
            var progress = new Progress<UpdateProgress>(p =>
            {
                var json = JsonSerializer.Serialize(new
                {
                    phase = p.Phase.ToString().ToLowerInvariant(),
                    message = p.Message,
                    percent = Math.Round(p.Percent, 1),
                });
                _ = ExecutePanelScript($"window.__wbShellUpdate&&window.__wbShellUpdate.progress({json})");
            });

            await _updater.UpdateAsync(release, allowUnverified, progress, _cts.Token);
            _pendingRelease = null;
            _miUpdate.Text = "检查更新…";
            await ExecutePanelScript(
                "window.__wbShellUpdate&&window.__wbShellUpdate.done(" +
                JsonSerializer.Serialize($"已更新到 {release.Tag}，服务已重新启动。") + ")");
            await Task.Delay(1500);
            ReloadPanel();
        }
        catch (UpdateException ex)
        {
            AppLog.Error("页面内更新失败", ex);
            var canRollback = ex.CanRollback && _updater.CanRollback;
            await ExecutePanelScript(
                "window.__wbShellUpdate&&window.__wbShellUpdate.error(" +
                JsonSerializer.Serialize(ex.Message) + "," + (canRollback ? "true" : "false") + ")");
        }
        catch (Exception ex)
        {
            AppLog.Error("页面内更新失败", ex);
            await ExecutePanelScript(
                "window.__wbShellUpdate&&window.__wbShellUpdate.error(" +
                JsonSerializer.Serialize("更新失败：" + ex.Message) + "," +
                (_updater.CanRollback ? "true" : "false") + ")");
        }
        finally
        {
            _updating = false;
            Updater.End();
            if (_snapshot.State == ServiceState.Ready) SetTrayEnabled(true, true);
        }
    }

    private async Task RollbackFromPanelAsync()
    {
        if (_updating) return;
        if (!Updater.TryBegin())
        {
            await ExecutePanelScript(
                "window.__wbShellUpdate&&window.__wbShellUpdate.error('已有更新任务正在进行，请稍后再试。',false)");
            return;
        }
        _updating = true;
        SetTrayEnabled(false, false);
        try
        {
            var progress = new Progress<UpdateProgress>(p =>
            {
                var json = JsonSerializer.Serialize(new
                {
                    phase = p.Phase.ToString().ToLowerInvariant(),
                    message = p.Message,
                    percent = Math.Round(p.Percent, 1),
                });
                _ = ExecutePanelScript($"window.__wbShellUpdate&&window.__wbShellUpdate.progress({json})");
            });
            await _updater.RollbackAsync(progress, _cts.Token);
            _pendingRelease = null;
            await ExecutePanelScript(
                "window.__wbShellUpdate&&window.__wbShellUpdate.done(" +
                JsonSerializer.Serialize("已回滚到更新前的版本，服务已重新启动。") + ")");
            await Task.Delay(1500);
            ReloadPanel();
        }
        catch (Exception ex)
        {
            AppLog.Error("页面内回滚失败", ex);
            await ExecutePanelScript(
                "window.__wbShellUpdate&&window.__wbShellUpdate.error(" +
                JsonSerializer.Serialize("回滚失败：" + ex.Message) + "," +
                (_updater.CanRollback ? "true" : "false") + ")");
        }
        finally
        {
            _updating = false;
            Updater.End();
            if (_snapshot.State == ServiceState.Ready) SetTrayEnabled(true, true);
        }
    }

    private void OpenUpdateForm()
    {
        if (_updateForm is { IsDisposed: false } existing)
        {
            existing.Activate();
            return;
        }

        var canInstall = _snapshot.State != ServiceState.External;
        _updateForm = new UpdateForm(_service, _updater, _pendingRelease, canInstall, ReloadPanel,
            _webEnvironment);
        _updateForm.FormClosed += (_, _) =>
        {
            _updateForm = null;
            if (_updateForm is null && _snapshot.State == ServiceState.Ready)
            {
                _miUpdate.Text = "检查更新…";
                _pendingRelease = null;
            }
        };
        _updateForm.Show(this);
        _updateForm.StartCheck();
    }

    // ── 生命周期 ────────────────────────────────────────────

    private void OnFormClosing(object? sender, FormClosingEventArgs e)
    {
        AppLog.Info($"FormClosing reason={e.CloseReason} reallyExit={_reallyExit} cancel={e.Cancel}");
        SaveBounds();
        _settings.Save();

        if (_reallyExit)
        {
            _tray.Visible = false;
            return;
        }

        if (e.CloseReason == CloseReason.WindowsShutDown || e.CloseReason == CloseReason.TaskManagerClosing)
        {
            _reallyExit = true;
            _tray.Visible = false;
            return;
        }

        e.Cancel = true;
        Hide();
        AppLog.Info("已最小化到托盘，服务继续运行");
        if (!_settings.TrayHintShown)
        {
            _settings.TrayHintShown = true;
            _settings.Save();
            _tray.ShowBalloonTip(4000, "已最小化到托盘",
                "服务仍在后台运行。右键托盘图标可以打开面板、重启或退出。", ToolTipIcon.Info);
        }
    }

    private async void RequestExit()
    {
        var stopService = false;
        if (_snapshot is { State: ServiceState.Ready, Owned: true })
        {
            var answer = MessageBox.Show(this,
                "退出后服务仍在后台运行，面板将无法再打开。\n\n要同时停止服务吗？",
                "退出 WorkBuddy Manager",
                MessageBoxButtons.YesNoCancel, MessageBoxIcon.Question);
            if (answer == DialogResult.Cancel) return;
            stopService = answer == DialogResult.Yes;
        }

        if (stopService)
        {
            try
            {
                SetTrayEnabled(false, false);
                await _service.StopAsync(_cts.Token);
            }
            catch (Exception ex)
            {
                AppLog.Error("退出时停止服务失败", ex);
                MessageBox.Show(this, "停止服务失败：\n" + ex.Message,
                    "WorkBuddy Manager", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
        }

        _reallyExit = true;
        _tray.Visible = false;
        _cts.Cancel();
        Close();
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            _showWait?.Unregister(null);
            _updateTimer.Stop();
            _tray.Visible = false;
            _tray.Dispose();
            _web.Dispose();
            _cts.Dispose();
        }
        base.Dispose(disposing);
    }
}
