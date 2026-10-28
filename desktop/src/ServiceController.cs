using System.Diagnostics;
using System.Management;
using System.Net;
using System.Net.Sockets;
using System.Text;

namespace WorkBuddyDesktop;

public enum ServiceState
{
    Starting,
    Ready,
    External,
    Error,
}

public sealed class ServiceSnapshot
{
    public ServiceState State { get; set; } = ServiceState.Starting;
    public string Message { get; set; } = "";
    public bool Owned { get; set; }
    public string? InstanceRoot { get; set; }
    public int Port { get; set; }
}

internal sealed record ProcessInfo(int Pid, string? Name, string? ExecutablePath, string? CommandLine);

internal static class WmiProcesses
{
    private const string Wql =
        "SELECT ProcessId, Name, ExecutablePath, CommandLine FROM Win32_Process " +
        "WHERE Name = 'python.exe' OR Name = 'pythonw.exe' OR Name = 'wb2api.exe'";

    public static List<ProcessInfo> Query()
    {
        var result = new List<ProcessInfo>();
        try
        {
            using var searcher = new ManagementObjectSearcher(Wql);
            using var collection = searcher.Get();
            foreach (ManagementObject item in collection)
            {
                try
                {
                    var pid = Convert.ToInt32(item["ProcessId"]);
                    result.Add(new ProcessInfo(
                        pid,
                        item["Name"] as string,
                        item["ExecutablePath"] as string,
                        item["CommandLine"] as string));
                }
                finally
                {
                    item.Dispose();
                }
            }
        }
        catch (Exception ex)
        {
            AppLog.Error("WMI 查询失败", ex);
        }
        return result;
    }
}

internal sealed class ServiceController
{
    private static readonly HttpClient ProbeClient = new(new SocketsHttpHandler
    {
        UseProxy = false,
        ConnectTimeout = TimeSpan.FromMilliseconds(800),
    })
    {
        Timeout = TimeSpan.FromSeconds(2),
    };

    private readonly string _root;
    private readonly EnvFile _env;
    private readonly object _gate = new();
    private Process? _manager;
    private Process? _upstream;

    public ServiceController(string root, EnvFile env)
    {
        _root = root;
        _env = env;
        Host = env.Get("WB_MANAGER_HOST") ?? "127.0.0.1";
        Port = env.GetInt("WB_MANAGER_PORT", 7864);
        ConnectHost = NormalizeConnectHost(Host);
    }

    public string Host { get; }
    public int Port { get; }
    public string ConnectHost { get; }

    public string BaseUrl => $"http://{ConnectHost}:{Port}";

    public Process? ManagerProcess { get { lock (_gate) return _manager; } }

    private static string NormalizeConnectHost(string host)
    {
        var h = host.Trim();
        if (h is "0.0.0.0" or "*" or "::" or "[::]") return "127.0.0.1";
        if (h == "::1") return "[::1]";
        return h;
    }

    public string PythonPath
    {
        get
        {
            var runtime = Path.Combine(_root, ".runtime", "python", "python.exe");
            if (File.Exists(runtime)) return runtime;
            return Path.Combine(_root, ".venv", "Scripts", "python.exe");
        }
    }

    public string UpstreamExe => Path.Combine(_root, "upstream", "wb2api.exe");
    public string UpstreamDir => Path.Combine(_root, "upstream");
    public string UpstreamPidFile => Path.Combine(UpstreamDir, "wb2api.pid");

    // ── 健康检查 ────────────────────────────────────────────

    public async Task<bool> IsHealthyAsync(CancellationToken ct = default)
    {
        try
        {
            using var response = await ProbeClient.GetAsync($"{BaseUrl}/api/healthz", ct).ConfigureAwait(false);
            if (!response.IsSuccessStatusCode) return false;
            var body = await response.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
            return body.Contains("workbuddy-manager", StringComparison.Ordinal);
        }
        catch
        {
            return false;
        }
    }

    public static async Task<bool> IsPortOpenAsync(string host, int port, int timeoutMs = 400)
    {
        try
        {
            using var client = new TcpClient();
            using var cts = new CancellationTokenSource(timeoutMs);
            await client.ConnectAsync(host, port, cts.Token).ConfigureAwait(false);
            return true;
        }
        catch
        {
            return false;
        }
    }

