using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;
using System.Threading.Tasks;

namespace WorkBuddyLauncher
{
    /// 控制模式主窗口：启停/重启项目、状态显示、监听配置、面板入口、托盘与关闭行为。
    internal sealed class MainForm : Form
    {
        private enum ProjectState { Stopped, Starting, Running, Stopping, Restarting, Error }

        /// 脚本执行后的等待目标：等项目就绪（healthz 通过）或等端口释放。
        private enum WaitKind { None, Healthy, Stopped }

        private readonly string _root;
        private readonly AppSettings _settings;
        private readonly ScriptRunner _runner = new ScriptRunner();
        private readonly System.Windows.Forms.Timer _timer = new System.Windows.Forms.Timer();
        private readonly NotifyIcon _tray = new NotifyIcon();

        private Label _lblDot;
        private Label _lblState;
        private Label _lblAddress;
        private Button _btnStart;
        private Button _btnRestart;
        private Button _btnStop;
        private Button _btnOpen;
        private TextBox _txtHost;
        private TextBox _txtPort;
        private Button _btnSave;
        private CheckBox _chkAutostart;
        private Button _btnUpdate;
        private TextBox _log;

        private ProjectState _state = ProjectState.Stopped;
        private WaitKind _wait = WaitKind.None;
        private DateTime _waitSince;
        private bool _probing;
        private bool _reallyExit;
        private bool _openBrowserOnHealthy;
        private bool _credentialLookupRunning;
        private bool _autostartLoading;
        private bool _startHidden;
        private bool _updating;
        private readonly EventWaitHandle _wake;
        private string _currentHost = EnvConfig.DefaultHost;
        private int _currentPort = EnvConfig.DefaultPort;

        public MainForm(string root) : this(root, false, null)
        {
        }

        /// <param name="startHidden">开机自启：首帧不显示窗口，只驻留托盘（issue #269）。</param>
        /// <param name="wake">第二个实例用来唤出本窗口的命名事件（可为 null）。</param>
        public MainForm(string root, bool startHidden, EventWaitHandle wake)
        {
            _root = root;
            _settings = new AppSettings(LauncherPaths.SettingsFile);
            _startHidden = startHidden;
            _wake = wake;

            Text = LauncherPaths.AppName;
            Icon = AppIcon.Value;
            Font = SystemFonts.MessageBoxFont;
            FormBorderStyle = FormBorderStyle.FixedSingle;
            MaximizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(480, 512);

            BuildUi();
            // 横幅后添加（Dock 计算顺序：横幅先停靠顶部，内容面板填充剩余）
            var banner = new BannerPanel(LauncherPaths.AppName, "项目服务控制台", 60);
            Controls.Add(banner);
            try
            {
                SetupTray();
            }
            catch (Exception ex)
            {
                // 托盘注册失败时**回退为显示窗口**：否则开机静默启动会变成
                // 「既无窗口也无图标」，用户只能去任务管理器结束进程（issue #269）。
                _startHidden = false;
                AppendLog("[WARN] 托盘初始化失败，已改为显示主窗口：" + ex.Message);
            }

            if (_wake != null)
            {
                // 第二个实例的唤醒信号：把已经在托盘的窗口拉回前台。
                ThreadPool.RegisterWaitForSingleObject(_wake, delegate
                {
                    BeginInvokeSafe(RestoreFromTray);
                }, null, Timeout.Infinite, false);
            }

            _timer.Interval = 2000;
            _timer.Tick += OnTick;
            _timer.Start();

            LoadConfig();
            SetState(ProjectState.Stopped, null);
            AppendLog("[INFO] 控制窗口已就绪，安装目录：" + _root);
            if (_startHidden)
            {
                AppendLog("[INFO] 已按开机自启静默启动，驻留托盘（双击托盘图标打开窗口）。");
                ShowTrayBalloonOnce();
            }

            Shown += delegate { ProbeAsync(); };
        }

        /// 开机静默启动：Application.Run 会请求显示窗口，这里拦下第一次，
        /// 只创建句柄（托盘与 BeginInvoke 都需要）但不让窗口出现。
        /// 不要用「先 Show 再 Hide」——那会在开机瞬间闪一下窗口。
        protected override void SetVisibleCore(bool value)
        {
            if (_startHidden)
            {
                _startHidden = false;
                base.SetVisibleCore(false);
                if (!IsHandleCreated) { CreateHandle(); }
                return;
            }
            base.SetVisibleCore(value);
        }

        private Panel _content;

