using System;
using System.IO;
using System.Threading;
using System.Windows.Forms;

namespace WorkBuddyLauncher
{
    /// 安装器 / 启动器 / 卸载器的统一入口。
    /// 同一个 exe 复制成不同名字后承担不同角色：安装包（任意位置）→ 安装模式；
    /// 安装目录里的 WorkBuddyManager.exe → 控制模式；Uninstall.exe → 卸载模式。
    internal static class Program
    {
        [STAThread]
        private static void Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            // 开机自启带 --tray：只驻留托盘，不显示主窗口（issue #269）。
            // 必须在构造 MainForm 之前解析：是否显示窗口由构造参数决定，而不是
            // 「先 Show 再 Hide」——那样开机瞬间会闪一下窗口。
            bool silent = false;
            foreach (string arg in args)
            {
                if (String.Equals(arg, Autostart.SilentArgument, StringComparison.OrdinalIgnoreCase) ||
                    String.Equals(arg, "--silent", StringComparison.OrdinalIgnoreCase))
                {
                    silent = true;
                }
            }

            // 卸载模式：安装目录里的 Uninstall.exe（按文件名判定）
            string exeName = Path.GetFileName(Application.ExecutablePath);
            if (exeName.StartsWith("Uninstall", StringComparison.OrdinalIgnoreCase))
            {
                Application.Run(new UninstallForm());
                return;
            }

            string exeDir = Path.GetDirectoryName(Application.ExecutablePath);

            // 控制模式：exe 所在目录就是一个安装好的项目（单实例）
            if (LauncherPaths.LooksInstalled(exeDir))
            {
                bool createdNew = false;
                // 第二个实例不再弹「已经在运行」的对话框：
                //   · 手动再点一次快捷方式 → 唤出已在运行的窗口；
                //   · 开机静默启动时已有实例 → 直接退出（用户已经在托盘里了）。
                using (var wake = new EventWaitHandle(false, EventResetMode.AutoReset,
                           @"Local\WorkBuddyManagerLauncherWake"))
                using (new Mutex(true, @"Local\WorkBuddyManagerLauncher", out createdNew))
                {
                    if (!createdNew)
                    {
                        if (!silent) { wake.Set(); }
                        return;
                    }
                    Application.Run(new MainForm(exeDir, silent, wake));
                }
                return;
            }

            // 安装模式：从任意位置双击的安装包
            Application.Run(new InstallerForm());
        }
    }
}
