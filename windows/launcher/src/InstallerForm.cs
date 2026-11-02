using System;
using System.Drawing;
using System.IO;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Win32;

namespace WorkBuddyLauncher
{
    /// 安装向导：欢迎 → 选择位置 → 安装进度 → 完成。
    internal sealed class InstallerForm : WizardForm
    {
        private readonly Panel _pageWelcome;
        private readonly Panel _pageLocation;
        private readonly Panel _pageProgress;
        private readonly Panel _pageDone;

        private TextBox _txtDir;
        private CheckBox _chkDesktop;
        private CheckBox _chkStartMenu;
        private Label _lblSpace;
        private ProgressBar _progress;
        private Label _lblProgressTitle;
        private Label _lblStatus;
        private CheckBox _chkLaunch;
        private Label _lblDoneDetail;

        private int _pageIndex;
        private bool _installFailed;
        private bool _isOverwrite;
        private string _installTarget;

        public InstallerForm()
            : base("安装 " + LauncherPaths.AppName, "WorkBuddy Manager 安装向导 · 内置全部运行环境")
        {
            _pageWelcome = MakePage();
            _pageLocation = MakePage();
            _pageProgress = MakePage();
            _pageDone = MakePage();

            BuildWelcomePage();
            BuildLocationPage();
            BuildProgressPage();
            BuildDonePage();

            ShowPage(_pageWelcome);
            UpdateNav();
        }

        private void BuildWelcomePage()
        {
            MakePageTitle(_pageWelcome, "欢迎使用 " + LauncherPaths.AppName + " 安装向导", 32);
            MakePageText(_pageWelcome,
                "本向导将指导您完成 " + LauncherPaths.AppName + " 的安装过程。\n\n" +
                "安装包已内置运行所需的全部环境（Python 运行时、上游网关、前端页面），\n" +
                "安装完成后即可直接启动，无需再安装任何依赖。",
                84, 90);
            MakePageText(_pageWelcome,
                "建议在继续之前关闭其他正在运行的 WorkBuddy 程序。\n单击「下一步」继续。",
                184, 50).ForeColor = SystemColors.GrayText;
        }

        private void BuildLocationPage()
        {
            MakePageTitle(_pageLocation, "选择安装位置", 28);
            MakePageText(_pageLocation,
                "安装向导将把程序安装到以下文件夹。若要安装到其他位置，请单击「浏览」。",
                66, 24);

            var dirLabel = new Label();
            dirLabel.Text = "安装到(&I):";
            dirLabel.SetBounds(28, 124, 76, 22);
            _pageLocation.Controls.Add(dirLabel);

            _txtDir = new TextBox();
            _txtDir.SetBounds(108, 120, 296, 24);
            _txtDir.Text = LauncherPaths.DefaultInstallDir;
            _pageLocation.Controls.Add(_txtDir);

            // 探测已安装位置（注册表卸载项记录的 InstallLocation），自动回填方便覆盖安装
            using (RegistryKey key = Registry.CurrentUser.OpenSubKey(LauncherPaths.UninstallRegKey))
            {
                if (key != null)
                {
                    string installed = key.GetValue("InstallLocation") as string;
                    if (!string.IsNullOrEmpty(installed) &&
                        Directory.Exists(installed) &&
                        LauncherPaths.LooksInstalled(installed))
                    {
                        _txtDir.Text = installed;
                    }
                }
            }

            var btnBrowse = new Button();
            btnBrowse.Text = "浏览…";
            btnBrowse.SetBounds(412, 119, 64, 26);
            btnBrowse.Click += OnBrowseClick;
            _pageLocation.Controls.Add(btnBrowse);

            _chkDesktop = new CheckBox();
            _chkDesktop.Text = "创建桌面快捷方式";
            _chkDesktop.SetBounds(108, 158, 160, 22);
            _chkDesktop.Checked = true;
            _pageLocation.Controls.Add(_chkDesktop);

            _chkStartMenu = new CheckBox();
            _chkStartMenu.Text = "创建开始菜单快捷方式";
            _chkStartMenu.SetBounds(276, 158, 190, 22);
            _chkStartMenu.Checked = true;
            _pageLocation.Controls.Add(_chkStartMenu);

            _lblSpace = MakePageText(_pageLocation, string.Empty, 198, 40);
            _lblSpace.ForeColor = SystemColors.GrayText;
        }

