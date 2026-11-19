using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace WorkBuddyLauncher
{
    /// 卸载向导：确认 → 清理进度 → 完成。清理逻辑含停服、快捷方式/注册表删除与自删除。
    internal sealed class UninstallForm : WizardForm
    {
        private readonly string _installDir;
        private readonly Panel _pageConfirm;
        private readonly Panel _pageProgress;
        private readonly Panel _pageDone;

        private Label _lblDir;
        private CheckBox _chkKeepData;
        private ProgressBar _progress;
        private Label _lblStatus;
        private Label _lblDone;

        private int _pageIndex;
        private int _totalFiles;
        private int _failedFiles;
        private int _keptFiles;
        private string[] _keptSamples = new string[0];
        private bool _keepData;

        public UninstallForm()
            : base("卸载 " + LauncherPaths.AppName, "WorkBuddy Manager 卸载向导")
        {
            _installDir = Path.GetDirectoryName(Application.ExecutablePath);

            _pageConfirm = MakePage();
            _pageProgress = MakePage();
            _pageDone = MakePage();

            BuildConfirmPage();
            BuildProgressPage();
            BuildDonePage();

            ShowPage(_pageConfirm);
            UpdateNav();
        }

        private void BuildConfirmPage()
        {
            MakePageTitle(_pageConfirm, "卸载 " + LauncherPaths.AppName, 32);
            MakePageText(_pageConfirm, "将从以下目录删除程序文件：", 78, 22);

            _lblDir = new Label();
            _lblDir.Text = _installDir;
            _lblDir.ForeColor = SystemColors.GrayText;
            _lblDir.AutoEllipsis = true;
            _lblDir.SetBounds(28, 102, ContentWidth - 56, 22);
            _pageConfirm.Controls.Add(_lblDir);

            _chkKeepData = new CheckBox();
            _chkKeepData.Text = "保留数据（账号、配置、日志）";
            _chkKeepData.SetBounds(28, 148, 320, 24);
            _pageConfirm.Controls.Add(_chkKeepData);

            MakePageText(_pageConfirm,
                "取消勾选将删除全部文件与数据，此操作不可撤销。",
                182, 22).ForeColor = SystemColors.GrayText;
        }

        private void BuildProgressPage()
        {
            MakePageTitle(_pageProgress, "正在卸载 " + LauncherPaths.AppName, 28);
            MakePageText(_pageProgress, "正在清理文件，请稍候…", 66, 24);

            _progress = new ProgressBar();
            _progress.SetBounds(28, 116, ContentWidth - 56, 22);
            _pageProgress.Controls.Add(_progress);

            _lblStatus = MakePageText(_pageProgress, string.Empty, 150, 60);
            _lblStatus.AutoEllipsis = true;
        }

        private void BuildDonePage()
        {
            MakePageTitle(_pageDone, "卸载完成", 36);
            _lblDone = MakePageText(_pageDone, string.Empty, 84, 120);
        }

        private void UpdateNav()
        {
            BackButton.Visible = false;
            NextButton.Visible = _pageIndex != 1;
            BtnCancel.Visible = _pageIndex != 1;
            NextButton.Text = _pageIndex == 0 ? "卸载(&U)" : "完成(&F)";
            if (_pageIndex == 1)
            {
                NextButton.Visible = false; // 清理中不可操作
            }
        }

        protected override void OnBack()
        {
        }

        protected override void OnNext()
        {
            PayloadInstaller.Diag("Uninstall OnNext pageIndex=" + _pageIndex);
            if (_pageIndex == 0)
            {
                StartUninstall();
                return;
            }
            if (_pageIndex == 2)
            {
                ScheduleSelfRemovalAndExit();
            }
        }

        private void StartUninstall()
        {
            PayloadInstaller.Diag("StartUninstall looksInstalled=" + LauncherPaths.LooksInstalled(_installDir));
            if (!LauncherPaths.LooksInstalled(_installDir))
            {
                MessageBox.Show(this,
                    "未在安装目录内检测到 WorkBuddy Manager。\n请在开始菜单或安装目录里运行「卸载 WorkBuddy Manager」。",
                    "卸载", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }

            string message = "确定要卸载 " + LauncherPaths.AppName + " 吗？";
            message += _chkKeepData.Checked
                ? "\n\n数据文件（账号、配置、日志）将被保留在安装目录。"
                : "\n\n所有文件与数据都将被删除，此操作不可撤销。";
            if (MessageBox.Show(this, message, "卸载", MessageBoxButtons.YesNo, MessageBoxIcon.Warning)
                != DialogResult.Yes)
            {
                PayloadInstaller.Diag("用户在确认框选择否");
                return;
            }
            PayloadInstaller.Diag("确认通过 keepData=" + _chkKeepData.Checked);

            _keepData = _chkKeepData.Checked;
            _pageIndex = 1;
            ShowPage(_pageProgress);
            UpdateNav();

            Task.Factory.StartNew(delegate { DoUninstall(); })
                .ContinueWith(delegate(Task task)
                {
                    if (task.IsFaulted)
                    {
                        MessageBox.Show(this, "卸载未完成：" + task.Exception.GetBaseException().Message,
                            "卸载", MessageBoxButtons.OK, MessageBoxIcon.Error);
                        _lblStatus.Text = "卸载未完成，可关闭窗口后重试。";
                        _pageIndex = 2;
                        ShowPage(_pageDone);
                        _lblDone.Text = "卸载未完成：" + task.Exception.GetBaseException().Message;
                        UpdateNav();
                        return;
                    }
                    _pageIndex = 2;
                    ShowPage(_pageDone);
                    UpdateNav();
                    ShowDoneSummary();
                }, TaskScheduler.FromCurrentSynchronizationContext());
        }

        private void ShowDoneSummary()
        {
            string done = LauncherPaths.AppName + " 已卸载。";
            if (_keepData) { done += "\n\n保留的数据文件位于：\n" + _installDir; }
            if (_failedFiles > 0)
            {
                done += "\n\n有 " + _failedFiles + " 个文件未能删除（可能被占用），可在重启后手动删除该目录。";
            }
            if (_keptFiles > 0)
            {
                done += "\n\n目录里还有 " + _keptFiles + " 个**不是本程序**的文件，已原样保留（不会被删）：";
                foreach (string sample in _keptSamples) { done += "\n  · " + sample; }
                if (_keptFiles > _keptSamples.Length) { done += "\n  · …等"; }
            }
            _lblDone.Text = done;
        }

        /// 在后台线程执行清理（自删除除外：它必须在窗口关闭后最后执行）。
        private void DoUninstall()
        {
            Report("正在关闭启动器窗口…");
            Maintenance.CloseRunningLauncher();

            Report("正在停止运行中的服务…");
            Maintenance.StopServices(_installDir);

            Report("正在删除快捷方式与注册表…");
            Maintenance.RemoveShortcutsAndRegistry();
            // 开机自启写在 HKCU Run（不在安装清单里），卸载时要单独清；只删
            // 指向本安装目录的值，避免误删同一用户另一份安装的启动项。
            Autostart.DisableIfOwned(_installDir);

            _totalFiles = Maintenance.CountFiles(_installDir);
            _progress.Maximum = Math.Max(_totalFiles, 1);
            Report("正在删除项目文件（共 " + _totalFiles + " 个）…");
            Maintenance.DeletionReport report = Maintenance.DeleteProgramFiles(_installDir, _keepData,
                Application.ExecutablePath,
                delegate(int done, int total, string name)
                {
                    _progress.Maximum = Math.Max(total, 1);
                    _progress.Value = Math.Min(done, Math.Max(total, 1));
                    _lblStatus.Text = "正在删除项目文件（已删除 " + done + "/" + Math.Max(total, 1) + " 个）…";
                });
            if (report.Refused != null)
            {
                // 不是本程序的安装目录：什么都没删，如实说清（issue #178 的护栏）
                throw new InvalidOperationException(report.Refused);
            }
            _failedFiles = report.Failed;
            _keptFiles = report.KeptFiles;
            _keptSamples = report.KeptSamples.ToArray();
        }

        /// 自删除：把自身改名（运行中的 exe 不能直接删），交给 cmd 在进程退出后清理。
        private void ScheduleSelfRemovalAndExit()
        {
            string self = Application.ExecutablePath;
            string renamed = self;
            try
            {
                string oldPath = self + ".old";
                File.Move(self, oldPath);
                renamed = oldPath;
            }
            catch { }

            try
            {
                // 循环重试等本进程真正退出后再删（ping 仅作间隔，最多约 2 分钟）：
                // 一次性 del 会因进程退出慢于 1 秒而落空，留下 .old 残留；
                // 目录非空时 rmdir 自动失败，保留数据模式下不会误删。
                string arguments = "/c for /l %i in (1,1,60) do @(del /f /q \"" + renamed + "\" 2>nul& if not exist \"" + renamed + "\" (rmdir \"" + _installDir + "\" 2>nul& exit)& ping -n 2 127.0.0.1 >nul)";
                var psi = new ProcessStartInfo("cmd.exe", arguments);
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                psi.WindowStyle = ProcessWindowStyle.Hidden;
                Process.Start(psi);
            }
            catch { }

            Application.Exit();
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
