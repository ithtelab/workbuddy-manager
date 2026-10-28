using System.Diagnostics;
using System.Text;
using System.Text.Json;

namespace WorkBuddyDesktop;

internal static class AppPaths
{
    public static string LocalDir { get; } = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "WorkBuddyDesktop");

    public static string LogDir => Path.Combine(LocalDir, "logs");
    public static string UiDir => Path.Combine(LocalDir, "ui");
    public static string WebViewProfile => Path.Combine(LocalDir, "WebView2");
    public static string UpdateWebViewProfile => Path.Combine(LocalDir, "WebView2Update");
    public static string SettingsFile => Path.Combine(LocalDir, "settings.json");

    public static void EnsureCreated()
    {
        Directory.CreateDirectory(LocalDir);
        Directory.CreateDirectory(LogDir);
        Directory.CreateDirectory(UiDir);
    }
}

internal static class AppLog
{
    private static readonly object Gate = new();
    private static readonly string FilePath = Path.Combine(AppPaths.LogDir, "app.log");
    private static readonly string FallbackPath = Path.Combine(Path.GetTempPath(), "WorkBuddyDesktop-app.log");

    public static string Path_ => FilePath;

    public static void Info(string message) => Write("INFO", message, null);

    public static void Error(Exception? ex) => Write("ERROR", ex?.Message ?? "unknown error", ex);

    public static void Error(string message, Exception? ex = null) => Write("ERROR", message, ex);

