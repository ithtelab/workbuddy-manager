using System.Net;
using System.Net.Http.Headers;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace WorkBuddyDesktop;

public sealed class ReleaseInfo
{
    public string Tag { get; init; } = "";
    public Version? Version { get; init; }
    public string TarUrl { get; init; } = "";
    public string? Sha256 { get; init; }
    public string Notes { get; init; } = "";
    public string HtmlUrl { get; init; } = "";
}

internal static class ReleaseClient
{
    public const string Repo = "ithtelab/workbuddy-manager";
    private const string UserAgent = "WorkBuddyManagerDesktop/1.0";

    private static readonly string[] LocalProxies =
    {
        "http://127.0.0.1:7897",
        "http://127.0.0.1:7890",
        "http://127.0.0.1:10809",
    };

    private const string DirectProxy = "::direct::";

    private static IEnumerable<string?> ProxyCandidates()
    {
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        yield return DirectProxy; // 先直连：很多环境直连可用，代理反而是坏的
        yield return null;        // 系统代理
        foreach (var key in new[] { "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy" })
        {
            var value = Environment.GetEnvironmentVariable(key);
            if (!string.IsNullOrWhiteSpace(value) && seen.Add(value)) yield return value;
        }
        foreach (var proxy in LocalProxies)
        {
            if (seen.Add(proxy)) yield return proxy;
        }
    }

    private static HttpClient BuildClient(string? proxy, TimeSpan timeout, bool allowRedirects = true)
    {
        var handler = new SocketsHttpHandler
        {
            ConnectTimeout = TimeSpan.FromSeconds(6),
            AllowAutoRedirect = allowRedirects,
            AutomaticDecompression = DecompressionMethods.All,
        };
        if (string.Equals(proxy, DirectProxy, StringComparison.Ordinal))
        {
            handler.UseProxy = false;
        }
        else if (proxy is not null)
        {
            handler.Proxy = new WebProxy(proxy);
            handler.UseProxy = true;
        }
        else
        {
            handler.UseProxy = true; // 用系统代理
        }
        var client = new HttpClient(handler) { Timeout = timeout };
        client.DefaultRequestHeaders.UserAgent.ParseAdd(UserAgent);
        client.DefaultRequestHeaders.Accept.Add(new MediaTypeWithQualityHeaderValue("application/vnd.github+json"));
        return client;
    }

    public sealed class Attempt<T>
    {
        public T? Value { get; init; }
        public string? Proxy { get; init; }
    }

    private static async Task<Attempt<string>> GetTextAsync(string url, TimeSpan timeout, CancellationToken ct)
    {
        Exception? last = null;
        foreach (var proxy in ProxyCandidates())
        {
            try
            {
                using var client = BuildClient(proxy, timeout);
                using var response = await client.GetAsync(url, ct).ConfigureAwait(false);
                if (!response.IsSuccessStatusCode) continue;
                var text = await response.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
                return new Attempt<string> { Value = text, Proxy = proxy };
            }
            catch (OperationCanceledException) when (!ct.IsCancellationRequested)
            {
                last = new TimeoutException("请求超时");
            }
            catch (Exception ex)
            {
                last = ex;
            }
        }
        throw new HttpRequestException($"无法连接 GitHub（已尝试直连与本地代理）。{last?.Message}");
    }

    public static async Task<ReleaseInfo> GetLatestAsync(CancellationToken ct)
    {
        // 1) Releases API：能一次拿到 tag、digest、发布说明
        try
        {
            var attempt = await GetTextAsync(
                $"https://api.github.com/repos/{Repo}/releases/latest",
                TimeSpan.FromSeconds(10), ct).ConfigureAwait(false);
            var info = ParseApiRelease(attempt.Value!);
            if (info is not null) return info;
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            AppLog.Info("Releases API 不可用，改用重定向：" + ex.Message);
        }

        // 2) releases/latest 的 302 重定向拿 tag（不受 API 限额影响）
        var tag = await GetTagViaRedirectAsync(ct).ConfigureAwait(false);
        if (tag is null) throw new HttpRequestException("无法获取最新版本号。");

        // 3) 再试 tags/{tag} 拿摘要
        string? sha = null;
        string notes = "";
        try
        {
            var attempt = await GetTextAsync(
                $"https://api.github.com/repos/{Repo}/releases/tags/{tag}",
                TimeSpan.FromSeconds(10), ct).ConfigureAwait(false);
            var info = ParseApiRelease(attempt.Value!);
            if (info is not null)
            {
                sha = info.Sha256;
                notes = info.Notes;
            }
        }
        catch
        {
        }

        // 4) 最后从发布页 HTML 里找发布说明里印的 sha256
        if (sha is null)
        {
            try
            {
                var htmlAttempt = await GetTextAsync(
                    $"https://github.com/{Repo}/releases/tag/{tag}",
                    TimeSpan.FromSeconds(12), ct).ConfigureAwait(false);
                sha = ExtractShaFromHtml(htmlAttempt.Value!);
            }
            catch
            {
            }
        }

        return new ReleaseInfo
        {
            Tag = tag,
            Version = ParseVersion(tag),
            TarUrl = $"https://github.com/{Repo}/releases/download/{tag}/workbuddy-manager-{tag}.tar.gz",
            Sha256 = sha,
            Notes = notes,
            HtmlUrl = $"https://github.com/{Repo}/releases/tag/{tag}",
        };
    }

