using System.Diagnostics;
using System.Formats.Tar;
using System.IO.Compression;
using System.Security.Cryptography;
using System.Text;

namespace WorkBuddyDesktop;

public enum UpdatePhase
{
    Idle,
    Checking,
    UpToDate,
    Available,
    Downloading,
    Verifying,
    Extracting,
    Stopping,
    Replacing,
    Dependencies,
    Starting,
    Done,
    Error,
}

public sealed class UpdateProgress
{
    public UpdatePhase Phase { get; init; }
    public string Message { get; init; } = "";
    public double Percent { get; init; }
}

public sealed class UpdateException : Exception
{
    public bool CanRollback { get; }

    public UpdateException(string message, bool canRollback = false, Exception? inner = null)
        : base(message, inner)
    {
        CanRollback = canRollback;
    }
}

internal sealed class Updater
{
    private static int _busy;

    /// <summary>同一时间只允许一个更新/回滚任务（面板与更新窗口共用）。</summary>
    public static bool TryBegin() => Interlocked.CompareExchange(ref _busy, 1, 0) == 0;

    public static void End() => Interlocked.Exchange(ref _busy, 0);

    public static bool IsBusy => Volatile.Read(ref _busy) == 1;

    private static readonly string[] ReplaceDirectories = { "server", "web\\out", "deploy", "docs" };
    private static readonly string[] ReplaceFiles = { "CHANGELOG.md", "README.md", "README.en.md" };

    private readonly string _root;
    private readonly ServiceController _service;

    private string? _backupRoot;
    private string? _stagingRoot;
    private string? _previousVersion;
    private bool _swapInProgress;

    private sealed record SwapEntry(string Target, string? Backup, bool IsDirectory);

    private readonly List<SwapEntry> _journal = new();

    public Updater(string root, ServiceController service)
    {
        _root = root;
        _service = service;
    }

    public string CurrentVersion => RootLocator.ReadVersion(_root) ?? "未知";

    public bool CanRollback => _backupRoot is not null && Directory.Exists(_backupRoot);

    public async Task<ReleaseInfo> CheckAsync(CancellationToken ct)
    {
        return await ReleaseClient.GetLatestAsync(ct).ConfigureAwait(false);
    }

    public static bool IsNewer(ReleaseInfo release, string? current)
    {
        var latest = release.Version;
        var now = ReleaseClient.ParseVersion(current);
        if (latest is null) return false;
        if (now is null) return true;
        return latest > now;
    }