        private void BuildProgressPage()
        {
            _lblProgressTitle = MakePageTitle(_pageProgress, "正在安装 " + LauncherPaths.AppName, 28);
            MakePageText(_pageProgress, "正在复制文件，请稍候…", 66, 24);

            _progress = new ProgressBar();
            _progress.SetBounds(28, 116, ContentWidth - 56, 22);
            _pageProgress.Controls.Add(_progress);

            _lblStatus = MakePageText(_pageProgress, string.Empty, 150, 60);
            _lblStatus.AutoEllipsis = true;
        }

        private void BuildDonePage()
        {
            MakePageTitle(_pageDone, LauncherPaths.AppName + " 安装完成", 36);
            _lblDoneDetail = MakePageText(_pageDone, string.Empty, 84, 60);

            _chkLaunch = new CheckBox();
            _chkLaunch.Text = "立即启动 " + LauncherPaths.AppName;
            _chkLaunch.SetBounds(28, 158, 300, 24);
            _chkLaunch.Checked = true;
            _pageDone.Controls.Add(_chkLaunch);
        }

        private void UpdateNav()
        {
            // 安装进行中禁止导航；失败时留在当前页显示错误，允许原路径重试或主动返回改路径。
            bool failedOnProgress = _pageIndex == 2 && _installFailed;
            BackButton.Visible = _pageIndex == 1 || failedOnProgress;
            BtnCancel.Visible = _pageIndex != 2 || failedOnProgress;
            NextButton.Visible = _pageIndex != 2 || failedOnProgress;
            BackButton.Text = failedOnProgress ? "返回(&B)" : "< 上一步(&B)";
            NextButton.Text = failedOnProgress ? "重试(&R)" :
                (_pageIndex == 3 ? "完成(&F)" : "下一步(&N) >");
            if (_pageIndex == 1) { UpdateSpaceInfo(); }
        }

        protected override void OnBack()
        {
            if (_pageIndex == 2 && _installFailed)
            {
                _pageIndex = 1;
                ShowPage(_pageLocation);
                UpdateNav();
                return;
            }
            if (_pageIndex != 1) { return; }
            _pageIndex = 0;
            ShowPage(_pageWelcome);
            UpdateNav();
        }

        protected override void OnNext()
        {
            switch (_pageIndex)
            {
                case 0:
                    _pageIndex = 1;
                    ShowPage(_pageLocation);
                    UpdateNav();
                    break;
                case 1:
                    if (ValidateLocation()) { StartInstall(); }
                    break;
                case 2:
                    if (_installFailed) { StartInstall(); }
                    break;
                case 3:
                    FinishAndMaybeLaunch();
                    break;
            }
        }

        private void OnBrowseClick(object sender, EventArgs e)
        {
            using (var dialog = new FolderBrowserDialog())
            {
                dialog.Description = "选择安装目录";
                dialog.SelectedPath = _txtDir.Text.Trim();
                if (dialog.ShowDialog(this) == DialogResult.OK)
                {
                    _txtDir.Text = dialog.SelectedPath;
                }
            }
        }

        /// 把用户选的目录解析成**真正的安装根**（见 ValidateLocation 里的说明）。
        public static string ResolveInstallTarget(string picked)
        {
            string clean = Path.GetFullPath(picked);
            if (LauncherPaths.LooksInstalled(clean)) { return clean; }
            string leaf = Path.GetFileName(clean.TrimEnd(Path.DirectorySeparatorChar,
                Path.AltDirectorySeparatorChar));
            if (string.Equals(leaf, "WorkBuddyManager", StringComparison.OrdinalIgnoreCase))
            {
                return clean;
            }
            return Path.Combine(clean, "WorkBuddyManager");
        }

