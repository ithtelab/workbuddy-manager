using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

namespace WorkBuddyLauncher
{
    /// 安装清单：安装时把「这个程序放下了哪些文件」逐个记下来，卸载（与覆盖安装前的
    /// 清理）只删这些 —— 外加程序自己会创建的那几个目录。
    ///
    /// 为什么必须有（issue #178，真实数据丢失）：早先卸载是**递归删掉安装目录里的
    /// 一切**。用户在向导里选了 `D:\workspace`，程序就装在那个目录里；卸载时整个
    /// `D:\workspace` 被删光，用户自己的资料一并没了。
    ///
    /// 所以规则改成三条：
    ///   1. 没在清单里、也不属于程序自己目录/文件的东西，**一律不删**；
    ///   2. 卸装前先确认这个目录**确实是本程序的安装根**（有 server\main.py 或清单），
    ///      否则直接拒绝 —— 在别处误点卸载不会伤到任何文件；
    ///   3. 安装根目录本身不删，删完留一个可能为空的目录，用户自己决定要不要清掉。
    internal static class InstallManifest
    {
        /// 清单文件名（放在安装根目录里，纯文本、一行一个相对路径、可人工查看）。
        public const string FileName = "WorkBuddyManager.install.txt";

        /// 程序自己会创建/拥有的**顶层目录**（运行时产生的内容也落在这些里面）。
        public static readonly string[] OwnDirs =
        {
            "server", "web", "deploy", "windows", "docs", "upstream", "data", "logs",
            "log", "tmp", ".runtime", ".tools", ".venv",
        };

        /// 程序自己拥有的**顶层文件**（清单之外的兜底：日志、标记、启动器与卸载器本身）。
        public static readonly string[] OwnFiles =
        {
            ".env", ".env.example", ".version", "WorkBuddyManager.exe", "Uninstall.exe",
            "CHANGELOG.md", "README.md", "README.en.md", "LICENSE", "Dockerfile",
            "docker-compose.yml", FileName, "WorkBuddyManager.exe.old", "Uninstall.exe.old",
        };

        public static bool LooksLikeInstallRoot(string dir)
        {
            if (string.IsNullOrEmpty(dir) || !Directory.Exists(dir)) { return false; }
            return File.Exists(Path.Combine(dir, "server", "main.py"))
                || File.Exists(Path.Combine(dir, FileName));
        }

        /// 写入清单（安装完成后调用；覆盖安装会重写）。
        public static void Write(string installDir, IEnumerable<string> relativePaths)
        {
            var lines = new List<string>();
            foreach (string raw in relativePaths)
            {
                string rel = (raw ?? string.Empty).Replace('/', '\\').Trim().TrimStart('\\');
                if (rel.Length > 0 && !lines.Contains(rel)) { lines.Add(rel); }
            }
            lines.Sort(StringComparer.OrdinalIgnoreCase);
            string text = string.Join("\r\n", lines.ToArray()) + "\r\n";
            File.WriteAllText(Path.Combine(installDir, FileName), text, new UTF8Encoding(false));
        }

        /// 读清单（不存在或损坏时返回空集合 —— 那时只有 OwnDirs/OwnFiles 兜底）。
        public static HashSet<string> Read(string installDir)
        {
            var set = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            try
            {
                string path = Path.Combine(installDir, FileName);
                if (!File.Exists(path)) { return set; }
                foreach (string line in File.ReadAllLines(path))
                {
                    string rel = line.Replace('/', '\\').Trim().TrimStart('\\');
                    if (rel.Length > 0) { set.Add(rel); }
                }
            }
            catch { }
            return set;
        }

        /// 这个相对路径属于本程序吗（决定删不删）。
        public static bool IsOwned(string relative, HashSet<string> manifest)
        {
            string rel = (relative ?? string.Empty).Replace('/', '\\').TrimStart('\\');
            if (rel.Length == 0) { return false; }
            if (manifest != null && manifest.Contains(rel)) { return true; }

            string[] parts = rel.Split('\\');
            string top = parts[0];
            if (parts.Length == 1)
            {
                foreach (string name in OwnFiles)
                {
                    if (string.Equals(top, name, StringComparison.OrdinalIgnoreCase)) { return true; }
                }
                // 顶层日志（manager.out.log / server.err.log 这类）
                if (top.EndsWith(".log", StringComparison.OrdinalIgnoreCase)) { return true; }
                return false;
            }
            foreach (string name in OwnDirs)
            {
                if (string.Equals(top, name, StringComparison.OrdinalIgnoreCase)) { return true; }
            }
            return false;
        }
    }
}
