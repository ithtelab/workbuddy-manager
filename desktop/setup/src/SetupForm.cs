using System.IO.Compression;
using System.Text;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace WorkBuddySetup;

internal sealed class SetupForm : Form
{
    private readonly WebView2 _web = new();
    private bool _webReady;
    private bool _busy;
    private string _currentDir = Installer.DefaultDir;
    private string _installedDir = "";
    public static bool DemoDone;

    public SetupForm()
    {
        Text = "WorkBuddy Manager 安装程序";
        var scale = DpiUtil.Scale;
        ClientSize = new Size((int)(680 * scale), (int)(720 * scale));
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
        FormClosing += OnFormClosing;
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
            core.NewWindowRequested += (_, args) => args.Handled = true;

            var html = File.ReadAllText(Path.Combine(AssetDir(), "setup.html"));
            core.NavigateToString(html);
            _webReady = true;
        }
        catch (Exception ex)
        {
            MessageBox.Show(this, "安装程序界面初始化失败：\n" + ex.Message,
                "WorkBuddy Manager", MessageBoxButtons.OK, MessageBoxIcon.Error);
            Close();
        }
    }

    private static string AssetDir()
    {
        var dir = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "WorkBuddyManagerSetup", "ui");
        Directory.CreateDirectory(dir);
        var assembly = typeof(SetupForm).Assembly;
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
        string? type = null, dir = null;
        bool launch = true;
        try
        {
            using var doc = JsonDocument.Parse(e.WebMessageAsJson);
            if (doc.RootElement.TryGetProperty("type", out var t)) type = t.GetString();
            if (doc.RootElement.TryGetProperty("dir", out var d)) dir = d.GetString();
            if (doc.RootElement.TryGetProperty("launch", out var l)) launch = l.ValueKind != JsonValueKind.False;
        }
        catch
        {
            return;
        }

        switch (type)
        {
            case "ready":
                if (DemoDone)
                {
                    Post(new { type = "done", dir = @"D:\WorkBuddyTest5" });
                    break;
                }
                PostInit();
                break;
            case "browse":
                Browse();
                break;
            case "install":
                if (!string.IsNullOrWhiteSpace(dir)) _currentDir = dir!;
                _ = RunInstallAsync(_currentDir);
                break;
            case "finish":
                if (launch && _installedDir.Length > 0) Installer.Launch(_installedDir);
                Close();
                break;
        }
    }

    private void Browse()
    {
        using var dialog = new FolderBrowserDialog
        {
            Description = "选择安装位置",
            UseDescriptionForTitle = true,
            SelectedPath = Directory.Exists(_currentDir) ? _currentDir : Installer.DefaultDir,
            ShowNewFolderButton = true,
        };
        if (dialog.ShowDialog(this) == DialogResult.OK)
        {
            _currentDir = dialog.SelectedPath;
            PostInit();
        }
    }

    private void PostInit()
    {
        var sizeMb = Payload.Size > 0 ? Math.Round(Payload.Size / 1024.0 / 1024.0) : 0;
        Post(new
        {
            type = "init",
            dir = _currentDir,
            version = ReadPayloadVersion(),
            sizeMb,
        });
    }

    private static string ReadPayloadVersion()
    {
        try
        {
            using var stream = Payload.Open();
            if (stream is null) return "";
            using var zip = new ZipArchive(stream, ZipArchiveMode.Read);
            var entry = zip.GetEntry(".version");
            if (entry is null) return "";
            using var reader = new StreamReader(entry.Open(), Encoding.UTF8);
            return reader.ReadToEnd().Trim();
        }
        catch
        {
            return "";
        }
    }

    private async Task RunInstallAsync(string dir)
    {
        if (_busy) return;
        _busy = true;
        try
        {
            var progress = new Progress<(int Percent, string Message)>(p =>
                Post(new { type = "progress", percent = p.Percent, message = p.Message }));
            await Task.Run(() => Installer.Install(dir, progress));
            _installedDir = Path.GetFullPath(dir.TrimEnd('\\', '/'));
            Post(new { type = "done", dir = _installedDir });
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

    private void OnFormClosing(object? sender, FormClosingEventArgs e)
    {
        if (!_busy) return;
        e.Cancel = true;
        MessageBox.Show(this, "正在安装，请等当前步骤完成后再关闭窗口。",
            "WorkBuddy Manager", MessageBoxButtons.OK, MessageBoxIcon.Information);
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