        private bool ValidateLocation()
        {
            _isOverwrite = false;
            string raw = _txtDir.Text.Trim();
            if (raw.Length == 0)
            {
                MessageBox.Show(this, "请先选择安装目录。", "安装", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return false;
            }

            string target;
            try
            {
                target = Path.GetFullPath(raw);
            }
            catch (Exception ex)
            {
                MessageBox.Show(this, "目录路径无效：" + ex.Message, "安装", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return false;
            }

            if (File.Exists(target))
            {
                MessageBox.Show(this, "目标是一个已存在的文件，请换一个目录。", "安装",
                    MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return false;
            }

            // 目录语义（issue #178）：把用户选的目录当**父目录**，程序装进它的
            // WorkBuddyManager 子目录；只有在「选中的目录本身已经是本程序的安装目录」
            // （覆盖安装）或「它的名字就叫 WorkBuddyManager」时才直接用它。
            // 早先是照字面装进选中目录，于是有人选了自己的 D:\workspace —— 卸载时
            // 那个目录被整份删掉。现在最坏情况也只是子目录陌生，不会把用户的目录当程序根。
            target = ResolveInstallTarget(target);

            if (LauncherPaths.LooksInstalled(target))
            {
                string oldVersion = Maintenance.ReadVersion(target);
                string versionText = oldVersion.Length > 0 ? "（版本 " + oldVersion + "）" : string.Empty;
                // 目标目录里配置的服务端口若正被监听，提示安装时将自动停止
                string runningNote = string.Empty;
                string oldHost;
                int oldPort;
                EnvConfig.ReadHostPort(LauncherPaths.EnvFilePath(target), out oldHost, out oldPort);
                if (HealthProbe.Check(EnvConfig.ToConnectHost(oldHost), oldPort, 800))
                {
                    runningNote = "\n\n检测到服务正在运行，安装时将自动停止。";
                }
                DialogResult overwrite = MessageBox.Show(this,
                    "检测到该目录已安装 " + LauncherPaths.AppName + versionText + "。" + runningNote +
                    "\n\n继续将卸载旧程序文件后安装新版本；个人数据（.env、账号、data）将保留。\n是否继续？",
                    "覆盖安装", MessageBoxButtons.YesNo, MessageBoxIcon.Question);
                if (overwrite != DialogResult.Yes) { return false; }
                _isOverwrite = true;
            }

            if (!HasEnoughSpace(target))
            {
                DialogResult proceed = MessageBox.Show(this,
                    "目标磁盘可用空间不足 300MB，安装可能失败。仍要继续？",
                    "安装", MessageBoxButtons.YesNo, MessageBoxIcon.Warning);
                if (proceed != DialogResult.Yes) { return false; }
            }

            _installTarget = target;
            return true;
        }

        private void StartInstall()
        {
            _installFailed = false;
            _progress.Value = 0;
            _lblProgressTitle.Text = "正在安装 " + LauncherPaths.AppName;
            _lblStatus.Text = "正在复制文件，请稍候…";
            _pageIndex = 2;
            ShowPage(_pageProgress);
            UpdateNav();

            string target = _installTarget;
            Task.Factory.StartNew(delegate { DoInstall(target); })
                .ContinueWith(delegate(Task task)
                {
                    if (task.IsFaulted)
                    {
                        Exception error = task.Exception.GetBaseException();
                        _installFailed = true;
                        _lblProgressTitle.Text = "安装未完成";
                        _lblStatus.Text = "安装失败：" + error.Message +
                            "\n已保留安装目录：" + target + "\n可直接重试，或点「返回」修改安装路径。";
                        UpdateNav();
                        MessageBox.Show(this,
                            "安装失败，已保留所选安装路径。\n\n原因：" + error.Message +
                            "\n\n可在当前页重试，或点「返回」更换安装目录。",
                            "安装失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
                        return;
                    }
                    _lblDoneDetail.Text = "程序已安装到：\n" + target;
                    _pageIndex = 3;
                    ShowPage(_pageDone);
                    UpdateNav();
                }, TaskScheduler.FromCurrentSynchronizationContext());
        }

        private void DoInstall(string target)
        {
            Diag("开始安装 target=" + target + " overwrite=" + _isOverwrite);

            if (_isOverwrite)
            {
                // 覆盖安装：先按「保留数据」卸载旧程序文件（停运行中的服务、清快捷方式/注册表），
                // 数据白名单（.env、data、upstream 认证与配置）原样保留
                Report("正在关闭运行中的控制窗口…");
                Maintenance.CloseRunningLauncher();

                Report("正在停止运行中的服务…");
                Maintenance.StopServices(target);

                Report("正在清理旧程序文件…");
                // 只清注册表，**不动快捷方式**：重写同名 lnk 会让 Windows 丢掉用户
                // 摆好的桌面图标位置（表现为每装一次图标就跳一次）。指向是否正确
                // 交给下面的 EnsureShortcut 判断，目标变了才重建。
                Maintenance.RemoveRegistryEntry();
                Maintenance.DeleteProgramFiles(target, true, null,
                    delegate(int done, int total, string name)
                    {
                        BeginInvokeSafe(delegate
                        {
                            _progress.Maximum = Math.Max(total, 1);
                            _progress.Value = Math.Min(done, Math.Max(total, 1));
                            _lblStatus.Text = "正在清理旧程序文件（" + done + "/" + Math.Max(total, 1) + "）…";
                        });
                    });
                Diag("旧程序清理完成");
            }

            Report("正在解压项目文件（约需十几秒）…");
            var extracted = new System.Collections.Generic.List<string>();
            PayloadInstaller.ExtractPayload(target, OnExtractProgress, extracted);
            Diag("解压完成：" + extracted.Count + " 个条目");

            Report("正在写入主程序与卸载程序…");
            PayloadInstaller.CopySelf(target);
            Diag("主程序复制完成");
            extracted.Add(LauncherPaths.AppExeName);
            extracted.Add(LauncherPaths.UninstallExeName);

            Report("正在记录安装清单…");
            InstallManifest.Write(target, extracted);
            Diag("清单已写入（卸载只删清单与程序自有目录里的文件）");

            if (_chkDesktop.Checked || _chkStartMenu.Checked)
            {
                Report("正在创建快捷方式…");
                string appExe = Path.Combine(target, LauncherPaths.AppExeName);
                if (_chkDesktop.Checked)
                {
                    PayloadInstaller.EnsureShortcut(LauncherPaths.DesktopShortcutPath, appExe, target, LauncherPaths.AppName);
                }
                if (_chkStartMenu.Checked)
                {
                    string menuDir = LauncherPaths.StartMenuFolder;
                    Directory.CreateDirectory(menuDir);
                    PayloadInstaller.EnsureShortcut(Path.Combine(menuDir, LauncherPaths.AppName + ".lnk"),
                        appExe, target, LauncherPaths.AppName);
                    // 卸载项始终重建：它必须指向 Uninstall.exe，用户也不会去挪它，
                    // 不存在「保留图标位置」的需求。
                    PayloadInstaller.CreateShortcut(Path.Combine(menuDir, "卸载 " + LauncherPaths.AppName + ".lnk"),
                        Path.Combine(target, LauncherPaths.UninstallExeName), target, "卸载 " + LauncherPaths.AppName);
                }
                Diag("快捷方式创建完成");
            }

            Report("正在注册卸载信息…");
            Maintenance.RegisterUninstallEntry(target);
            Diag("注册表写入完成");
            Report("安装完成。");
        }

        /// 安装过程诊断日志（%TEMP%\wb-install-diag.log），用于排障。
        private static readonly string DiagLogPath = Path.Combine(Path.GetTempPath(), "wb-install-diag.log");

        private static void Diag(string message)
        {
            try
            {
                File.AppendAllText(DiagLogPath,
                    DateTime.Now.ToString("HH:mm:ss.fff") + " " + message + Environment.NewLine);
            }
            catch { }
        }

        private void OnExtractProgress(int done, int total, string name)
        {
            BeginInvokeSafe(delegate
            {
                _progress.Maximum = total;
                _progress.Value = Math.Min(done, total);
                string shown = name.Length > 56 ? name.Substring(0, 56) + "…" : name;
                _lblStatus.Text = "正在解压（" + done + "/" + total + "）：" + shown;
            });
        }

        private void FinishAndMaybeLaunch()
        {
            if (_chkLaunch.Checked)
            {
                try
                {
                    System.Diagnostics.Process.Start(Path.Combine(_installTarget, LauncherPaths.AppExeName));
                }
                catch (Exception ex)
                {
                    MessageBox.Show(this, "启动失败：" + ex.Message, LauncherPaths.AppName,
                        MessageBoxButtons.OK, MessageBoxIcon.Warning);
                }
            }
            // Application.Run 模式下赋值 DialogResult 不会关窗，必须显式 Close
            Close();
        }

        private void UpdateSpaceInfo()
        {
            string text = "所需空间：约 400 MB";
            try
            {
                string rootPath = Path.GetPathRoot(Path.GetFullPath(_txtDir.Text.Trim()));
                if (!string.IsNullOrEmpty(rootPath))
                {
                    var drive = new DriveInfo(rootPath);
                    if (drive.IsReady)
                    {
                        long freeGB = drive.AvailableFreeSpace / (1024L * 1024 * 1024);
                        text += "      该磁盘可用空间：" + freeGB + " GB";
                    }
                }
            }
            catch { }
            _lblSpace.Text = text;
        }

        private static bool HasEnoughSpace(string target)
        {
            try
            {
                string rootPath = Path.GetPathRoot(Path.GetFullPath(target));
                if (string.IsNullOrEmpty(rootPath)) { return true; }
                var drive = new DriveInfo(rootPath);
                return !drive.IsReady || drive.AvailableFreeSpace >= 300L * 1024 * 1024;
            }
            catch
            {
                return true;
            }
        }

        private void Report(string status)
        {
            BeginInvokeSafe(delegate { _lblStatus.Text = status; });
        }

        private void BeginInvokeSafe(Action action)
        {
            if (!IsHandleCreated || IsDisposed) { return; }
            try { BeginInvoke(action); }
            catch { }
        }
    }
}