    private static ReleaseInfo? ParseApiRelease(string json)
    {
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        if (!root.TryGetProperty("tag_name", out var tagProp)) return null;
        var tag = tagProp.GetString();
        if (string.IsNullOrWhiteSpace(tag)) return null;

        string? sha = null;
        string tarUrl = "";
        if (root.TryGetProperty("assets", out var assets) && assets.ValueKind == JsonValueKind.Array)
        {
            foreach (var asset in assets.EnumerateArray())
            {
                var name = asset.TryGetProperty("name", out var n) ? n.GetString() : null;
                if (name is null) continue;
                if (!name.EndsWith(".tar.gz", StringComparison.OrdinalIgnoreCase)) continue;
                tarUrl = asset.TryGetProperty("browser_download_url", out var u) ? u.GetString() ?? "" : "";
                if (asset.TryGetProperty("digest", out var d))
                {
                    var digest = d.GetString();
                    if (digest is not null && digest.StartsWith("sha256:", StringComparison.OrdinalIgnoreCase))
                    {
                        sha = digest["sha256:".Length..].Trim().ToLowerInvariant();
                    }
                }
                break;
            }
        }

        if (string.IsNullOrEmpty(tarUrl))
        {
            tarUrl = $"https://github.com/{Repo}/releases/download/{tag}/workbuddy-manager-{tag}.tar.gz";
        }

        return new ReleaseInfo
        {
            Tag = tag,
            Version = ParseVersion(tag),
            TarUrl = tarUrl,
            Sha256 = sha,
            Notes = root.TryGetProperty("body", out var b) ? b.GetString() ?? "" : "",
            HtmlUrl = root.TryGetProperty("html_url", out var h) ? h.GetString() ?? "" : "",
        };
    }

    private static async Task<string?> GetTagViaRedirectAsync(CancellationToken ct)
    {
        Exception? last = null;
        foreach (var proxy in ProxyCandidates())
        {
            try
            {
                using var client = BuildClient(proxy, TimeSpan.FromSeconds(8), allowRedirects: false);
                using var request = new HttpRequestMessage(HttpMethod.Head,
                    $"https://github.com/{Repo}/releases/latest");
                using var response = await client.SendAsync(request, ct).ConfigureAwait(false);
                var location = response.Headers.Location?.ToString();
                if (location is null) continue;
                var match = Regex.Match(location, @"/releases/tag/([^/?#]+)");
                if (match.Success) return match.Groups[1].Value;
            }
            catch (OperationCanceledException) when (!ct.IsCancellationRequested)
            {
                last = new TimeoutException("请求超时");
            }
            catch (Exception ex)
            {
                last = ex;
            }
        }
        if (last is not null) AppLog.Info("获取最新 tag 失败：" + last.Message);
        return null;
    }

    private static string? ExtractShaFromHtml(string html)
    {
        var match = Regex.Match(html,
            @"([a-fA-F0-9]{64})\s+(?:</?[^>]+>\s*)*workbuddy-manager-[^<\s""]*\.tar\.gz",
            RegexOptions.IgnoreCase);
        if (match.Success) return match.Groups[1].Value.ToLowerInvariant();

        var loose = Regex.Match(html, @"workbuddy-manager-v[0-9.]+\.tar\.gz[\s\S]{0,400}?([a-fA-F0-9]{64})");
        if (loose.Success) return loose.Groups[1].Value.ToLowerInvariant();
        return null;
    }

    public static async Task DownloadAsync(string url, string destination, string? proxy,
        IProgress<(long received, long? total)> progress, CancellationToken ct)
    {
        var candidates = new List<string?> { null, proxy, DirectProxy };
        candidates.AddRange(ProxyCandidates());
        var seen = new HashSet<string?>(StringComparer.OrdinalIgnoreCase);

        Exception? last = null;
        foreach (var candidate in candidates)
        {
            if (!seen.Add(candidate)) continue;
            try
            {
                using var client = BuildClient(candidate, TimeSpan.FromMinutes(5));
                using var response = await client.GetAsync(url, HttpCompletionOption.ResponseHeadersRead, ct)
                    .ConfigureAwait(false);
                response.EnsureSuccessStatusCode();
                var total = response.Content.Headers.ContentLength;
                await using var source = await response.Content.ReadAsStreamAsync(ct).ConfigureAwait(false);
                await using var target = new FileStream(destination, FileMode.Create, FileAccess.Write, FileShare.None,
                    81920, useAsync: true);

                var buffer = new byte[81920];
                long received = 0;
                while (true)
                {
                    var read = await source.ReadAsync(buffer, ct).ConfigureAwait(false);
                    if (read == 0) break;
                    await target.WriteAsync(buffer.AsMemory(0, read), ct).ConfigureAwait(false);
                    received += read;
                    progress.Report((received, total));
                }
                return;
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                throw;
            }
            catch (Exception ex)
            {
                last = ex;
                AppLog.Info($"下载尝试失败（{candidate ?? "系统代理"}）：{ex.Message}");
                try
                {
                    if (File.Exists(destination)) File.Delete(destination);
                }
                catch
                {
                }
            }
        }
        throw new HttpRequestException("下载发布包失败。" + (last?.Message ?? ""));
    }

    private static readonly Regex VersionRegex = new(@"(\d+)\.(\d+)\.(\d+)", RegexOptions.Compiled);

    public static Version? ParseVersion(string? raw)
    {
        if (string.IsNullOrWhiteSpace(raw)) return null;
        var match = VersionRegex.Match(raw);
        return match.Success ? new Version(int.Parse(match.Groups[1].Value),
            int.Parse(match.Groups[2].Value), int.Parse(match.Groups[3].Value)) : null;
    }
}
