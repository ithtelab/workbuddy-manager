using System.Threading;

namespace WorkBuddyDesktop;

internal static class Program
{
    private const string MutexName = @"Local\WorkBuddyManagerDesktop";
    private const string ShowEventName = @"Local\WorkBuddyManagerDesktop.Show";

    [STAThread]
    private static void Main(string[] args)
    {
        AppPaths.EnsureCreated();

        string? rootArg = null;
        var autoUpdate = false;
        var showUpdate = false;
        var showTrayPopup = false;
        for (var i = 0; i < args.Length; i++)
        {
            if (args[i].StartsWith("--root=", StringComparison.OrdinalIgnoreCase))
            {
                rootArg = args[i]["--root=".Length..];
            }
            else if (string.Equals(args[i], "--root", StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
            {
                rootArg = args[++i];
            }
            else if (string.Equals(args[i], "--update-now", StringComparison.OrdinalIgnoreCase))
            {
                autoUpdate = true;
            }
            else if (string.Equals(args[i], "--show-update", StringComparison.OrdinalIgnoreCase))
            {
                showUpdate = true;
            }
            else if (string.Equals(args[i], "--tray-popup", StringComparison.OrdinalIgnoreCase))
            {
                showTrayPopup = true;
            }
        }

        using var mutex = new Mutex(true, MutexName, out var createdNew);
        if (!createdNew)
        {
            try
            {
                using var showEvent = EventWaitHandle.OpenExisting(ShowEventName);
                showEvent.Set();
            }
            catch
            {
            }
            return;
        }

        ApplicationConfiguration.Initialize();
        Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException);
        Application.ThreadException += (_, e) => AppLog.Error("未处理的界面异常", e.Exception);
        AppDomain.CurrentDomain.UnhandledException += (_, e) =>
            AppLog.Error("未处理的异常", e.ExceptionObject as Exception);

        var settings = AppSettings.Load();

        string? root = null;
        if (!string.IsNullOrWhiteSpace(rootArg))
        {
            root = RootLocator.Find(rootArg);
            if (root is null)
            {
                MessageBox.Show($"--root 指定的目录不是 WorkBuddy Manager 目录：\n{rootArg}",
                    "WorkBuddy Manager", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }
        }

        root ??= RootLocator.Find(AppContext.BaseDirectory);
        if (root is null && settings.ProjectRoot is not null)
        {
            root = RootLocator.Find(settings.ProjectRoot);
        }

        while (root is null)
        {
            using var dialog = new FolderBrowserDialog
            {
                Description = "请选择 WorkBuddy Manager 项目目录（包含 server\\main.py）",
                UseDescriptionForTitle = true,
            };
            if (dialog.ShowDialog() != DialogResult.OK) return;

            var picked = RootLocator.Find(dialog.SelectedPath);
            if (picked is null)
            {
                MessageBox.Show("所选目录不是有效的 WorkBuddy Manager 目录（找不到 server\\main.py）。",
                    "WorkBuddy Manager", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                continue;
            }
            root = picked;
            settings.ProjectRoot = picked;
            settings.Save();
        }

        AppLog.Info($"启动：root={root}, version={RootLocator.ReadVersion(root)}");

        using var showHandle = new EventWaitHandle(false, EventResetMode.AutoReset, ShowEventName);
        using var form = new MainForm(root, settings, showHandle, autoUpdate, showUpdate, showTrayPopup);
        Application.Run(form);
        Environment.ExitCode = form.ExitCode;
    }
}
