using System.Text;

namespace WorkBuddySetup;

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Any(a => a.Equals("--silent", StringComparison.OrdinalIgnoreCase)))
        {
            return RunSilent(args);
        }

        SetupForm.DemoDone = args.Any(a => a.Equals("--demo-done", StringComparison.OrdinalIgnoreCase));
        ApplicationConfiguration.Initialize();
        Application.Run(new SetupForm());
        return 0;
    }

    private static int RunSilent(string[] args)
    {
        var dir = GetArg(args, "--dir") ?? Installer.DefaultDir;
        var log = Path.Combine(Path.GetTempPath(), "WorkBuddySetup.log");
        try
        {
            File.WriteAllText(log, "start " + dir + Environment.NewLine, Encoding.UTF8);
            Installer.Install(dir, new FileProgress(log));
            File.AppendAllText(log, "OK" + Environment.NewLine, Encoding.UTF8);
            if (args.Any(a => a.Equals("--launch", StringComparison.OrdinalIgnoreCase)))
            {
                Installer.Launch(dir);
            }
            return 0;
        }
        catch (Exception ex)
        {
            File.AppendAllText(log, "ERR " + ex + Environment.NewLine, Encoding.UTF8);
            return 1;
        }
    }

    private static string? GetArg(string[] args, string name)
    {
        for (var i = 0; i < args.Length; i++)
        {
            if (args[i].StartsWith(name + "=", StringComparison.OrdinalIgnoreCase))
            {
                return args[i][(name.Length + 1)..];
            }
            if (string.Equals(args[i], name, StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
            {
                return args[i + 1];
            }
        }
        return null;
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
