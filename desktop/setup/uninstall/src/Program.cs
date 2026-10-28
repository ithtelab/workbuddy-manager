using System.Text;

namespace WorkBuddyUninstall;

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Any(a => a.Equals("--silent", StringComparison.OrdinalIgnoreCase)))
        {
            var log = Path.Combine(Path.GetTempPath(), "WorkBuddyUninstall.log");
            try
            {
                File.WriteAllText(log, "start " + Uninstaller.Root + Environment.NewLine, Encoding.UTF8);
                var kept = Uninstaller.Run(Uninstaller.Root, keepData: true, removeSettings: true,
                    new FileProgress(log));
                File.AppendAllText(log, "OK kept=" + (kept ?? "(none)") + Environment.NewLine, Encoding.UTF8);
                return 0;
            }
            catch (Exception ex)
            {
                File.AppendAllText(log, "ERR " + ex + Environment.NewLine, Encoding.UTF8);
                return 1;
            }
        }

        ApplicationConfiguration.Initialize();
        Application.Run(new UninstallForm());
        return 0;
    }

    private sealed class FileProgress : IProgress<(int Percent, string Message)>
    {
        private readonly string _log;
        private readonly object _gate = new();

        public FileProgress(string log) => _log = log;

        public void Report((int Percent, string Message) value)
        {
            lock (_gate)
            {
                File.AppendAllText(_log, $"{value.Percent}% {value.Message}{Environment.NewLine}", Encoding.UTF8);
            }
        }
    }
}