    // ── 实例发现 ────────────────────────────────────────────

    public List<ProcessInfo> FindManagerInstances()
    {
        var portTag = "--port " + Port;
        var portTagEq = "--port=" + Port;
        return WmiProcesses.Query()
            .Where(p => p.CommandLine is not null)
            .Where(p => p.CommandLine!.Contains("server.main:app", StringComparison.OrdinalIgnoreCase))
            .Where(p => p.CommandLine!.Contains(portTag, StringComparison.OrdinalIgnoreCase)
                        || p.CommandLine!.Contains(portTagEq, StringComparison.OrdinalIgnoreCase))
            .ToList();
    }

    public bool InstanceBelongsToRoot(ProcessInfo info)
    {
        if (info.ExecutablePath is not null && ProcessUtil.PathIsUnder(info.ExecutablePath, _root))
        {
            return true;
        }
        return info.CommandLine is not null && CommandLineMentionsRoot(info.CommandLine);
    }

    private bool CommandLineMentionsRoot(string commandLine)
    {
        var normalized = commandLine.Replace('/', '\\').ToLowerInvariant();
        var root = ProcessUtil.NormalizePath(_root);
        return normalized.Contains(root, StringComparison.OrdinalIgnoreCase);
    }

    public string? DescribeInstance(ProcessInfo info)
    {
        if (!string.IsNullOrWhiteSpace(info.ExecutablePath)) return info.ExecutablePath;
        if (!string.IsNullOrWhiteSpace(info.CommandLine))
        {
            var text = info.CommandLine!.Trim();
            return text.Length <= 160 ? text : text[..160] + "…";
        }
        return null;
    }

    // ── 启动 ────────────────────────────────────────────────

    public async Task<ServiceSnapshot> EnsureStartedAsync(IProgress<string> progress, CancellationToken ct)
    {
        progress.Report("正在检查本地服务…");

        if (await IsHealthyAsync(ct).ConfigureAwait(false))
        {
            return SnapshotWhenAlreadyRunning();
        }

        if (await IsPortOpenAsync(ConnectHost, Port).ConfigureAwait(false))
        {
            progress.Report("端口已监听，等待服务就绪…");
            var deadline = DateTime.UtcNow.AddSeconds(15);
            while (DateTime.UtcNow < deadline)
            {
                ct.ThrowIfCancellationRequested();
                if (await IsHealthyAsync(ct).ConfigureAwait(false))
                {
                    return SnapshotWhenAlreadyRunning();
                }
                await Task.Delay(400, ct).ConfigureAwait(false);
            }
            return new ServiceSnapshot
            {
                State = ServiceState.Error,
                Port = Port,
                Message = $"端口 {Port} 已被占用，但 /api/healthz 检查未通过。可能端口被其他程序占用，或服务启动失败。",
            };
        }

        if (!File.Exists(PythonPath))
        {
            return new ServiceSnapshot
            {
                State = ServiceState.Error,
                Port = Port,
                Message = $"未找到 Python 运行环境：{PythonPath}\n请先运行一次 windows\\prepare.ps1。",
            };
        }

        progress.Report("正在启动上游网关…");
        StartUpstream();

        progress.Report("正在启动管理面板…");
        StartManager();

        var startDeadline = DateTime.UtcNow.AddSeconds(45);
        while (DateTime.UtcNow < startDeadline)
        {
            ct.ThrowIfCancellationRequested();
            if (await IsHealthyAsync(ct).ConfigureAwait(false))
            {
                return new ServiceSnapshot
                {
                    State = ServiceState.Ready,
                    Owned = true,
                    Port = Port,
                    InstanceRoot = _root,
                    Message = "服务已就绪",
                };
            }

            Process? manager;
            lock (_gate) manager = _manager;
            if (manager is { HasExited: true })
            {
                var tail = ReadTail(Path.Combine(_root, "data", "manager.err.log"), 3000);
                return new ServiceSnapshot
                {
                    State = ServiceState.Error,
                    Port = Port,
                    Message = $"管理面板进程已退出（代码 {manager.ExitCode}）。\n{tail}",
                };
            }

            progress.Report("正在等待服务就绪…");
            await Task.Delay(400, ct).ConfigureAwait(false);
        }

        return new ServiceSnapshot
        {
            State = ServiceState.Error,
            Port = Port,
            Message = "启动超时（45 秒）。请检查 data\\manager.err.log 与 data\\manager.out.log。",
        };
    }

