using System.Diagnostics;
using System.Text;
using Microsoft.Win32;

namespace WorkBuddyUninstall;

internal static class Uninstaller
{
    public static string Root => Path.GetDirectoryName(Environment.ProcessPath ?? Application.ExecutablePath)!;

    public static bool IsValidInstall(string root) =>
        File.Exists(Path.Combine(root, "WorkBuddyManager.exe")) &&
        File.Exists(Path.Combine(root, "server", "main.py"));

    /// <summary>只删除本程序自己的东西：安装目录、快捷方式、注册表项、设置目录。</summary>
    public static string? Run(string root, bool keepData, bool removeSettings,
        IProgress<(int Percent, string Message)> progress)
    {
        var full = Path.GetFullPath(root).TrimEnd('\\', '/');
        if (!IsValidInstall(full))
        {
            throw new InvalidOperationException("当前目录不是 WorkBuddy Manager 的安装目录，已中止卸载。");
        }
        if (full.Length <= 3)
        {
            throw new InvalidOperationException("安装目录路径异常，已中止卸载。");
        }

        progress.Report((8, "正在停止程序…"));
        StopProcesses(full);

        progress.Report((24, "正在删除快捷方式…"));
        RemoveShortcuts(full);

        progress.Report((36, "正在清理注册表…"));
        RemoveRegistry();

        if (removeSettings)
        {
            progress.Report((50, "正在清理设置与登录缓存…"));
            TryDeleteDirectory(Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "WorkBuddyDesktop"));
        }

        string? kept = null;
        if (keepData)
        {
            var data = Path.Combine(full, "data");
            if (Directory.Exists(data))
            {
                var destination = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "WorkBuddyManager", "uninstalled-data-" + DateTime.Now.ToString("yyyyMMdd-HHmmss"));
                Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
                CopyDirectory(data, destination);
                Directory.Delete(data, recursive: true);
                kept = destination;
            }
        }

        progress.Report((70, "正在删除程序文件…"));
        ScheduleDelete(full);
        progress.Report((100, kept is null ? "卸载完成" : "卸载完成，账号与配置已保留"));
        return kept;
    }

    private static void CopyDirectory(string source, string destination)
    {
        Directory.CreateDirectory(destination);
        foreach (var file in Directory.GetFiles(source))
        {
            File.Copy(file, Path.Combine(destination, Path.GetFileName(file)), overwrite: true);
        }
        foreach (var directory in Directory.GetDirectories(source))
        {
            CopyDirectory(directory, Path.Combine(destination, Path.GetFileName(directory)));
        }
    }

    private static void StopProcesses(string root)
    {
        foreach (var process in Process.GetProcesses())
        {
            if (process.Id == Environment.ProcessId) continue;
            try
            {
                var path = process.MainModule?.FileName;
                if (path is null || !path.StartsWith(root, StringComparison.OrdinalIgnoreCase)) continue;
                process.Kill(entireProcessTree: true);
                process.WaitForExit(3000);
            }
            catch
            {
                // 没有权限或进程已退出：忽略，不阻断卸载
            }
            finally
            {
                process.Dispose();
            }
        }
    }

    private static void RemoveShortcuts(string root)
    {
        var targets = new[]
        {
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), "WorkBuddy Manager.lnk"),
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                "Microsoft", "Windows", "Start Menu", "Programs", "WorkBuddy Manager.lnk"),
        };

        foreach (var shortcut in targets)
        {
            if (!File.Exists(shortcut)) continue;
            try
            {
                var shellType = Type.GetTypeFromProgID("WScript.Shell");
                if (shellType is null) continue;
                dynamic shell = Activator.CreateInstance(shellType)!;
                dynamic link = shell.CreateShortcut(shortcut);
                var target = (string)link.TargetPath;
                // 只删指向本安装目录的快捷方式，别的同名片不碰
                if (target.StartsWith(root, StringComparison.OrdinalIgnoreCase))
                {
                    File.Delete(shortcut);
                }
            }
            catch
            {
                // 读不到目标就不删，宁可留一个快捷方式也不误删
            }
        }
    }

    private static void RemoveRegistry()
    {
        try
        {
            Registry.CurrentUser.DeleteSubKeyTree(
                @"Software\Microsoft\Windows\CurrentVersion\Uninstall\WorkBuddyManager", throwOnMissingSubKey: false);
        }
        catch
        {
        }
        try
        {
            using var run = Registry.CurrentUser.OpenSubKey(
                @"Software\Microsoft\Windows\CurrentVersion\Run", writable: true);
            run?.DeleteValue("WorkBuddyManager", throwOnMissingValue: false);
        }
        catch
        {
        }
    }

    private static void TryDeleteDirectory(string path)
    {
        try
        {
            if (Directory.Exists(path)) Directory.Delete(path, recursive: true);
        }
        catch
        {
        }
    }

    /// <summary>
    /// 卸载程序自己就在被删的目录里，直接删会失败；写一个批处理，等本进程退出后再删目录。
    /// 只删传进来的这一个目录（且前面已校验过它确实是安装目录）。
    /// </summary>
    private static void ScheduleDelete(string root)
    {
        try
        {
            var batch = Path.Combine(Path.GetTempPath(), "wb-uninstall-" + Guid.NewGuid().ToString("N") + ".bat");
            var content = string.Join("\r\n", new[]
            {
                "@echo off",
                "ping -n 3 127.0.0.1 >nul",
                "rmdir /s /q \"" + root + "\"",
                "del \"%~f0\"",
                "",
            });
            File.WriteAllText(batch, content, Encoding.Default);
            Process.Start(new ProcessStartInfo
            {
                FileName = "cmd.exe",
                Arguments = "/c \"" + batch + "\"",
                CreateNoWindow = true,
                UseShellExecute = false,
                WindowStyle = ProcessWindowStyle.Hidden,
            });
        }
        catch
        {
        }
    }
}
