using System;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Windows.Forms;

namespace WorkBuddyLauncher
{
    /// 内嵌项目包（payload.zip）的解压与安装辅助。
    internal static class PayloadInstaller
    {
        /// 解压内嵌 payload 到目标目录（覆盖同名文件）。
        /// progress 回调参数：已完成条目数、总条目数、当前条目相对路径；可为 null。
        /// collect：可选，把解压出来的相对路径收集起来（安装器据此写安装清单）。
        public static void ExtractPayload(string targetDir, Action<int, int, string> progress)
        {
            ExtractPayload(targetDir, progress, null);
        }

        public static void ExtractPayload(string targetDir, Action<int, int, string> progress,
            System.Collections.Generic.List<string> collect)
        {
            Diag("ExtractPayload 开始");
            using (Stream stream = Assembly.GetExecutingAssembly()
                       .GetManifestResourceStream(LauncherPaths.PayloadResourceName))
            {
                if (stream == null)
                {
                    throw new InvalidOperationException("安装包缺少内嵌项目文件（payload.zip），请用构建脚本重新生成。");
                }
                using (var archive = new ZipArchive(stream, ZipArchiveMode.Read))
                {
                    int total = archive.Entries.Count;
                    Diag("条目总数=" + total);
                    int done = 0;
                    foreach (ZipArchiveEntry entry in archive.Entries)
                    {
                        // PowerShell 5.1 的 ZipFile 生成的条目名以 \ 分隔（.NET Framework 行为），
                        // 目录条目以 \ 结尾 —— 统一成 / 再判断，否则「xx\」会被当作文件名解压卡死
                        string fullName = entry.FullName.Replace('\\', '/');
                        string relative = fullName.Replace('/', Path.DirectorySeparatorChar);
                        string destination = Path.Combine(targetDir, relative);
                        if (fullName.EndsWith("/", StringComparison.Ordinal))
                        {
                            Directory.CreateDirectory(destination);
                        }
                        else
                        {
                            Directory.CreateDirectory(Path.GetDirectoryName(destination));
                            entry.ExtractToFile(destination, true);
                            if (collect != null) { collect.Add(relative); }
                        }
                        done++;
                        // 逐条回调会把 UI 刷爆：按批次节流，最后一条必报
                        if (progress != null && (done % 25 == 0 || done == total))
                        {
                            progress(done, total, relative);
                        }
                    }
                    Diag("循环结束 done=" + done);
                }
                Diag("zip 已释放");
            }
            Diag("ExtractPayload 返回");
        }

        /// 安装过程诊断日志（%TEMP%\wb-install-diag.log），用于排障。
        internal static void Diag(string message)
        {
            try
            {
                File.AppendAllText(Path.Combine(Path.GetTempPath(), "wb-install-diag.log"),
                    DateTime.Now.ToString("HH:mm:ss.fff") + " " + message + Environment.NewLine);
            }
            catch { }
        }

        /// 把自身复制成安装目录里的主程序与卸载程序。
        public static void CopySelf(string installDir)
        {
            string self = Application.ExecutablePath;
            File.Copy(self, Path.Combine(installDir, LauncherPaths.AppExeName), true);
            File.Copy(self, Path.Combine(installDir, LauncherPaths.UninstallExeName), true);
        }

        /// 创建快捷方式（WScript.Shell COM）。
        public static void CreateShortcut(string lnkPath, string targetPath, string workingDir, string description)
        {
            Type shellType = Type.GetTypeFromProgID("WScript.Shell");
            if (shellType == null)
            {
                throw new InvalidOperationException("系统缺少 WScript.Shell，无法创建快捷方式。");
            }
            dynamic shell = Activator.CreateInstance(shellType);
            dynamic shortcut = shell.CreateShortcut(lnkPath);
            shortcut.TargetPath = targetPath;
            shortcut.WorkingDirectory = workingDir;
            shortcut.IconLocation = targetPath + ",0";
            shortcut.Description = description;
            shortcut.Save();
        }

        /// 读取已有快捷方式指向的目标路径；文件不存在或读不到时返回 null。
        public static string ReadShortcutTarget(string lnkPath)
        {
            if (!File.Exists(lnkPath)) { return null; }
            try
            {
                Type shellType = Type.GetTypeFromProgID("WScript.Shell");
                if (shellType == null) { return null; }
                dynamic shell = Activator.CreateInstance(shellType);
                dynamic shortcut = shell.CreateShortcut(lnkPath);
                string target = shortcut.TargetPath as string;
                return string.IsNullOrEmpty(target) ? null : target;
            }
            catch { return null; }
        }

        /// 两个路径是否指向同一文件（规范化后按不区分大小写比较）。
        private static bool SamePath(string a, string b)
        {
            if (string.IsNullOrEmpty(a) || string.IsNullOrEmpty(b)) { return false; }
            try
            {
                return string.Equals(Path.GetFullPath(a), Path.GetFullPath(b),
                    StringComparison.OrdinalIgnoreCase);
            }
            catch
            {
                return string.Equals(a, b, StringComparison.OrdinalIgnoreCase);
            }
        }

        /// 目录下是否已存在**任意文件名**、但指向 targetExe 的快捷方式。
        ///
        /// 用户可能把桌面快捷方式改过名：这时按固定路径找不到它，重装再建一个
        /// 就会出现两个图标。所以创建前按「指向」再找一遍。
        public static bool ShortcutExistsFor(string directory, string targetExe)
        {
            try
            {
                foreach (string lnk in Directory.GetFiles(directory, "*.lnk"))
                {
                    if (SamePath(ReadShortcutTarget(lnk), targetExe)) { return true; }
                }
            }
            catch { }
            return false;
        }

        /// 确保快捷方式存在且指向正确 —— 已经对了就一个字都不写。
        ///
        /// 为什么不能无脑重建：覆盖安装时重写同名 .lnk 会让 Windows 丢掉用户
        /// 摆好的桌面图标位置（图标坐标与 lnk 文件关联），装完就「跳位置」。
        public static void EnsureShortcut(string lnkPath, string targetPath, string workingDir, string description)
        {
            string current = ReadShortcutTarget(lnkPath);
            if (SamePath(current, targetPath)) { return; }
            if (current == null)
            {
                // 目标名的 lnk 不存在，但同目录已有别的快捷方式指向它（用户改过名）
                string dir = Path.GetDirectoryName(lnkPath);
                if (!string.IsNullOrEmpty(dir) && ShortcutExistsFor(dir, targetPath)) { return; }
            }
            CreateShortcut(lnkPath, targetPath, workingDir, description);
        }
    }
}