    private ServiceSnapshot SnapshotWhenAlreadyRunning()
    {
        var instances = FindManagerInstances();
        var ours = instances.FirstOrDefault(InstanceBelongsToRoot);
        if (ours is not null || instances.Count == 0)
        {
            return new ServiceSnapshot
            {
                State = ServiceState.Ready,
                Owned = ours is not null || instances.Count == 0,
                Port = Port,
                InstanceRoot = ours is not null ? _root : null,
                Message = "服务已在运行",
            };
        }

        var other = instances[0];
        return new ServiceSnapshot
        {
            State = ServiceState.External,
            Owned = false,
            Port = Port,
            InstanceRoot = DescribeInstance(other),
            Message = "当前端口上运行的是另一个目录的实例",
        };
    }

    private void StartManager()
    {
        lock (_gate)
        {
            if (_manager is { HasExited: false }) return;

            var outLog = Path.Combine(_root, "data", "manager.out.log");
            var errLog = Path.Combine(_root, "data", "manager.err.log");
            Directory.CreateDirectory(Path.GetDirectoryName(outLog)!);

            var psi = new ProcessStartInfo
            {
                FileName = PythonPath,
                WorkingDirectory = _root,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8,
            };
            psi.ArgumentList.Add("-m");
            psi.ArgumentList.Add("uvicorn");
            psi.ArgumentList.Add("server.main:app");
            psi.ArgumentList.Add("--env-file");
            psi.ArgumentList.Add(".env");
            psi.ArgumentList.Add("--host");
            psi.ArgumentList.Add(Host);
            psi.ArgumentList.Add("--port");
            psi.ArgumentList.Add(Port.ToString());
            psi.Environment["PYTHONUTF8"] = "1";
            psi.Environment["PYTHONIOENCODING"] = "utf-8";

            var process = new Process { StartInfo = psi, EnableRaisingEvents = true };
            process.Start();
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
            process.OutputDataReceived += (_, e) => AppendLog(outLog, e.Data);
            process.ErrorDataReceived += (_, e) => AppendLog(errLog, e.Data);
            _manager = process;
            AppLog.Info($"已启动管理面板 PID={process.Id} {Host}:{Port}");
        }
    }

    private void StartUpstream()
    {
        var exe = UpstreamExe;
        if (!File.Exists(exe))
        {
            AppLog.Info("未找到 upstream\\wb2api.exe，跳过上游启动");
            return;
        }

        var existing = FindUpstreamProcesses();
        if (existing.Count > 0)
        {
            WritePidFile(existing[0].Pid);
            AppLog.Info($"上游已在运行 PID={existing[0].Pid}");
            return;
        }

        lock (_gate)
        {
            if (_upstream is { HasExited: false }) return;

            Directory.CreateDirectory(Path.Combine(UpstreamDir, "data"));
            var outLog = Path.Combine(UpstreamDir, "data", "server.out.log");
            var errLog = Path.Combine(UpstreamDir, "data", "server.err.log");

            var psi = new ProcessStartInfo
            {
                FileName = exe,
                WorkingDirectory = UpstreamDir,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8,
            };
            psi.ArgumentList.Add("-config");
            psi.ArgumentList.Add("config.json");

            var process = new Process { StartInfo = psi, EnableRaisingEvents = true };
            process.Start();
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
            process.OutputDataReceived += (_, e) => AppendLog(outLog, e.Data);
            process.ErrorDataReceived += (_, e) => AppendLog(errLog, e.Data);
            _upstream = process;
            WritePidFile(process.Id);
            AppLog.Info($"已启动上游网关 PID={process.Id}");
        }
    }

    private List<ProcessInfo> FindUpstreamProcesses()
    {
        return WmiProcesses.Query()
            .Where(p => string.Equals(p.Name, "wb2api.exe", StringComparison.OrdinalIgnoreCase))
            .Where(p => p.ExecutablePath is not null && ProcessUtil.PathIsUnder(p.ExecutablePath, UpstreamDir))
            .ToList();
    }