        private void BuildUi()
        {
            _content = new Panel();
            _content.Dock = DockStyle.Fill;
            _content.BackColor = Color.White;
            Controls.Add(_content);

            _lblDot = new Label();
            _lblDot.Text = "●";
            _lblDot.Font = new Font(Font.FontFamily, 15f, FontStyle.Regular);
            _lblDot.SetBounds(24, 16, 30, 30);
            _content.Controls.Add(_lblDot);

            _lblState = new Label();
            _lblState.Font = new Font(Font.FontFamily, 13f, FontStyle.Bold);
            _lblState.SetBounds(58, 14, 180, 28);
            _lblState.TextAlign = ContentAlignment.MiddleLeft;
            _content.Controls.Add(_lblState);

            _lblAddress = new Label();
            _lblAddress.ForeColor = SystemColors.GrayText;
            _lblAddress.SetBounds(58, 44, 396, 18);
            _content.Controls.Add(_lblAddress);

            _btnStart = MakeActionButton("启动项目", 24, OnStartClick);
            _btnRestart = MakeActionButton("重启项目", 136, OnRestartClick);
            _btnStop = MakeActionButton("关闭项目", 248, OnStopClick);
            _btnOpen = MakeActionButton("打开面板", 360, OnOpenClick);

            var configBox = new GroupBox();
            configBox.Text = "监听配置（保存后重启项目生效）";
            configBox.SetBounds(24, 126, 432, 72);
            _content.Controls.Add(configBox);

            var hostLabel = new Label();
            hostLabel.Text = "IP 地址";
            hostLabel.SetBounds(14, 32, 48, 20);
            configBox.Controls.Add(hostLabel);

            _txtHost = new TextBox();
            _txtHost.SetBounds(66, 30, 140, 24);
            configBox.Controls.Add(_txtHost);

            var portLabel = new Label();
            portLabel.Text = "端口";
            portLabel.SetBounds(218, 32, 36, 20);
            configBox.Controls.Add(portLabel);

            _txtPort = new TextBox();
            _txtPort.SetBounds(258, 30, 62, 24);
            configBox.Controls.Add(_txtPort);

            _btnSave = new Button();
            _btnSave.Text = "保存配置";
            _btnSave.SetBounds(330, 29, 86, 26);
            _btnSave.Click += OnSaveClick;
            configBox.Controls.Add(_btnSave);

            // 开机自启（issue #269）：HKCU Run，当前用户即可，不需要管理员。
            // 默认关闭；勾选后下次登录静默驻留托盘。
            _chkAutostart = new CheckBox();
            _chkAutostart.Text = "开机自动启动（静默驻留托盘）";
            _chkAutostart.SetBounds(24, 204, 300, 22);
            _chkAutostart.CheckedChanged += OnAutostartChanged;
            _content.Controls.Add(_chkAutostart);

            // 检查更新（issue #288）：查最新 Release → 下载安装包 + 校验 sha256 → 启动安装向导。
            _btnUpdate = new Button();
            _btnUpdate.Text = "检查更新";
            _btnUpdate.SetBounds(330, 202, 126, 26);
            _btnUpdate.Click += OnCheckUpdate;
            _content.Controls.Add(_btnUpdate);

            var logLabel = new Label();
            logLabel.Text = "输出日志";
            logLabel.SetBounds(24, 236, 432, 20);
            _content.Controls.Add(logLabel);

            _log = new TextBox();
            _log.Multiline = true;
            _log.ReadOnly = true;
            _log.ScrollBars = ScrollBars.Vertical;
            _log.BackColor = SystemColors.Window;
            _log.SetBounds(24, 258, 432, 178);
            _content.Controls.Add(_log);
        }

