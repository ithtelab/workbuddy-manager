using System.Text;

namespace WorkBuddyDesktop;

internal static class RootLocator
{
    /// <summary>
    /// 从起始目录向上找包含 server\main.py 的目录。exe 放根目录或 desktop\app\ 都能定位。
    /// </summary>
    public static string? Find(string start)
    {
        try
        {
            var dir = new DirectoryInfo(Path.GetFullPath(start));
            for (var depth = 0; depth < 5 && dir is not null; depth++, dir = dir.Parent)
            {
                if (File.Exists(Path.Combine(dir.FullName, "server", "main.py")))
                {
                    return dir.FullName;
                }
            }
        }
        catch
        {
        }
        return null;
    }

    public static string? ReadVersion(string root)
    {
        try
        {
            var path = Path.Combine(root, ".version");
            if (!File.Exists(path)) return null;
            var text = File.ReadAllText(path).Trim();
            return string.IsNullOrWhiteSpace(text) ? null : text;
        }
        catch
        {
            return null;
        }
    }
}

/// <summary>
/// .env 的轻量读写：保留原有行序与注释，只改需要改的键；写回 UTF-8（无 BOM）。
/// </summary>
internal sealed class EnvFile
{
    private readonly string _path;
    private readonly List<string> _lines;
    private string _newLine = "\n";

    public EnvFile(string path)
    {
        _path = path;
        if (File.Exists(path))
        {
            var raw = File.ReadAllText(path, Encoding.UTF8);
            _newLine = raw.Contains("\r\n") ? "\r\n" : "\n";
            _lines = raw.Replace("\r\n", "\n").Split('\n').ToList();
            if (_lines.Count > 0 && _lines[^1].Length == 0) _lines.RemoveAt(_lines.Count - 1);
        }
        else
        {
            _lines = new List<string>();
        }
    }

    public string? Get(string key)
    {
        foreach (var line in _lines)
        {
            var parsed = Parse(line);
            if (parsed is { } kv && kv.Key == key) return kv.Value;
        }
        return null;
    }

    public int GetInt(string key, int fallback)
    {
        return int.TryParse(Get(key), out var value) ? value : fallback;
    }

    public bool Set(string key, string value)
    {
        var newLine = $"{key}={value}";
        for (var i = 0; i < _lines.Count; i++)
        {
            var parsed = Parse(_lines[i]);
            if (parsed is { } kv && kv.Key == key)
            {
                if (_lines[i] == newLine) return false;
                _lines[i] = newLine;
                return true;
            }
        }
        _lines.Add(newLine);
        return true;
    }

    public void Save()
    {
        AppPaths.EnsureCreated();
        var text = string.Join(_newLine, _lines) + _newLine;
        File.WriteAllText(_path, text, new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
    }

    private static KeyValuePair<string, string>? Parse(string line)
    {
        var trimmed = line.Trim();
        if (trimmed.Length == 0 || trimmed.StartsWith('#')) return null;
        var eq = trimmed.IndexOf('=');
        if (eq <= 0) return null;
        var key = trimmed[..eq].Trim();
        var value = trimmed[(eq + 1)..].Trim();
        if (value.Length >= 2 &&
            ((value[0] == '"' && value[^1] == '"') || (value[0] == '\'' && value[^1] == '\'')))
        {
            value = value[1..^1];
        }
        return new KeyValuePair<string, string>(key, value);
    }

    /// <summary>
    /// 把指向旧目录的绝对路径改到当前 root。只改「结尾与预期相对路径一致」的值，
    /// 用户自己指到别处的自定义路径不动。返回是否有改动。
    /// </summary>
    public bool NormalizePathsToRoot(string root)
    {
        var map = new (string Key, string Relative)[]
        {
            ("WB_AUTH_DIR", @"upstream\auths"),
            ("WB_UPSTREAM_CONFIG", @"upstream\config.json"),
            ("WB_UPSTREAM_DIR", @"upstream"),
            ("WB2API_START_SCRIPT", @"upstream\start-workbuddy2api.cmd"),
            ("WB2API_STOP_SCRIPT", @"upstream\stop-workbuddy2api.cmd"),
            ("WB2API_LOG_FILE", @"upstream\data\server.err.log"),
            ("WB_DATA_DIR", @"data"),
            ("WB_DB", @"data\manager.db"),
            ("WB_USERS_FILE", @"data\users.json"),
            ("WB_STATIC_DIR", @"web\out"),
        };

        var changed = false;
        foreach (var (key, relative) in map)
        {
            var current = Get(key);
            if (string.IsNullOrWhiteSpace(current)) continue;
            if (!Path.IsPathRooted(current)) continue;

            var expected = Path.Combine(root, relative);
            if (string.Equals(Path.GetFullPath(current), Path.GetFullPath(expected),
                    StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            // 只接管「同一结构、只是根目录变了」的值（例如文件夹改名之后）
            if (!EndsWithRelative(current, relative)) continue;
            changed |= Set(key, expected);
        }
        return changed;
    }

    private static bool EndsWithRelative(string value, string relative)
    {
        var normalized = value.Replace('/', '\\').TrimEnd('\\');
        var rel = relative.TrimEnd('\\');
        return normalized.EndsWith("\\" + rel, StringComparison.OrdinalIgnoreCase)
            || normalized.Equals(rel, StringComparison.OrdinalIgnoreCase);
    }
}
