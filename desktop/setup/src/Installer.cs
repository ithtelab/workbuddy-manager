using System.Diagnostics;
using System.IO.Compression;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Win32;

namespace WorkBuddySetup;

internal static class Installer
{
    public const string DefaultUser = "admin";
    public const string DefaultPassword = "admin123";

    public static string DefaultDir => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "Programs", "WorkBuddyManager");

    public static void Install(string targetDir, IProgress<(int Percent, string Message)> progress)
    {
        var root = Path.GetFullPath(targetDir.TrimEnd('\\', '/'));
        Directory.CreateDirectory(root);

        if (IsAppRunning(root))
        {
            throw new InvalidOperationException("目标目录里的 WorkBuddy Manager 正在运行，请先退出后再安装。");
        }

        using var payload = Payload.Open()
            ?? throw new InvalidOperationException("安装包数据不完整（缺少内嵌文件），请重新下载安装包。");
        using var zip = new ZipArchive(payload, ZipArchiveMode.Read);

        var files = zip.Entries.Where(e => !string.IsNullOrEmpty(e.Name)).ToList();
        var total = files.Sum(e => Math.Max(0, e.Length));
        long done = 0;
        var rootFull = root.TrimEnd('\\') + "\\";

        foreach (var entry in files)
        {
            var name = entry.FullName.Replace('\\', '/');
            if (name.Contains("..") || name.StartsWith('/') || name.Contains(':'))
            {
                throw new InvalidOperationException("安装包内包含非法路径：" + name);
            }

            var destination = Path.GetFullPath(Path.Combine(root, name));
            if (!destination.StartsWith(rootFull, StringComparison.OrdinalIgnoreCase))
            {
                throw new InvalidOperationException("安装包内路径越界：" + name);
            }

            Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
            entry.ExtractToFile(destination, overwrite: true);
            done += Math.Max(0, entry.Length);
            if (total > 0)
            {
                var percent = (int)Math.Min(90, done * 90 / total);
                progress.Report((percent, $"正在解压文件… {percent}%"));
            }
        }

        progress.Report((92, "正在写入配置…"));
        EnsureDirectories(root);

        var envPath = Path.Combine(root, ".env");
        var upstreamConfig = Path.Combine(root, "upstream", "config.json");
        var usersPath = Path.Combine(root, "data", "users.json");

        var apiKey = ReadExistingApiKey(envPath) ?? NewApiKey();
        var freshInstall = !File.Exists(usersPath);

        if (freshInstall)
        {
            WriteUsers(root);
        }
        if (!File.Exists(envPath))
        {
            WriteEnv(root, apiKey);
        }
        if (!File.Exists(upstreamConfig))
        {
            WriteUpstreamConfig(root, apiKey);
        }
        if (freshInstall)
        {
            WriteFirstRunLogin(root);
        }

        progress.Report((96, "正在创建快捷方式…"));
        CreateShortcuts(root);
        WriteUninstallRegistry(root);

        progress.Report((100, "安装完成"));
    }

    public static void Launch(string root)
    {
        var exe = Path.Combine(root, "WorkBuddyManager.exe");
        if (!File.Exists(exe)) return;
        Process.Start(new ProcessStartInfo
        {
            FileName = exe,
            WorkingDirectory = root,
            UseShellExecute = true,
        });
    }

    private static bool IsAppRunning(string root)
    {
        try
        {
            foreach (var process in Process.GetProcessesByName("WorkBuddyManager"))
            {
                try
                {
                    var path = process.MainModule?.FileName;
                    if (path is not null && path.StartsWith(root, StringComparison.OrdinalIgnoreCase))
                    {
                        return true;
                    }
                }
                catch
                {
                }
            }
        }
        catch
        {
        }
        return false;
    }

    private static void EnsureDirectories(string root)
    {
        foreach (var relative in new[] { "data", "upstream", "upstream\\auths", "upstream\\data" })
        {
            Directory.CreateDirectory(Path.Combine(root, relative));
        }
    }

    private static string NewApiKey() => "wbk_" + Convert.ToHexString(RandomNumberGenerator.GetBytes(16)).ToLowerInvariant();

    private static string? ReadExistingApiKey(string envPath)
    {
        try
        {
            if (!File.Exists(envPath)) return null;
            foreach (var line in File.ReadAllLines(envPath))
            {
                var trimmed = line.Trim();
                if (!trimmed.StartsWith("WB2API_KEY=", StringComparison.OrdinalIgnoreCase)) continue;
                var value = trimmed["WB2API_KEY=".Length..].Trim();
                if (value.Length > 0) return value;
            }
        }
        catch
        {
        }
        return null;
    }

    private static string ToForwardSlashes(string path) => path.Replace('\\', '/').TrimEnd('/');

    private static void WriteEnv(string root, string apiKey)
    {
        var p = ToForwardSlashes(root);
        var lines = new[]
        {
            "# WorkBuddy Manager —— Windows 本地部署（安装程序生成）",
            "WB_MANAGER_HOST=127.0.0.1",
            "WB_MANAGER_PORT=7864",
            "",
            "WB2API_BASE=http://127.0.0.1:7863",
            "WB2API_MODE=native",
            "WB2API_KEY=" + apiKey,
            "",
            "WB_UPSTREAM_DIR=" + p + "/upstream",
            "WB_AUTH_DIR=" + p + "/upstream/auths",
            "WB_UPSTREAM_CONFIG=" + p + "/upstream/config.json",
            "WB2API_START_SCRIPT=" + p + "/upstream/start-workbuddy2api.cmd",
            "WB2API_STOP_SCRIPT=" + p + "/upstream/stop-workbuddy2api.cmd",
            "WB2API_LOG_FILE=" + p + "/upstream/data/server.err.log",
            "",
            "WB_DATA_DIR=" + p + "/data",
            "WB_DB=" + p + "/data/manager.db",
            "WB_USERS_FILE=" + p + "/data/users.json",
            "WB_STATIC_DIR=" + p + "/web/out",
            "",
            "WB_SECURE_COOKIE=false",
            "",
        };
        File.WriteAllText(Path.Combine(root, ".env"), string.Join("\r\n", lines), new UTF8Encoding(false));
    }

    private static void WriteUpstreamConfig(string root, string apiKey)
    {
        var example = Path.Combine(root, "upstream", "config.example.json");
        JsonObject config;
        if (File.Exists(example))
        {
            config = JsonNode.Parse(File.ReadAllText(example, Encoding.UTF8))?.AsObject() ?? new JsonObject();
        }
        else
        {
            config = new JsonObject();
        }
        config["listen"] = ":7863";
        config["api_key"] = apiKey;

        var json = config.ToJsonString(new JsonSerializerOptions { WriteIndented = true });
        File.WriteAllText(Path.Combine(root, "upstream", "config.json"), json, new UTF8Encoding(false));
    }

    private static void WriteUsers(string root)
    {
        var salt = RandomNumberGenerator.GetBytes(16);
        var digest = Rfc2898DeriveBytes.Pbkdf2(
            Encoding.UTF8.GetBytes(DefaultPassword), salt, 260000, HashAlgorithmName.SHA256, 32);
        var pwdHash = "pbkdf2_sha256$260000$" + Convert.ToHexString(salt).ToLowerInvariant()
                      + "$" + Convert.ToHexString(digest).ToLowerInvariant();

        var secret = Convert.ToBase64String(RandomNumberGenerator.GetBytes(48))
            .TrimEnd('=').Replace('+', '-').Replace('/', '_');

        var data = new
        {
            secret,
            users = new[]
            {
                new { username = DefaultUser, role = "admin", pwd_hash = pwdHash },
            },
            api_keys = Array.Empty<object>(),
        };

        var json = JsonSerializer.Serialize(data, new JsonSerializerOptions { WriteIndented = true });
        Directory.CreateDirectory(Path.Combine(root, "data"));
        File.WriteAllText(Path.Combine(root, "data", "users.json"), json, new UTF8Encoding(false));
    }

    private static void WriteFirstRunLogin(string root)
    {
        var data = new { username = DefaultUser, password = DefaultPassword };
        var json = JsonSerializer.Serialize(data, new JsonSerializerOptions { WriteIndented = true });
        File.WriteAllText(Path.Combine(root, "data", "desktop-firstrun.json"), json, new UTF8Encoding(false));
    }

    private static void WriteUninstallRegistry(string root)
    {
        try
        {
            var exe = Path.Combine(root, "WorkBuddyManager.exe");
            var uninstall = Path.Combine(root, "Uninstall.exe");
            using var key = Registry.CurrentUser.CreateSubKey(
                @"Software\Microsoft\Windows\CurrentVersion\Uninstall\WorkBuddyManager");
            if (key is null) return;
            key.SetValue("DisplayName", "WorkBuddy Manager");
            key.SetValue("DisplayVersion", ReadVersion(root));
            key.SetValue("Publisher", "WorkBuddy");
            key.SetValue("InstallLocation", root);
            key.SetValue("DisplayIcon", exe);
            key.SetValue("UninstallString", "\"" + uninstall + "\"");
            key.SetValue("QuietUninstallString", "\"" + uninstall + "\" --silent");
            key.SetValue("NoModify", 1, RegistryValueKind.DWord);
            key.SetValue("NoRepair", 1, RegistryValueKind.DWord);
            key.SetValue("EstimatedSize", EstimateSizeKb(root), RegistryValueKind.DWord);
        }
        catch
        {
        }
    }

    private static string ReadVersion(string root)
    {
        try
        {
            var text = File.ReadAllText(Path.Combine(root, ".version")).Trim();
            return text.TrimStart('v', 'V');
        }
        catch
        {
            return "1.0.0";
        }
    }

    private static int EstimateSizeKb(string root)
    {
        try
        {
            long total = 0;
            foreach (var file in new DirectoryInfo(root).EnumerateFiles("*", SearchOption.AllDirectories))
            {
                total += file.Length;
            }
            return (int)Math.Min(int.MaxValue, total / 1024);
        }
        catch
        {
            return 0;
        }
    }

    private static void CreateShortcuts(string root)
    {
        var exe = Path.Combine(root, "WorkBuddyManager.exe");
        if (!File.Exists(exe)) return;

        var desktop = Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
        var programs = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
            "Microsoft", "Windows", "Start Menu", "Programs");

        CreateShortcut(Path.Combine(desktop, "WorkBuddy Manager.lnk"), exe, root);
        Directory.CreateDirectory(programs);
        CreateShortcut(Path.Combine(programs, "WorkBuddy Manager.lnk"), exe, root);
    }

    private static void CreateShortcut(string path, string exe, string workingDirectory)
    {
        try
        {
            var shellType = Type.GetTypeFromProgID("WScript.Shell");
            if (shellType is null) return;
            dynamic shell = Activator.CreateInstance(shellType)!;
            dynamic link = shell.CreateShortcut(path);
            link.TargetPath = exe;
            link.WorkingDirectory = workingDirectory;
            link.IconLocation = exe;
            link.Description = "WorkBuddy Manager";
            link.Save();
        }
        catch
        {
            // 快捷方式失败不影响安装
        }
    }
}
