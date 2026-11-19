using System;
using System.IO;

namespace WorkBuddyLauncher
{
    /// 应用级常量与固定路径。
    internal static class LauncherPaths
    {
        public const string AppName = "WorkBuddy Manager";
        public const string AppExeName = "WorkBuddyManager.exe";
        public const string UninstallExeName = "Uninstall.exe";
        public const string UninstallRegKey = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\WorkBuddyManager";

        /// 内嵌 payload 的资源名（csc -resource:payload.zip,payload.zip）。
        public const string PayloadResourceName = "payload.zip";

        /// 安装标识：payload 解压产物之一，用它判断目录里是否是一个安装好的项目。
        private const string InstallMarker = @"server\main.py";

        public static bool LooksInstalled(string dir)
        {
            return !string.IsNullOrEmpty(dir) && File.Exists(Path.Combine(dir, InstallMarker));
        }

        public static string DefaultInstallDir
        {
            get
            {
                return Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "WorkBuddyManager");
            }
        }

        /// 项目内脚本路径（start/prepare/service-tools 等都在 windows\ 下）。
        public static string ScriptPath(string root, string fileName)
        {
            return Path.Combine(Path.Combine(root, "windows"), fileName);
        }

        public static string EnvFilePath(string root)
        {
            return Path.Combine(root, ".env");
        }

        /// 启动器偏好目录（%APPDATA%\WorkBuddyManager）。
        public static string AppDataDir
        {
            get
            {
                return Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "WorkBuddyManager");
            }
        }

        public static string SettingsFile
        {
            get { return Path.Combine(AppDataDir, "launcher.conf"); }
        }

        public static string DesktopShortcutPath
        {
            get
            {
                return Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), AppName + ".lnk");
            }
        }

        /// 开始菜单的程序组目录（%APPDATA%\Microsoft\Windows\Start Menu\Programs\WorkBuddy Manager）。
        public static string StartMenuFolder
        {
            get
            {
                return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), AppName);
            }
        }
    }
}
