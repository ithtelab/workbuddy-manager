using System.Text;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace WorkBuddyUninstall;

internal sealed class UninstallForm : Form
{
    private readonly WebView2 _web = new();
    private bool _webReady;
    private bool _busy;

    public UninstallForm()
    {
        Text = "卸载 WorkBuddy Manager";
        var scale = DpiUtil.Scale;
        ClientSize = new Size((int)(620 * scale), (int)(600 * scale));
        FormBorderStyle = FormBorderStyle.FixedSingle;
        MaximizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;
        BackColor = Color.White;
        try
        {
            Icon = System.Drawing.Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        }
        catch
        {
        }

        _web.Dock = DockStyle.Fill;
        _web.BackColor = Color.White;
        Controls.Add(_web);

        Shown += async (_, _) => await InitializeAsync();
        FormClosing += (_, e) =>
        {
            if (!_busy) return;
            e.Cancel = true;
            MessageBox.Show(this, "正在卸载，请稍候。", "WorkBuddy Manager",
                MessageBoxButtons.OK, MessageBoxIcon.Information);
        };
    }

    private async Task InitializeAsync()
    {
        try
        {
            var profile = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "WorkBuddyManagerSetup", "WebView2");
            var environment = await CoreWebView2Environment.CreateAsync(null, profile);
            await _web.EnsureCoreWebView2Async(environment);
            var core = _web.CoreWebView2;
            core.Settings.AreDevToolsEnabled = false;
            core.Settings.IsStatusBarEnabled = false;
            core.Settings.AreDefaultContextMenusEnabled = false;
            core.WebMessageReceived += OnWebMessage;
            var html = File.ReadAllText(Path.Combine(AssetDir(), "uninstall.html"));
            core.NavigateToString(html);
            _webReady = true;
        }
        catch (Exception ex)
        {
            MessageBox.Show(this, "卸载界面初始化失败：\n" + ex.Message, "WorkBuddy Manager",
                MessageBoxButtons.OK, MessageBoxIcon.Error);
            Close();
        }
    }

    private static string AssetDir()
    {
        var dir = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "WorkBuddyManagerSetup", "ui");
        Directory.CreateDirectory(dir);
        var assembly = typeof(UninstallForm).Assembly;
        foreach (var resource in assembly.GetManifestResourceNames())
        {
            if (!resource.EndsWith(".html", StringComparison.OrdinalIgnoreCase)) continue;
            var marker = resource.IndexOf("WebAssets.", StringComparison.Ordinal);
            var name = marker >= 0 ? resource[(marker + "WebAssets.".Length)..] : Path.GetFileName(resource);
            using var stream = assembly.GetManifestResourceStream(resource);
            if (stream is null) continue;
            using var reader = new StreamReader(stream, Encoding.UTF8);
            File.WriteAllText(Path.Combine(dir, name), reader.ReadToEnd(), new UTF8Encoding(false));
        }
        return dir;
    }

    private void OnWebMessage(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        string? type = null;
        var keepData = true;
        var removeSettings = true;
        try
        {
            using var doc = JsonDocument.Parse(e.WebMessageAsJson);
            if (doc.RootElement.TryGetProperty("type", out var t)) type = t.GetString();
            if (doc.RootElement.TryGetProperty("keepData", out var k)) keepData = k.ValueKind != JsonValueKind.False;
            if (doc.RootElement.TryGetProperty("removeSettings", out var r)) removeSettings = r.ValueKind != JsonValueKind.False;
        }
        catch
        {
            return;
        }

        switch (type)
        {
            case "ready":
                Post(new { type = "init", root = Uninstaller.Root });
                break;
            case "uninstall":
                _ = RunAsync(keepData, removeSettings);
                break;
            case "close":
                if (!_busy) Close();
                break;
            case "folder":
                try
                {
                    System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
                    {
                        FileName = Uninstaller.Root,
                        UseShellExecute = true,
                    });
                }
                catch
                {
                }
                break;
        }
    }

    private async Task RunAsync(bool keepData, bool removeSettings)
    {
        if (_busy) return;
        _busy = true;
        try
        {
            var progress = new Progress<(int Percent, string Message)>(p =>
                Post(new { type = "progress", percent = p.Percent, message = p.Message }));
            var kept = await Task.Run(() => Uninstaller.Run(Uninstaller.Root, keepData, removeSettings, progress));
            Post(new { type = "done", kept = kept ?? "" });
        }
        catch (Exception ex)
        {
            Post(new { type = "error", message = ex.Message });
        }
        finally
        {
            _busy = false;
        }
    }

    private void Post(object payload)
    {
        try
        {
            if (!_webReady || _web.CoreWebView2 is null) return;
            _web.CoreWebView2.PostWebMessageAsJson(JsonSerializer.Serialize(payload));
        }
        catch
        {
        }
    }
}
