using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

namespace WorkBuddyLauncher
{
    /// 安装/卸载共用的清理与登记逻辑：
    /// 覆盖安装前的旧程序清理、卸载时的服务停止与残留清除都走这里，保证两处行为一致。
    internal static class Maintenance
    {
        /// 保留数据白名单（相对安装根；覆盖安装与卸载的「保留数据」都按它跳过）。
        public static readonly string[] KeepPaths = { ".env", "data", "upstream\\auths", "upstream\\config.json" };

        /// 结束正在运行的控制窗口（WorkBuddyManager.exe）。
        public static void CloseRunningLauncher()
        {
            foreach (Process process in Process.GetProcessesByName("WorkBuddyManager"))
            {
                try
                {
                    process.CloseMainWindow();
                    if (!process.WaitForExit(3000)) { process.Kill(); process.WaitForExit(3000); }
                }
                catch { }
                finally { process.Dispose(); }
            }
        }

        /// 停止安装目录里配置的后台服务（service-tools stop，幂等）。
        public static void StopServices(string installDir)
        {
            string serviceTools = Path.Combine(Path.Combine(installDir, "windows"), "service-tools.ps1");
            if (File.Exists(serviceTools))
            {
                ScriptRunner.RunWait(serviceTools, new[] { "stop" });
            }
        }

        /// 删除桌面 / 开始菜单快捷方式与注册表卸载项（**卸载**用）。
        public static void RemoveShortcutsAndRegistry()
        {
            RemoveShortcuts();
            RemoveRegistryEntry();
        }

        /// 只删注册表卸载项。
        ///
        /// **覆盖安装用**：快捷方式文件必须留着 —— 重写同名 .lnk 会让 Windows
        /// 丢掉用户摆好的桌面图标位置，表现为「每装一次图标就跳一次」。
        /// 指向的修正交给 PayloadInstaller.EnsureShortcut（目标变了才重建）。
        public static void RemoveRegistryEntry()
        {
            try { Registry.CurrentUser.DeleteSubKeyTree(LauncherPaths.UninstallRegKey, false); }
            catch { }
        }

        /// 删除桌面 / 开始菜单快捷方式（卸载用）。
        public static void RemoveShortcuts()
        {
            TryDeleteFile(Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
                LauncherPaths.AppName + ".lnk"));
            string menuDir = LauncherPaths.StartMenuFolder;
            TryDeleteFile(Path.Combine(menuDir, LauncherPaths.AppName + ".lnk"));
            TryDeleteFile(Path.Combine(menuDir, "卸载 " + LauncherPaths.AppName + ".lnk"));
            try { Directory.Delete(menuDir, false); }
            catch { }
        }

        /// 删除结果：删了几个、失败几个、**保留**了几个（不属于本程序的文件）。
        public sealed class DeletionReport
        {
            public int Deleted;
            public int Failed;
            public int KeptFiles;
            public readonly List<string> KeptSamples = new List<string>();
            public string Refused;
        }

        /// 删除安装目录里的**程序文件**；keepData 时跳过白名单数据，selfExclude 为运行中的自身 exe。
        ///
        /// 只删「本程序的东西」：安装清单里登记的（`InstallManifest`）、外加程序自己会创建
        /// 的顶层目录/文件。**别的文件一律不动**，并在报告里如实列出 —— 这条规则来自
        /// issue #178：早先递归删光整个安装目录，用户把程序装在自己的 `D:\workspace`
        /// 里，卸载把那个目录连同他的资料一起删了。
        ///
        /// 另外：本目录必须先被认成**本程序的安装根**（server\main.py 或清单存在），
        /// 否则直接拒绝（`Refused` 带原因）——在别处误点卸载不会伤到任何文件。
        public static DeletionReport DeleteProgramFiles(string installDir, bool keepData, string selfExclude,
            Action<int, int, string> progress)
        {
            var report = new DeletionReport();
            if (!InstallManifest.LooksLikeInstallRoot(installDir))
            {
                report.Refused = "这个目录看起来不是 " + LauncherPaths.AppName +
                    " 的安装目录（没找到 server\\main.py，也没有安装清单），已中止删除。";
                return report;
            }
            deleted = 0;
            int total = CountFiles(installDir);
            var manifest = InstallManifest.Read(installDir);
            report.Failed = DeleteTree(installDir, string.Empty, keepData, selfExclude,
                manifest, report,
                delegate(int done)
                {
                    if (progress != null) { progress(done, total, string.Empty); }
                });
            report.Deleted = deleted;
            if (progress != null) { progress(total, total, string.Empty); }
            return report;
        }