    private static void Write(string level, string message, Exception? ex)
    {
        var line = new StringBuilder()
            .Append(DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff"))
            .Append(" [").Append(level).Append("] ")
            .Append(message);
        if (ex is not null)
        {
            line.AppendLine().Append(ex);
        }
        var text = line.ToString() + Environment.NewLine;

        if (TryAppend(FilePath, text)) return;
        // 主日志写不进去（被占用、权限、目录异常）时退到临时目录，至少留下证据
        TryAppend(FallbackPath, text);
    }

    private static bool TryAppend(string path, string text)
    {
        try
        {
            AppPaths.EnsureCreated();
            if (path == FilePath) RotateIfNeeded();
            lock (Gate)
            {
                File.AppendAllText(path, text, Encoding.UTF8);
            }
            return true;
        }
        catch
        {
            return false;
        }
    }

    private static void RotateIfNeeded()
    {
        var info = new FileInfo(FilePath);
        if (!info.Exists || info.Length < 5 * 1024 * 1024) return;
        var old = FilePath + ".1";
        try
        {
            if (File.Exists(old)) File.Delete(old);
            File.Move(FilePath, old);
        }
        catch
        {
        }
    }

    public static string Tail(int maxChars = 4000)
    {
        try
        {
            if (!File.Exists(FilePath)) return "";
            using var fs = new FileStream(FilePath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            var start = Math.Max(0, fs.Length - maxChars);
            fs.Seek(start, SeekOrigin.Begin);
            using var reader = new StreamReader(fs, Encoding.UTF8);
            return reader.ReadToEnd();
        }
        catch
        {
            return "";
        }
    }
}

internal static class ProcessUtil
{
    public static string NormalizePath(string path)
    {
        try
        {
            return Path.GetFullPath(path).TrimEnd('\\', '/').Replace('/', '\\').ToLowerInvariant();
        }
        catch
        {
            return path.TrimEnd('\\', '/').Replace('/', '\\').ToLowerInvariant();
        }
    }

    public static bool PathIsUnder(string candidate, string root)
    {
        var c = NormalizePath(candidate);
        var r = NormalizePath(root);
        return c.Equals(r, StringComparison.OrdinalIgnoreCase)
            || c.StartsWith(r + "\\", StringComparison.OrdinalIgnoreCase);
    }

    public static void OpenFolder(string path)
    {
        try
        {
            Process.Start(new ProcessStartInfo { FileName = path, UseShellExecute = true });
        }
        catch (Exception ex)
        {
            AppLog.Error(ex);
        }
    }

    public static void OpenUrl(string url)
    {
        try
        {
            if (!url.StartsWith("http://", StringComparison.OrdinalIgnoreCase)
                && !url.StartsWith("https://", StringComparison.OrdinalIgnoreCase)) return;
            Process.Start(new ProcessStartInfo { FileName = url, UseShellExecute = true });
        }
        catch (Exception ex)
        {
            AppLog.Error(ex);
        }
    }
}

internal sealed class AppSettings
{
    public string? ProjectRoot { get; set; }
    public int WindowX { get; set; } = -1;
    public int WindowY { get; set; } = -1;
    public int WindowWidth { get; set; } = 1280;
    public int WindowHeight { get; set; } = 840;
    public bool WindowMaximized { get; set; } = true;
    public bool CloseToTray { get; set; } = true;
    public bool TrayHintShown { get; set; }
    public bool AutoPromptUpdate { get; set; } = true;
    public string? LoginUser { get; set; }
    public string? LoginSecret { get; set; }
    public bool AutoLogin { get; set; } = true;
    public bool SilentStart { get; set; }
    public bool AutoStart { get; set; }

    public static AppSettings Load()
    {
        try
        {
            var path = AppPaths.SettingsFile;
            if (File.Exists(path))
            {
                var data = JsonSerializer.Deserialize<AppSettings>(File.ReadAllText(path));
                if (data is not null) return data;
            }
        }
        catch (Exception ex)
        {
            AppLog.Error(ex);
        }
        return new AppSettings();
    }

    public void Save()
    {
        try
        {
            AppPaths.EnsureCreated();
            File.WriteAllText(AppPaths.SettingsFile,
                JsonSerializer.Serialize(this, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch (Exception ex)
        {
            AppLog.Error(ex);
        }
    }
}

internal sealed class CredentialStore
{
    private readonly AppSettings _settings;

    public CredentialStore(AppSettings settings) => _settings = settings;

    public bool HasCredentials =>
        !string.IsNullOrWhiteSpace(_settings.LoginUser) && !string.IsNullOrWhiteSpace(_settings.LoginSecret);

    public (string User, string Password)? Load()
    {
        if (!HasCredentials) return null;
        try
        {
            var encrypted = Convert.FromBase64String(_settings.LoginSecret!);
            var plain = System.Security.Cryptography.ProtectedData.Unprotect(
                encrypted, null, System.Security.Cryptography.DataProtectionScope.CurrentUser);
            return (_settings.LoginUser!, System.Text.Encoding.UTF8.GetString(plain));
        }
        catch (Exception ex)
        {
            AppLog.Error("读取已保存的登录信息失败", ex);
            return null;
        }
    }

    public void Save(string user, string password)
    {
        try
        {
            var encrypted = System.Security.Cryptography.ProtectedData.Protect(
                System.Text.Encoding.UTF8.GetBytes(password), null,
                System.Security.Cryptography.DataProtectionScope.CurrentUser);
            _settings.LoginUser = user;
            _settings.LoginSecret = Convert.ToBase64String(encrypted);
            _settings.Save();
        }
        catch (Exception ex)
        {
            AppLog.Error("保存登录信息失败", ex);
        }
    }

    public void Clear()
    {
        _settings.LoginUser = null;
        _settings.LoginSecret = null;
        _settings.Save();
    }
}

internal static class AutoStartManager
{
    private const string RunKeyPath = @"Software\Microsoft\Windows\CurrentVersion\Run";
    private const string ValueName = "WorkBuddyManager";

    public static bool IsEnabled()
    {
        try
        {
            using var key = Microsoft.Win32.Registry.CurrentUser.OpenSubKey(RunKeyPath);
            return key?.GetValue(ValueName) is string value && value.Length > 0;
        }
        catch
        {
            return false;
        }
    }

    public static void Set(bool enabled, string exePath)
    {
        try
        {
            using var key = Microsoft.Win32.Registry.CurrentUser.CreateSubKey(RunKeyPath);
            if (enabled)
            {
                key.SetValue(ValueName, "\"" + exePath + "\"");
            }
            else
            {
                key.DeleteValue(ValueName, throwOnMissingValue: false);
            }
        }
        catch (Exception ex)
        {
            AppLog.Error("修改开机自启失败", ex);
        }
    }
}


internal static class DpiUtil
{
    public static float Scale
    {
        get
        {
            try
            {
                using var graphics = Graphics.FromHwnd(IntPtr.Zero);
                return graphics.DpiX / 96f;
            }
            catch
            {
                return 1f;
            }
        }
    }
}
