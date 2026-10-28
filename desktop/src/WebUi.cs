using System.Reflection;
using System.Text;
using Microsoft.Web.WebView2.WinForms;

namespace WorkBuddyDesktop;

internal static class WebUi
{
    public static string EnsureAssets()
    {
        AppPaths.EnsureCreated();
        var directory = AppPaths.UiDir;
        Directory.CreateDirectory(directory);

        var assembly = Assembly.GetExecutingAssembly();
        foreach (var resource in assembly.GetManifestResourceNames())
        {
            if (!resource.EndsWith(".html", StringComparison.OrdinalIgnoreCase)) continue;
            var marker = resource.IndexOf("WebAssets.", StringComparison.Ordinal);
            var fileName = marker >= 0
                ? resource[(marker + "WebAssets.".Length)..]
                : Path.GetFileName(resource);
            using var stream = assembly.GetManifestResourceStream(resource);
            if (stream is null) continue;
            using var reader = new StreamReader(stream, Encoding.UTF8);
            var html = reader.ReadToEnd();
            File.WriteAllText(Path.Combine(directory, fileName), html, new UTF8Encoding(false));
        }
        return directory;
    }

    public static void Post(WebView2 webView, object payload)
    {
        try
        {
            if (webView.CoreWebView2 is null) return;
            var json = System.Text.Json.JsonSerializer.Serialize(payload);
            webView.CoreWebView2.PostWebMessageAsJson(json);
        }
        catch (Exception ex)
        {
            AppLog.Error(ex);
        }
    }

    public static string? ExtractMessageType(string json)
    {
        try
        {
            using var doc = System.Text.Json.JsonDocument.Parse(json);
            return doc.RootElement.TryGetProperty("type", out var type) ? type.GetString() : null;
        }
        catch
        {
            return null;
        }
    }
}

internal static class ThemeInfo
{
    public static bool AppsUseLightTheme
    {
        get
        {
            try
            {
                using var key = Microsoft.Win32.Registry.CurrentUser.OpenSubKey(
                    @"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize");
                var value = key?.GetValue("AppsUseLightTheme");
                return value is int i && i != 0;
            }
            catch
            {
                return false;
            }
        }
    }

    public static Color WindowBackground => AppsUseLightTheme ? Color.FromArgb(250, 250, 250) : Color.FromArgb(12, 12, 14);
}