        /// 勾选 / 取消开机自启：写或删 HKCU Run。写失败时把勾选状态退回去，
        /// 不能让界面显示「已开启」而注册表里其实没有。
        private void OnAutostartChanged(object sender, EventArgs e)
        {
            if (_autostartLoading) { return; }
            try
            {
                if (_chkAutostart.Checked)
                {
                    Autostart.Enable();
                    AppendLog("[CFG] 已开启开机自启：" + Autostart.CommandLine());
                }
                else
                {
                    Autostart.Disable();
                    AppendLog("[CFG] 已关闭开机自启。");
                }
            }
            catch (Exception ex)
            {
                _autostartLoading = true;
                _chkAutostart.Checked = !_chkAutostart.Checked;
                _autostartLoading = false;
                MessageBox.Show(this, "设置开机自启失败：" + ex.Message, "开机自启",
                    MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }

        private Button MakeActionButton(string text, int x, EventHandler onClick)
        {
            var button = new Button();
            button.Text = text;
            button.SetBounds(x, 80, 104, 32);
            button.Click += onClick;
            _content.Controls.Add(button);
            return button;
        }

        private void SetupTray()
        {
            _tray.Icon = AppIcon.Value;
            _tray.Text = LauncherPaths.AppName;
            _tray.Visible = true;
            _tray.DoubleClick += delegate { RestoreFromTray(); };

            var menu = new ContextMenuStrip();
            menu.Items.Add("显示主窗口", null, delegate { RestoreFromTray(); });
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add("查看初始管理员密码", null, OnShowInitialCredentials);
            menu.Items.Add("检查更新…", null, OnCheckUpdate);
            menu.Items.Add("重置关闭行为提示", null, OnResetCloseBehavior);
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add("退出启动器", null, OnTrayExit);
            _tray.ContextMenuStrip = menu;
        }

        private string InitialCredentialsShownKey
        {
            get
            {
                // 偏好文件按用户共享，但「首次展示」必须按安装目录隔离，
                // 否则另一份测试安装会抑制正式安装的首次密码提示。
                string path = Path.GetFullPath(_root).TrimEnd(Path.DirectorySeparatorChar).ToUpperInvariant();
                using (SHA256 sha = SHA256.Create())
                {
                    byte[] hash = sha.ComputeHash(Encoding.UTF8.GetBytes(path));
                    return "initial_pwd_shown_" + BitConverter.ToString(hash, 0, 8).Replace("-", "");
                }
            }
        }

        /// 首次启动成功后提示一次初始管理员账号（服务端随机生成，只打印进 stderr 日志）。
        /// 日志可能比 healthz 晚几百毫秒落盘，所以在后台重试几秒；成功后写标记，
        /// 后续再次打开同一安装目录的启动器不会重复弹出。
        private void MaybeShowInitialCredentials()
        {
            if (_settings.Get(InitialCredentialsShownKey, null) != null || _credentialLookupRunning) { return; }
            _credentialLookupRunning = true;
            string errLog = Path.Combine(Path.Combine(_root, "data"), "manager.err.log");
            Task.Factory.StartNew(delegate
            {
                for (int attempt = 0; attempt < 10; attempt++)
                {
                    string user;
                    string password;
                    if (InitialCredentials.TryExtract(errLog, out user, out password)) { return true; }
                    Thread.Sleep(500);
                }
                return false;
            }).ContinueWith(delegate(Task<bool> task)
            {
                _credentialLookupRunning = false;
                if (IsDisposed || task.IsFaulted || !task.Result) { return; }
                ShowInitialCredentials(true);
            }, TaskScheduler.FromCurrentSynchronizationContext());
        }

        private void OnShowInitialCredentials(object sender, EventArgs e)
        {
            ShowInitialCredentials(markShown: false);
        }

        private void ShowInitialCredentials(bool markShown)
        {
            string errLog = Path.Combine(Path.Combine(_root, "data"), "manager.err.log");
            string user;
            string pwd;
            if (!InitialCredentials.TryExtract(errLog, out user, out pwd))
            {
                if (!markShown)
                {
                    string message;
                    if (HasConfiguredAdminPassword())
                    {
                        message = ".env 已配置 WB_ADMIN_PASSWORD，因此服务端不会生成或记录随机初始密码。请使用该配置值登录。";
                    }
                    else if (File.Exists(Path.Combine(Path.Combine(_root, "data"), "users.json")))
                    {
                        message = "该安装目录已有管理员账号，但既没有初始密码文件、日志里也没有初始密码记录。\n" +
                            "通常意味着你已经改过密码（初始密码文件会随之删除）——请使用你改过的密码登录。\n" +
                            "若是覆盖安装且你从未改过密码，密码只以哈希保存、无法从 users.json 反查，" +
                            "请通过受信任的本机重置流程重设密码。";
                    }
                    else
                    {
                        message = "日志中未找到初始账号记录。请先启动项目并等待服务就绪；若 .env 预设了 WB_ADMIN_PASSWORD，则使用该配置值登录。";
                    }
                    MessageBox.Show(this, message, "初始管理员账号", MessageBoxButtons.OK, MessageBoxIcon.Information);
                }
                return;
            }
            if (markShown)
            {
                _settings.Set(InitialCredentialsShownKey, "1");
                _settings.Save();
            }
            try { Clipboard.SetText(pwd); } catch { }
            MessageBox.Show(this,
                "面板的初始管理员账号如下（密码已复制到剪贴板）：\n\n用户名：admin\n密码：" + pwd +
                "\n\n请登录后立即在「设置 → 管理用户」中修改密码。\n" +
                "（若你已修改过密码，此处显示的是初始密码、已失效。随时可在托盘菜单再次查看。）",
                "初始管理员账号", MessageBoxButtons.OK, MessageBoxIcon.Information);
        }

        private bool HasConfiguredAdminPassword()
        {
            try
            {
                string envPath = LauncherPaths.EnvFilePath(_root);
                if (!File.Exists(envPath)) { return false; }
                foreach (string line in File.ReadAllLines(envPath, Encoding.UTF8))
                {
                    int separator = line.IndexOf('=');
                    if (separator <= 0 || line.Substring(0, separator).Trim() != "WB_ADMIN_PASSWORD") { continue; }
                    string value = line.Substring(separator + 1).Trim().Trim('"').Trim();
                    return value.Length > 0 && value != "''" && !value.StartsWith("#", StringComparison.Ordinal);
                }
            }
            catch { }
            return false;
        }

        private void LoadConfig()
        {
            EnvConfig.ReadHostPort(LauncherPaths.EnvFilePath(_root), out _currentHost, out _currentPort);
            _txtHost.Text = _currentHost;
            _txtPort.Text = _currentPort.ToString();
            UpdateAddressLabel();

            // 初始勾选状态不能触发写入：用 _autostartLoading 静音 CheckedChanged。
            _autostartLoading = true;
            _chkAutostart.Checked = Autostart.IsEnabled();
            _autostartLoading = false;
        }

        private string AddressUrl()
        {
            return "http://" + EnvConfig.ToConnectHost(_currentHost) + ":" + _currentPort + "/";
        }

        private void UpdateAddressLabel()
        {
            _lblAddress.Text = "面板地址：" + AddressUrl();
        }

        private void SetState(ProjectState state, string note)
        {
            _state = state;
            string text;
            Color color;
            switch (state)
            {
                case ProjectState.Starting: text = "启动中…"; color = Color.DarkOrange; break;
                case ProjectState.Running: text = "运行中"; color = Color.ForestGreen; break;
                case ProjectState.Stopping: text = "停止中…"; color = Color.DarkOrange; break;
                case ProjectState.Restarting: text = "重启中…"; color = Color.DarkOrange; break;
                case ProjectState.Error: text = "异常"; color = Color.Firebrick; break;
                default: text = "未运行"; color = Color.Gray; break;
            }
            if (note != null) { text = note; }
            _lblState.Text = text;
            _lblState.ForeColor = color;
            _lblDot.ForeColor = color;
            UpdateButtons();
        }

        private void UpdateButtons()
        {
            bool busy = _runner.IsRunning || _wait != WaitKind.None;
            bool running = _state == ProjectState.Running;
            _btnStart.Enabled = !busy && !running;
            _btnRestart.Enabled = !busy && running;
            _btnStop.Enabled = !busy && running;
            _btnOpen.Enabled = running;
            _btnSave.Enabled = !busy;
            _txtHost.Enabled = !busy;
            _txtPort.Enabled = !busy;
        }

        // ── 操作按钮 ──────────────────────────────────────────────

        private void OnStartClick(object sender, EventArgs e)
        {
            string host = _txtHost.Text.Trim();
            int port;
            string error;
            if (!EnvConfig.TryValidateHost(host, out error) ||
                !EnvConfig.TryValidatePort(_txtPort.Text, out port, out error))
            {
                MessageBox.Show(this, error, "配置无效", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            try
            {
                // 界面上的配置先落盘，保证服务按用户填写的地址启动
                EnvConfig.WriteHostPort(LauncherPaths.EnvFilePath(_root), host, port);
            }
            catch (Exception ex)
            {
                MessageBox.Show(this, "写入配置失败：" + ex.Message, "启动", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }
            _currentHost = host;
            _currentPort = port;
            UpdateAddressLabel();

            // 端口已被其他程序占用时立即提示（常见：机器上另有一个实例占着默认端口），
            // 而不是进入「启动中…」傻等健康检查超时。
            // 探测用连接地址，占用判断用**监听地址**（`::` 与 `0.0.0.0` 各自映射到
            // 对应的 Any，否则通配监听会被误判成端口空闲）。
            if (!HealthProbe.Check(EnvConfig.ToConnectHost(host), port, 1000) &&
                PortCheck.InUse(host, port))
            {
                MessageBox.Show(this,
                    "端口 " + port + " 已被其他程序占用，无法在此端口启动。\n" +
                    "请更换端口（或先停掉占用它的程序）后重试。",
                    "启动", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                SetState(ProjectState.Stopped, null);
                return;
            }

            SetState(ProjectState.Starting, null);
            AppendLog("[RUN] 正在准备运行环境…");
            _runner.Run(LauncherPaths.ScriptPath(_root, "prepare.ps1"), null, AppendLogSafe,
                delegate(int prepareCode)
                {
                    BeginInvokeSafe(delegate
                    {
                        if (prepareCode != 0)
                        {
                            SetState(ProjectState.Error, "环境准备失败");
                            AppendLog("[ERROR] 环境准备失败（退出码 " + prepareCode + "），请查看上方输出。");
                            return;
                        }
                        AppendLog("[RUN] 正在启动服务…");
                        _runner.Run(LauncherPaths.ScriptPath(_root, "service-tools.ps1"), new[] { "start" },
                            AppendLogSafe, delegate(int startCode)
                            {
                                BeginInvokeSafe(delegate
                                {
                                    if (startCode != 0)
                                    {
                                        SetState(ProjectState.Error, "启动失败（退出码 " + startCode + "）");
                                        return;
                                    }
                                    _wait = WaitKind.Healthy;
                                    _waitSince = DateTime.Now;
                                    _openBrowserOnHealthy = true;
                                    AppendLog("[RUN] 服务进程已拉起，等待健康检查…");
                                    UpdateButtons();
                                });
                            });
                    });
                });
            UpdateButtons();
        }

        private void OnRestartClick(object sender, EventArgs e)
        {
            SetState(ProjectState.Restarting, null);
            AppendLog("[RUN] 正在重启服务…");
            _runner.Run(LauncherPaths.ScriptPath(_root, "service-tools.ps1"), new[] { "restart" },
                AppendLogSafe, delegate(int code)
                {
                    BeginInvokeSafe(delegate
                    {
                        if (code != 0)
                        {
                            SetState(ProjectState.Error, "重启失败（退出码 " + code + "）");
                            return;
                        }
                        _wait = WaitKind.Healthy;
                        _waitSince = DateTime.Now;
                        _openBrowserOnHealthy = false;
                        AppendLog("[RUN] 等待服务恢复…");
                        UpdateButtons();
                    });
                });
            UpdateButtons();
        }

        private void OnStopClick(object sender, EventArgs e)
        {
            SetState(ProjectState.Stopping, null);
            AppendLog("[RUN] 正在停止服务…");
            _runner.Run(LauncherPaths.ScriptPath(_root, "service-tools.ps1"), new[] { "stop" },
                AppendLogSafe, delegate(int code)
                {
                    BeginInvokeSafe(delegate
                    {
                        _wait = WaitKind.Stopped;
                        _waitSince = DateTime.Now;
                        AppendLog("[RUN] 停止指令已执行，等待端口释放…");
                        UpdateButtons();
                    });
                });
            UpdateButtons();
        }

        private void OnOpenClick(object sender, EventArgs e)
        {
            OpenPanel();
        }

        private void OpenPanel()
        {
            string url = AddressUrl();
            // ShellExecute 在 DDE/浏览器冷启动时可能阻塞数秒，放后台线程避免卡 UI
            Task.Factory.StartNew(delegate
            {
                try
                {
                    Process.Start(url);
                    AppendLogSafe("[WEB] 已打开面板：" + url);
                }
                catch (Exception ex)
                {
                    AppendLogSafe("[ERROR] 打开浏览器失败：" + ex.Message);
                }
            });
        }

        private void OnSaveClick(object sender, EventArgs e)
        {
            string host = _txtHost.Text.Trim();
            int port;
            string error;
            if (!EnvConfig.TryValidateHost(host, out error) ||
                !EnvConfig.TryValidatePort(_txtPort.Text, out port, out error))
            {
                MessageBox.Show(this, error, "配置无效", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            try
            {
                EnvConfig.WriteHostPort(LauncherPaths.EnvFilePath(_root), host, port);
            }
            catch (Exception ex)
            {
                MessageBox.Show(this, "保存失败：" + ex.Message, "保存配置",
                    MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }
            _currentHost = host;
            _currentPort = port;
            UpdateAddressLabel();
            AppendLog("[CFG] 已保存监听配置：" + AddressUrl());
            string tip = _state == ProjectState.Running ? "已保存。重启项目后生效。" : "已保存。";
            MessageBox.Show(this, tip, "保存配置", MessageBoxButtons.OK, MessageBoxIcon.Information);
        }

        // ── 状态轮询 ──────────────────────────────────────────────

        private void OnTick(object sender, EventArgs e)
        {
            if (_probing || _runner.IsRunning) { return; }
            ProbeAsync();
        }

        private void ProbeAsync()
        {
            _probing = true;
            string host = EnvConfig.ToConnectHost(_currentHost);
            int port = _currentPort;
            Task.Factory.StartNew(delegate { return HealthProbe.Check(host, port, 1500); })
                .ContinueWith(delegate(Task<bool> task)
                {
                    _probing = false;
                    if (IsDisposed) { return; }
                    OnProbeResult(!task.IsFaulted && task.Result);
                }, TaskScheduler.FromCurrentSynchronizationContext());
        }

        private void OnProbeResult(bool healthy)
        {
            if (_runner.IsRunning) { return; }

            if (_wait == WaitKind.Healthy)
            {
                if (healthy)
                {
                    _wait = WaitKind.None;
                    SetState(ProjectState.Running, null);
                    AppendLog("[OK] 服务已就绪：" + AddressUrl());
                    if (_openBrowserOnHealthy)
                    {
                        _openBrowserOnHealthy = false;
                        OpenPanel();
                    }
                    // 无论是刚点击启动，还是控制台打开时服务本来就在运行，都尝试读取一次初始密码
                    MaybeShowInitialCredentials();
                    return;
                }
                double seconds = (DateTime.Now - _waitSince).TotalSeconds;
                if (seconds > 600)
                {
                    _wait = WaitKind.None;
                    _openBrowserOnHealthy = false;
                    SetState(ProjectState.Error, "启动超时");
                    AppendLog("[ERROR] 等待健康检查超过 10 分钟，请查看上方输出日志排查。");
                    return;
                }
                _lblState.Text = "启动中…（已等待 " + (int)seconds + " 秒）";
                return;
            }

            if (_wait == WaitKind.Stopped)
            {
                if (!healthy)
                {
                    _wait = WaitKind.None;
                    SetState(ProjectState.Stopped, null);
                    AppendLog("[OK] 服务已停止。");
                    return;
                }
                double seconds = (DateTime.Now - _waitSince).TotalSeconds;
                if (seconds > 20)
                {
                    _wait = WaitKind.None;
                    SetState(ProjectState.Running, null);
                    AppendLog("[WARN] 服务仍在运行，请查看上方输出日志。");
                    return;
                }
                _lblState.Text = "停止中…（已等待 " + (int)seconds + " 秒）";
                return;
            }

            SetState(healthy ? ProjectState.Running : ProjectState.Stopped, null);
            if (healthy) { MaybeShowInitialCredentials(); }
        }

        // ── 托盘与关闭行为 ────────────────────────────────────────

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            PayloadInstaller.Diag("OnFormClosing reason=" + e.CloseReason + " reallyExit=" + _reallyExit);
            if (!_reallyExit && e.CloseReason == CloseReason.UserClosing)
            {
                string action = _settings.Get("close_action", null);
                PayloadInstaller.Diag("close_action=" + (action ?? "<null>"));
                if (action == null)
                {
                    using (var dialog = new CloseDialog())
                    {
                        DialogResult result = dialog.ShowDialog(this);
                        PayloadInstaller.Diag("CloseDialog result=" + result);
                        if (result == DialogResult.Cancel)
                        {
                            e.Cancel = true;
                            return;
                        }
                        action = result == DialogResult.Yes ? "minimize" : "exit";
                        if (dialog.RememberChoice)
                        {
                            _settings.Set("close_action", action);
                            _settings.Save();
                        }
                    }
                }
                if (action == "minimize")
                {
                    e.Cancel = true;
                    HideToTray();
                    return;
                }
                _reallyExit = true; // 退出启动器：项目服务继续在后台运行
            }
            base.OnFormClosing(e);
        }

        protected override void OnFormClosed(FormClosedEventArgs e)
        {
            _timer.Stop();
            _tray.Visible = false;
            _tray.Dispose();
            base.OnFormClosed(e);
        }

        private void HideToTray()
        {
            Hide();
            ShowTrayBalloonOnce();
        }

        /// 托盘气泡只弹一次（用户已经知道托盘图标之后不再打扰）。
        /// 开机静默启动复用同一标记：第一次开机会看到一次提示，之后安静驻留。
        private void ShowTrayBalloonOnce()
        {
            if (_settings.Get("tray_balloon_shown", null) != null) { return; }
            try
            {
                _tray.ShowBalloonTip(3000, LauncherPaths.AppName,
                    "已最小化到托盘，双击图标可恢复窗口。", ToolTipIcon.Info);
                _settings.Set("tray_balloon_shown", "1");
                _settings.Save();
            }
            catch
            {
                // 开机瞬间 explorer 可能还没准备好接收气泡：不写标记，
                // 下次最小化到托盘时还能再提示一次。
            }
        }

        private void RestoreFromTray()
        {
            Show();
            WindowState = FormWindowState.Normal;
            Activate();
        }

        private void OnResetCloseBehavior(object sender, EventArgs e)
        {
            _settings.Remove("close_action");
            _settings.Save();
            MessageBox.Show(this, "已恢复询问：下次关闭窗口时会再次弹出选择。", LauncherPaths.AppName,
                MessageBoxButtons.OK, MessageBoxIcon.Information);
        }

        private void OnTrayExit(object sender, EventArgs e)
        {
            _reallyExit = true;
            Close();
        }

        // ── 检查更新（issue #288）─────────────────────────────────
        //
        // 只做三件事：查最新 Release、下载安装包与同目录 .sha256、校验后启动
        // 安装向导。**不做静默覆盖**：安装器没有静默参数，而且装到一半失败时
        // 用户需要看到向导给出的原因。账号、.env 与 data 由安装器的覆盖安装
        // 逻辑保留（见 InstallerForm.DoInstall 的保留白名单）。

        private const string LatestReleaseApi =
            "https://api.github.com/repos/ithtelab/workbuddy-manager/releases/latest";
        private const string SetupAssetName = "WorkBuddyManagerSetup.exe";

        private sealed class UpdateInfo
        {
            public string Version;
            public string SetupUrl;
            public string ShaUrl;
        }

        private void OnCheckUpdate(object sender, EventArgs e)
        {
            if (_updating) { return; }
            _updating = true;
            _btnUpdate.Enabled = false;
            AppendLog("[UPDATE] 正在检查最新版本…");
            Task.Factory.StartNew(delegate { RunUpdateCheck(); });
        }

        private void RunUpdateCheck()
        {
            try
            {
                // GitHub 只接受 TLS 1.2+；.NET 4.x 的默认协议在部分机器上仍是
                // TLS 1.0（Windows 10 早期版本）。用数值 3072 而不是枚举名，
                // 是为了在 .NET 4.0 的编译引用下也能通过（运行时 4.8 支持）。
                try
                {
                    ServicePointManager.SecurityProtocol |=
                        (SecurityProtocolType)3072;
                }
                catch { }

                string json = HttpGet(LatestReleaseApi, 20);
                UpdateInfo info = ParseLatestRelease(json);
                if (info == null)
                {
                    throw new InvalidDataException("发布信息里没有找到安装包或 SHA-256 摘要");
                }
                Version latest = ParseVersion(info.Version);
                Version current = CurrentVersion();
                if (latest == null)
                {
                    throw new InvalidDataException("无法识别最新版本号：" + info.Version);
                }
                if (current != null && latest <= current)
                {
                    AppendLog("[UPDATE] 已是最新版本 v" + current + "。");
                    ShowInfoOnUi("已是最新版本（v" + current + "）。");
                    return;
                }

                string message = "发现新版本 v" + latest
                    + (current != null ? "（当前 v" + current + "）" : "")
                    + "。\n\n是否下载并安装？\n\n"
                    + "安装程序会覆盖当前程序文件；.env、账号与 data 目录会保留。\n"
                    + "下载完成后会先用发布页的 SHA-256 校验安装包，再启动安装向导。";
                if (!ConfirmOnUi(message)) { return; }

                string dir = Path.Combine(Path.GetTempPath(),
                    "WorkBuddyManager-update-" + DateTime.Now.ToString("yyyyMMddHHmmss"));
                Directory.CreateDirectory(dir);
                string setupPath = Path.Combine(dir, SetupAssetName);
                string shaPath = setupPath + ".sha256";

                AppendLog("[UPDATE] 正在下载 " + SetupAssetName + "…");
                DownloadFile(info.SetupUrl, setupPath, "安装包");
                DownloadFile(info.ShaUrl, shaPath, "摘要");
                string expected = ReadExpectedSha(shaPath);
                if (!VerifySha256(setupPath, expected))
                {
                    throw new InvalidDataException(
                        "安装包 SHA-256 与发布页不一致，已中止（未运行安装程序）");
                }

                AppendLog("[UPDATE] 校验通过，正在启动安装向导…");
                Process.Start(new ProcessStartInfo
                {
                    FileName = setupPath,
                    WorkingDirectory = dir,
                    UseShellExecute = true,
                });
                // 安装器的覆盖流程会关闭旧的控制窗口；这里先退出，避免占用 exe。
                BeginInvokeSafe(delegate
                {
                    _reallyExit = true;
                    Close();
                });
            }
            catch (Exception ex)
            {
                AppendLog("[ERROR] 检查更新失败：" + ex.Message);
                ShowInfoOnUi("检查更新失败：" + ex.Message
                    + "\n\n可以先从 Releases 页面手动下载覆盖安装。");
            }
            finally
            {
                BeginInvokeSafe(delegate
                {
                    _updating = false;
                    _btnUpdate.Enabled = true;
                });
            }
        }

        private static UpdateInfo ParseLatestRelease(string json)
        {
            string version = MatchGroup(json, "\"tag_name\"\\s*:\\s*\"v?([^\"]+)\"");
            string setup = AssetUrl(json, SetupAssetName);
            string sha = AssetUrl(json, SetupAssetName + ".sha256");
            if (string.IsNullOrEmpty(version) || string.IsNullOrEmpty(setup)
                || string.IsNullOrEmpty(sha))
            {
                return null;
            }
            if (!IsHttps(setup) || !IsHttps(sha))
            {
                return null;   // 发布资产必须是 HTTPS，不接受降级
            }
            return new UpdateInfo { Version = version.Trim(), SetupUrl = setup, ShaUrl = sha };
        }

        private static string AssetUrl(string json, string assetName)
        {
            string pattern = "\"name\"\\s*:\\s*\"" + Regex.Escape(assetName)
                + "\"[\\s\\S]{0,2500}?\"browser_download_url\"\\s*:\\s*\"([^\"]+)\"";
            return MatchGroup(json, pattern);
        }

        private static string MatchGroup(string text, string pattern)
        {
            Match m = Regex.Match(text, pattern);
            return m.Success ? m.Groups[1].Value : "";
        }

        private static bool IsHttps(string url)
        {
            Uri uri;
            return Uri.TryCreate(url, UriKind.Absolute, out uri)
                && uri.Scheme == Uri.UriSchemeHttps;
        }

        private static Version CurrentVersion()
        {
            Version v = ParseVersion(Application.ProductVersion);
            if (v != null) { return v; }
            try
            {
                return ParseVersion(FileVersionInfo.GetVersionInfo(
                    Application.ExecutablePath).FileVersion);
            }
            catch
            {
                return null;
            }
        }

        private static Version ParseVersion(string raw)
        {
            string text = (raw ?? "").Trim().TrimStart('v', 'V');
            int cut = text.IndexOfAny(new[] { '-', '+', ' ' });
            if (cut >= 0) { text = text.Substring(0, cut); }
            Version parsed;
            return Version.TryParse(text, out parsed) ? parsed : null;
        }

        private static string ReadExpectedSha(string path)
        {
            string text = File.ReadAllText(path, Encoding.UTF8).Trim();
            int cut = text.IndexOfAny(new[] { ' ', '\t', '\r', '\n' });
            if (cut > 0) { text = text.Substring(0, cut); }
            text = text.ToLowerInvariant();
            if (!Regex.IsMatch(text, "^[0-9a-f]{64}$"))
            {
                throw new InvalidDataException("发布页的 SHA-256 摘要格式不正确");
            }
            return text;
        }

        private static bool VerifySha256(string path, string expected)
        {
            using (SHA256 sha = SHA256.Create())
            using (FileStream fs = File.OpenRead(path))
            {
                string actual = BitConverter.ToString(sha.ComputeHash(fs))
                    .Replace("-", "").ToLowerInvariant();
                return String.Equals(actual, expected, StringComparison.Ordinal);
            }
        }

        private static string HttpGet(string url, int timeoutSeconds)
        {
            HttpWebRequest request = (HttpWebRequest)WebRequest.Create(url);
            request.UserAgent = "WorkBuddyManager-Launcher";
            request.Accept = "application/vnd.github+json";
            request.Timeout = timeoutSeconds * 1000;
            request.ReadWriteTimeout = timeoutSeconds * 1000;
            ApplyUpdateProxy(request);
            using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
            using (StreamReader reader = new StreamReader(
                       response.GetResponseStream(), Encoding.UTF8))
            {
                return reader.ReadToEnd();
            }
        }

        private void DownloadFile(string url, string path, string label)
        {
            HttpWebRequest request = (HttpWebRequest)WebRequest.Create(url);
            request.UserAgent = "WorkBuddyManager-Launcher";
            request.Timeout = 30000;
            request.ReadWriteTimeout = 30000;
            ApplyUpdateProxy(request);
            using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
            using (Stream input = response.GetResponseStream())
            using (FileStream output = File.Create(path))
            {
                byte[] buffer = new byte[81920];
                long total = response.ContentLength;
                long read = 0;
                int lastBucket = -1;
                int n;
                while ((n = input.Read(buffer, 0, buffer.Length)) > 0)
                {
                    output.Write(buffer, 0, n);
                    read += n;
                    if (total > 0)
                    {
                        int bucket = (int)(read * 10 / total);
                        if (bucket != lastBucket)
                        {
                            lastBucket = bucket;
                            AppendLogSafe("[UPDATE] " + label + "下载 " + (bucket * 10) + "%");
                        }
                    }
                }
            }
        }

        private static void ApplyUpdateProxy(HttpWebRequest request)
        {
            // 与服务器端更新器同一个约定：国内网络访问 GitHub 时可设 WB_UPDATE_PROXY。
            string proxy = Environment.GetEnvironmentVariable("WB_UPDATE_PROXY");
            if (!String.IsNullOrEmpty(proxy))
            {
                request.Proxy = new WebProxy(proxy);
            }
        }

        private bool ConfirmOnUi(string message)
        {
            bool confirmed = false;
            if (!IsHandleCreated || IsDisposed) { return false; }
            try
            {
                Invoke(new Action(delegate
                {
                    confirmed = MessageBox.Show(this, message, "检查更新",
                        MessageBoxButtons.YesNo, MessageBoxIcon.Question) == DialogResult.Yes;
                }));
            }
            catch
            {
                return false;
            }
            return confirmed;
        }

        private void ShowInfoOnUi(string message)
        {
            BeginInvokeSafe(delegate
            {
                MessageBox.Show(this, message, "检查更新",
                    MessageBoxButtons.OK, MessageBoxIcon.Information);
            });
        }

        // ── 日志与线程辅助 ────────────────────────────────────────

        private void AppendLog(string line)
        {
            if (_log.IsDisposed) { return; }
            _log.AppendText(DateTime.Now.ToString("HH:mm:ss") + "  " + line + Environment.NewLine);
            if (_log.Lines.Length > 520)
            {
                string[] lines = _log.Lines;
                string[] keep = new string[400];
                Array.Copy(lines, lines.Length - 400, keep, 0, 400);
                _log.Lines = keep;
            }
            _log.SelectionStart = _log.TextLength;
            _log.ScrollToCaret();
        }

        private void AppendLogSafe(string line)
        {
            BeginInvokeSafe(delegate { AppendLog(line); });
        }

        private void BeginInvokeSafe(Action action)
        {
            if (!IsHandleCreated || IsDisposed) { return; }
            try { BeginInvoke(action); }
            catch { }
        }
    }
}