        private static int deleted;

        private static int DeleteTree(string root, string relative, bool keepData, string selfExclude,
            HashSet<string> manifest, DeletionReport report, Action<int> onProgress)
        {
            int failures = 0;
            string absolute = relative.Length == 0 ? root : Path.Combine(root, relative);

            string[] files;
            try { files = Directory.GetFiles(absolute); }
            catch { return 0; }
            foreach (string file in files)
            {
                string rel = relative.Length == 0 ? Path.GetFileName(file) : relative + "\\" + Path.GetFileName(file);
                if (ShouldKeep(rel, keepData)) { continue; }
                if (!InstallManifest.IsOwned(rel, manifest))
                {
                    // 不是本程序的文件：留着，记一笔（用户能在卸载完成页里看到）
                    report.KeptFiles++;
                    if (report.KeptSamples.Count < 5) { report.KeptSamples.Add(rel); }
                    continue;
                }
                if (selfExclude != null &&
                    string.Equals(file, selfExclude, StringComparison.OrdinalIgnoreCase)) { continue; }
                if (!TryDeleteFile(file)) { failures++; }
                deleted++;
                if (deleted % 40 == 0 && onProgress != null) { onProgress(deleted); }
            }

            string[] dirs;
            try { dirs = Directory.GetDirectories(absolute); }
            catch { return failures; }
            foreach (string dir in dirs)
            {
                string rel = relative.Length == 0 ? Path.GetFileName(dir) : relative + "\\" + Path.GetFileName(dir);
                if (ShouldKeep(rel, keepData)) { continue; }
                failures += DeleteTree(root, rel, keepData, selfExclude, manifest, report, onProgress);
                // 只在真的空了才删目录：里面有别人的文件时留着它
                try
                {
                    if (Directory.GetFileSystemEntries(dir).Length == 0) { Directory.Delete(dir, false); }
                }
                catch { }
            }
            return failures;
        }

        public static bool ShouldKeep(string relative, bool keepData)
        {
            if (!keepData) { return false; }
            string path = relative.Replace('/', '\\').TrimEnd('\\').ToLowerInvariant();
            foreach (string keep in KeepPaths)
            {
                if (path == keep || path.StartsWith(keep + "\\")) { return true; }
            }
            return false;
        }

        private static bool TryDeleteFile(string path)
        {
            for (int attempt = 0; attempt < 3; attempt++)
            {
                try { File.Delete(path); return true; }
                catch { Thread.Sleep(150); }
            }
            return !File.Exists(path);
        }

        public static int CountFiles(string dir)
        {
            int count = 0;
            try
            {
                count += Directory.GetFiles(dir).Length;
                foreach (string sub in Directory.GetDirectories(dir))
                {
                    count += CountFiles(sub);
                }
            }
            catch { }
            return count;
        }

        public static string ReadVersion(string installDir)
        {
            try
            {
                string file = Path.Combine(installDir, ".version");
                if (File.Exists(file)) { return File.ReadAllText(file, Encoding.UTF8).Trim(); }
            }
            catch { }
            return string.Empty;
        }

        /// 注册「设置 → 应用」的卸载入口。
        public static void RegisterUninstallEntry(string target)
        {
            using (RegistryKey key = Registry.CurrentUser.CreateSubKey(LauncherPaths.UninstallRegKey))
            {
                if (key == null) { return; }
                key.SetValue("DisplayName", LauncherPaths.AppName);
                string version = ReadVersion(target);
                if (version.Length > 0) { key.SetValue("DisplayVersion", version); }
                key.SetValue("Publisher", LauncherPaths.AppName);
                key.SetValue("InstallLocation", target);
                key.SetValue("DisplayIcon", Path.Combine(target, LauncherPaths.AppExeName));
                key.SetValue("UninstallString", "\"" + Path.Combine(target, LauncherPaths.UninstallExeName) + "\"");
                key.SetValue("NoModify", 1, RegistryValueKind.DWord);
                key.SetValue("NoRepair", 1, RegistryValueKind.DWord);
            }
        }
    }
}