    public async Task UpdateAsync(ReleaseInfo release, bool allowUnverified,
        IProgress<UpdateProgress> progress, CancellationToken ct)
    {
        _journal.Clear();
        _swapInProgress = false;
        _previousVersion = RootLocator.ReadVersion(_root);

        var workDir = Path.Combine(Path.GetTempPath(), "WorkBuddyManager-update-" + DateTime.Now.ToString("yyyyMMddHHmmss"));
        Directory.CreateDirectory(workDir);
        var packagePath = Path.Combine(workDir, "package.tar.gz");

        try
        {
            _stagingRoot = Path.Combine(_root, ".update-staging-" + DateTime.Now.ToString("yyyyMMddHHmmss"));
            _backupRoot = Path.Combine(_root, ".update-backup-" + DateTime.Now.ToString("yyyyMMddHHmmss"));

            progress.Report(new UpdateProgress
            {
                Phase = UpdatePhase.Downloading,
                Message = $"正在下载 {release.Tag}…",
            });

            var lastReport = DateTime.MinValue;
            var downloadProgress = new Progress<(long received, long? total)>(p =>
            {
                var now = DateTime.UtcNow;
                if ((now - lastReport).TotalMilliseconds < 120) return;
                lastReport = now;
                var percent = p.total is > 0 ? Math.Clamp(p.received * 100.0 / p.total.Value, 0, 100) : 0;
                var text = p.total is > 0
                    ? $"{p.received / 1024.0 / 1024.0:F1} / {p.total.Value / 1024.0 / 1024.0:F1} MB"
                    : $"{p.received / 1024.0 / 1024.0:F1} MB";
                progress.Report(new UpdateProgress
                {
                    Phase = UpdatePhase.Downloading,
                    Message = "正在下载发布包 " + text,
                    Percent = percent,
                });
            });

            await ReleaseClient.DownloadAsync(release.TarUrl, packagePath, null, downloadProgress, ct)
                .ConfigureAwait(false);

            progress.Report(new UpdateProgress
            {
                Phase = UpdatePhase.Verifying,
                Message = "正在校验 SHA-256…",
                Percent = 100,
            });

            var actual = ComputeSha256(packagePath);
            if (string.IsNullOrWhiteSpace(release.Sha256))
            {
                if (!allowUnverified)
                {
                    throw new UpdateException(
                        "没能从发布页取得这个版本的 SHA-256，已拒绝安装。\n" +
                        "如果你确认包来自官方 Releases 页面，可以选择「未校验也继续」。",
                        canRollback: false);
                }
                AppLog.Info($"未取得发布页摘要，按用户选择继续安装：本地 sha256={actual}");
            }
            else if (!string.Equals(actual, release.Sha256, StringComparison.OrdinalIgnoreCase))
            {
                throw new UpdateException(
                    $"发布包校验失败，已中止。\n期望：{release.Sha256}\n实际：{actual}", canRollback: false);
            }

            progress.Report(new UpdateProgress
            {
                Phase = UpdatePhase.Extracting,
                Message = "正在解压到临时目录…",
                Percent = 0,
            });

            if (Directory.Exists(_stagingRoot)) Directory.Delete(_stagingRoot, true);
            Directory.CreateDirectory(_stagingRoot);
            SafeExtract(packagePath, _stagingRoot);

            var top = FindPackageRoot(_stagingRoot);
            if (top is null)
            {
                throw new UpdateException("发布包结构不符合预期（找不到 workbuddy-manager-* 目录）。");
            }
            if (!File.Exists(Path.Combine(top, "server", "main.py"))
                || !File.Exists(Path.Combine(top, "web", "out", "index.html")))
            {
                throw new UpdateException("发布包缺少 server/main.py 或 web/out/index.html，已中止。");
            }
            var stagedVersionPath = Path.Combine(top, ".version");
            if (File.Exists(stagedVersionPath))
            {
                var stagedVersion = File.ReadAllText(stagedVersionPath).Trim();
                if (!string.IsNullOrWhiteSpace(stagedVersion)
                    && !string.Equals(stagedVersion, release.Tag, StringComparison.OrdinalIgnoreCase))
                {
                    throw new UpdateException(
                        $"发布包里的版本号（{stagedVersion}）与最新版（{release.Tag}）不一致，已中止。");
                }
            }

            progress.Report(new UpdateProgress
            {
                Phase = UpdatePhase.Stopping,
                Message = "正在停止服务…",
            });
            await _service.StopAsync(ct).ConfigureAwait(false);

            progress.Report(new UpdateProgress
            {
                Phase = UpdatePhase.Replacing,
                Message = "正在替换程序文件…",
            });

            try
            {
                _swapInProgress = true;
                ReplaceFromStaging(top);
            }
            catch (Exception ex)
            {
                RestoreFiles();
                _swapInProgress = false;
                throw new UpdateException("替换文件失败，已回滚到更新前。\n" + ex.Message,
                    canRollback: false, ex);
            }

            _swapInProgress = false;

            progress.Report(new UpdateProgress
            {
                Phase = UpdatePhase.Dependencies,
                Message = "正在同步 Python 依赖…",
            });
            var depWarning = SyncDependencies(ct);

            progress.Report(new UpdateProgress
            {
                Phase = UpdatePhase.Starting,
                Message = "正在重新启动服务…",
            });

            var innerProgress = new Progress<string>(message =>
                progress.Report(new UpdateProgress { Phase = UpdatePhase.Starting, Message = message }));
            var snapshot = await _service.EnsureStartedAsync(innerProgress, ct).ConfigureAwait(false);

            if (snapshot.State is not (ServiceState.Ready or ServiceState.External))
            {
                throw new UpdateException(
                    "更新已完成，但服务没能正常启动：\n" + snapshot.Message +
                    (string.IsNullOrWhiteSpace(depWarning) ? "" : "\n\n依赖同步输出：\n" + depWarning),
                    canRollback: CanRollback);
            }

            CleanupBackup();
            File.WriteAllText(Path.Combine(_root, ".version"), release.Tag, new UTF8Encoding(false));
            if (Directory.Exists(_stagingRoot)) Directory.Delete(_stagingRoot, true);

            progress.Report(new UpdateProgress
            {
                Phase = UpdatePhase.Done,
                Message = $"已更新到 {release.Tag}，服务已重新启动。",
                Percent = 100,
            });
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (UpdateException)
        {
            throw;
        }
        catch (Exception ex)
        {
            if (_journal.Count > 0 && !_swapInProgress)
            {
                try
                {
                    await _service.StopAsync(CancellationToken.None).ConfigureAwait(false);
                }
                catch
                {
                }
                RestoreFiles();
            }
            throw new UpdateException("更新失败：" + ex.Message, CanRollback, ex);
        }
        finally
        {
            try
            {
                if (!CanRollback && Directory.Exists(_stagingRoot)) Directory.Delete(_stagingRoot, true);
                if (File.Exists(packagePath)) File.Delete(packagePath);
                if (Directory.Exists(workDir)) Directory.Delete(workDir, true);
            }
            catch
            {
            }
        }
    }

    public async Task RollbackAsync(IProgress<UpdateProgress> progress, CancellationToken ct)
    {
        if (!CanRollback)
        {
            throw new UpdateException("没有可用的更新前备份。");
        }

        progress.Report(new UpdateProgress { Phase = UpdatePhase.Stopping, Message = "正在停止服务…" });
        try
        {
            await _service.StopAsync(ct).ConfigureAwait(false);
        }
        catch
        {
        }

        progress.Report(new UpdateProgress { Phase = UpdatePhase.Replacing, Message = "正在恢复更新前的文件…" });
        RestoreFiles();

        progress.Report(new UpdateProgress { Phase = UpdatePhase.Starting, Message = "正在重新启动服务…" });
        var snapshot = await _service.EnsureStartedAsync(
            new Progress<string>(m => progress.Report(new UpdateProgress { Phase = UpdatePhase.Starting, Message = m })),
            ct).ConfigureAwait(false);

        if (snapshot.State is not (ServiceState.Ready or ServiceState.External))
        {
            throw new UpdateException("已恢复文件，但服务启动失败：\n" + snapshot.Message);
        }

        CleanupBackup();
        progress.Report(new UpdateProgress
        {
            Phase = UpdatePhase.Done,
            Message = "已回滚到更新前的版本，服务已重新启动。",
            Percent = 100,
        });
    }

    // ── 文件替换 ────────────────────────────────────────────

    private void ReplaceFromStaging(string stagedTop)
    {
        Directory.CreateDirectory(_backupRoot!);

        foreach (var relative in ReplaceDirectories)
        {
            var source = Path.Combine(stagedTop, relative);
            if (!Directory.Exists(source)) continue;

            var target = Path.Combine(_root, relative);
            var backupName = relative.Replace('\\', '_').Replace('/', '_');
            var backup = Path.Combine(_backupRoot!, backupName);

            // 先登记再动手：即使 Move 中途失败，回滚也知道该恢复什么
            _journal.Add(new SwapEntry(target, Directory.Exists(target) ? backup : null, true));

            if (Directory.Exists(target))
            {
                Directory.Move(target, backup);
            }
            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
            Directory.Move(source, target);
        }

        var filesBackup = Path.Combine(_backupRoot!, "files");
        foreach (var relative in ReplaceFiles)
        {
            var source = Path.Combine(stagedTop, relative);
            if (!File.Exists(source)) continue;

            var target = Path.Combine(_root, relative);
            string? backup = null;
            if (File.Exists(target))
            {
                Directory.CreateDirectory(filesBackup);
                backup = Path.Combine(filesBackup, relative.Replace('\\', '_'));
                File.Copy(target, backup, overwrite: true);
            }
            _journal.Add(new SwapEntry(target, backup, false));
            File.Copy(source, target, overwrite: true);
        }
    }

    private void RestoreFiles()
    {
        foreach (var entry in _journal.AsEnumerable().Reverse())
        {
            try
            {
                if (entry.IsDirectory)
                {
                    if (Directory.Exists(entry.Target)) Directory.Delete(entry.Target, true);
                    if (entry.Backup is not null && Directory.Exists(entry.Backup))
                    {
                        Directory.Move(entry.Backup, entry.Target);
                    }
                }
                else
                {
                    if (entry.Backup is not null && File.Exists(entry.Backup))
                    {
                        File.Copy(entry.Backup, entry.Target, overwrite: true);
                    }
                    else if (File.Exists(entry.Target))
                    {
                        File.Delete(entry.Target);
                    }
                }
            }
            catch (Exception ex)
            {
                AppLog.Error($"回滚 {entry.Target} 失败", ex);
            }
        }
        _journal.Clear();

        if (_previousVersion is not null)
        {
            try
            {
                File.WriteAllText(Path.Combine(_root, ".version"), _previousVersion, new UTF8Encoding(false));
            }
            catch (Exception ex)
            {
                AppLog.Error("恢复 .version 失败", ex);
            }
        }
    }

    private void CleanupBackup()
    {
        try
        {
            if (_backupRoot is not null && Directory.Exists(_backupRoot))
            {
                Directory.Delete(_backupRoot, true);
            }
            _backupRoot = null;
        }
        catch (Exception ex)
        {
            AppLog.Error("清理更新备份失败", ex);
        }
    }

    // ── 解压 ────────────────────────────────────────────────

    private static string ComputeSha256(string path)
    {
        using var stream = File.OpenRead(path);
        using var sha = SHA256.Create();
        return Convert.ToHexString(sha.ComputeHash(stream)).ToLowerInvariant();
    }

    private static void SafeExtract(string packagePath, string destination)
    {
        var destinationFull = Path.GetFullPath(destination).TrimEnd('\\') + "\\";
        using var file = File.OpenRead(packagePath);
        using var gzip = new GZipStream(file, CompressionMode.Decompress);
        using var reader = new TarReader(gzip);

        TarEntry? entry;
        while ((entry = reader.GetNextEntry()) is not null)
        {
            var name = entry.Name.Replace('\\', '/');
            if (string.IsNullOrWhiteSpace(name)) continue;
            if (name.StartsWith('/') || name.Contains(':')) throw new InvalidOperationException("发布包内包含非法路径：" + name);
            if (name.Split('/').Any(part => part == "..")) throw new InvalidOperationException("发布包内包含上跳路径：" + name);

            var target = Path.GetFullPath(Path.Combine(destination, name));
            if (!target.StartsWith(destinationFull, StringComparison.OrdinalIgnoreCase))
            {
                throw new InvalidOperationException("发布包内路径越界：" + name);
            }

            switch (entry.EntryType)
            {
                case TarEntryType.Directory:
                    Directory.CreateDirectory(target);
                    break;
                case TarEntryType.RegularFile:
                case TarEntryType.V7RegularFile:
                case TarEntryType.ContiguousFile:
                    Directory.CreateDirectory(Path.GetDirectoryName(target)!);
                    entry.ExtractToFile(target, overwrite: true);
                    break;
                case TarEntryType.SymbolicLink:
                case TarEntryType.HardLink:
                case TarEntryType.CharacterDevice:
                case TarEntryType.BlockDevice:
                case TarEntryType.Fifo:
                    throw new InvalidOperationException("发布包内包含不支持的条目类型：" + name);
                default:
                    break;
            }
        }
    }

    private static string? FindPackageRoot(string staging)
    {
        var dirs = Directory.GetDirectories(staging);
        var named = dirs.FirstOrDefault(d =>
            Path.GetFileName(d).StartsWith("workbuddy-manager-", StringComparison.OrdinalIgnoreCase));
        if (named is not null) return named;
        return dirs.Length == 1 ? dirs[0] : null;
    }

    // ── 依赖同步 ────────────────────────────────────────────

    private string SyncDependencies(CancellationToken ct)
    {
        try
        {
            var requirements = Path.Combine(_root, "server", "requirements.txt");
            if (!File.Exists(requirements)) return "";

            var uv = Path.Combine(_root, ".tools", "uv", "uv.exe");
            var python = _service.PythonPath;
            if (!File.Exists(uv))
            {
                AppLog.Info("未找到 .tools\\uv\\uv.exe，跳过依赖同步");
                return "未找到 .tools\\uv\\uv.exe，已跳过依赖同步。如果新版本依赖有变化，请手动同步。";
            }
            var psi = new ProcessStartInfo
            {
                WorkingDirectory = _root,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8,
            };

            if (File.Exists(uv))
            {
                psi.FileName = uv;
                psi.ArgumentList.Add("pip");
                psi.ArgumentList.Add("install");
                psi.ArgumentList.Add("-r");
                psi.ArgumentList.Add(requirements);
                psi.ArgumentList.Add("--python");
                psi.ArgumentList.Add(python);
            }
            else
            {
                psi.FileName = python;
                psi.ArgumentList.Add("-m");
                psi.ArgumentList.Add("pip");
                psi.ArgumentList.Add("install");
                psi.ArgumentList.Add("-r");
                psi.ArgumentList.Add(requirements);
            }

            using var process = Process.Start(psi);
            if (process is null) return "无法启动依赖同步进程";

            var output = new StringBuilder();
            process.OutputDataReceived += (_, e) => { if (e.Data is not null) output.AppendLine(e.Data); };
            process.ErrorDataReceived += (_, e) => { if (e.Data is not null) output.AppendLine(e.Data); };
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();

            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
            timeout.CancelAfter(TimeSpan.FromMinutes(5));
            try
            {
                process.WaitForExitAsync(timeout.Token).GetAwaiter().GetResult();
            }
            catch (OperationCanceledException)
            {
                try
                {
                    if (!process.HasExited) process.Kill(entireProcessTree: true);
                }
                catch
                {
                }
                AppLog.Info("依赖同步超时，继续启动服务");
                return "依赖同步超时";
            }

            var text = output.ToString();
            if (process.ExitCode != 0)
            {
                AppLog.Info($"依赖同步返回 {process.ExitCode}: {text}");
                return text.Length > 4000 ? text[^4000..] : text;
            }
            return "";
        }
        catch (Exception ex)
        {
            AppLog.Error("依赖同步失败", ex);
            return ex.Message;
        }
    }
}