    private void WritePidFile(int pid)
    {
        try
        {
            Directory.CreateDirectory(UpstreamDir);
            File.WriteAllText(UpstreamPidFile, pid.ToString(), new UTF8Encoding(false));
        }
        catch (Exception ex)
        {
            AppLog.Error("写上游 PID 文件失败", ex);
        }
    }

    private static void AppendLog(string path, string? line)
    {
        if (line is null) return;
        try
        {
            lock (typeof(ServiceController))
            {
                File.AppendAllText(path, line + Environment.NewLine, Encoding.UTF8);
            }
        }
        catch
        {
        }
    }

    private static string ReadTail(string path, int maxChars)
    {
        try
        {
            if (!File.Exists(path)) return "";
            using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
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

    // ── 停止 ────────────────────────────────────────────────

    public async Task StopAsync(CancellationToken ct = default)
    {
        Process? manager;
        lock (_gate) manager = _manager;

        if (manager is { HasExited: false })
        {
            try
            {
                manager.Kill(entireProcessTree: true);
                await manager.WaitForExitAsync(ct).ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                AppLog.Error("结束管理面板进程失败", ex);
            }
        }
        else
        {
            var instances = FindManagerInstances();
            if (instances.Count == 0)
            {
                if (await IsPortOpenAsync(ConnectHost, Port).ConfigureAwait(false))
                {
                    throw new InvalidOperationException(
                        $"端口 {Port} 上有进程在监听，但不是本软件启动的管理面板，已中止停止操作。");
                }
            }
            else
            {
                // uv 的 .venv 会再派生一个真实解释器子进程，子进程的命令行里只有 uv 的
                // python 路径，看不出属于哪个目录。判断归属时以「同一端口的整棵进程树」
                // 为单位：只要有一个进程属于本目录，就认为这个端口上的服务是我们的。
                if (!instances.Any(InstanceBelongsToRoot))
                {
                    throw new InvalidOperationException(
                        "正在运行的管理面板属于另一个目录的实例，已中止停止操作。");
                }
                foreach (var instance in instances)
                {
                    try
                    {
                        Process.GetProcessById(instance.Pid).Kill(entireProcessTree: true);
                    }
                    catch (ArgumentException)
                    {
                        // 进程树的父进程先退出时，子进程可能已经不在了
                    }
                    catch (Exception ex)
                    {
                        AppLog.Error($"结束 PID {instance.Pid} 失败", ex);
                    }
                }
            }
        }

        StopUpstream();

        var deadline = DateTime.UtcNow.AddSeconds(20);
        while (DateTime.UtcNow < deadline)
        {
            if (!await IsPortOpenAsync(ConnectHost, Port).ConfigureAwait(false)) break;
            await Task.Delay(250, ct).ConfigureAwait(false);
        }

        if (await IsPortOpenAsync(ConnectHost, Port).ConfigureAwait(false))
        {
            throw new InvalidOperationException($"端口 {Port} 仍在监听，服务可能没有完全停止。");
        }
    }

    private void StopUpstream()
    {
        try
        {
            if (_upstream is { HasExited: false })
            {
                _upstream.Kill(entireProcessTree: true);
                _upstream.WaitForExit(5000);
            }
        }
        catch (Exception ex)
        {
            AppLog.Error("结束上游进程失败", ex);
        }

        foreach (var inst in FindUpstreamProcesses())
        {
            try
            {
                Process.GetProcessById(inst.Pid).Kill(entireProcessTree: true);
            }
            catch (Exception ex)
            {
                AppLog.Error($"结束上游 PID {inst.Pid} 失败", ex);
            }
        }

        try
        {
            if (File.Exists(UpstreamPidFile)) File.Delete(UpstreamPidFile);
        }
        catch
        {
        }
    }

    public async Task<ServiceSnapshot> RestartAsync(IProgress<string> progress, CancellationToken ct)
    {
        progress.Report("正在停止服务…");
        try
        {
            await StopAsync(ct).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            return new ServiceSnapshot { State = ServiceState.Error, Port = Port, Message = ex.Message };
        }
        await Task.Delay(500, ct).ConfigureAwait(false);
        return await EnsureStartedAsync(progress, ct).ConfigureAwait(false);
    }
}
