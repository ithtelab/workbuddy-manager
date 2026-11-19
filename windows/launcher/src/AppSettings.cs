using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

namespace WorkBuddyLauncher
{
    /// 启动器偏好存储：key=value 纯文本，落在 %APPDATA%\WorkBuddyManager\launcher.conf。
    internal sealed class AppSettings
    {
        private readonly string _path;
        private readonly Dictionary<string, string> _values =
            new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

        public AppSettings(string path)
        {
            _path = path;
            Load();
        }

        public string Get(string key, string fallback)
        {
            string value;
            return _values.TryGetValue(key, out value) ? value : fallback;
        }

        public void Set(string key, string value)
        {
            _values[key] = value;
        }

        public void Remove(string key)
        {
            _values.Remove(key);
        }

        public void Save()
        {
            Directory.CreateDirectory(Path.GetDirectoryName(_path));
            var lines = new List<string>();
            foreach (KeyValuePair<string, string> pair in _values)
            {
                lines.Add(pair.Key + "=" + pair.Value);
            }
            File.WriteAllLines(_path, lines.ToArray(), new UTF8Encoding(false));
        }

        private void Load()
        {
            if (!File.Exists(_path)) { return; }
            foreach (string line in File.ReadAllLines(_path, Encoding.UTF8))
            {
                int index = line.IndexOf('=');
                if (index > 0)
                {
                    _values[line.Substring(0, index).Trim()] = line.Substring(index + 1).Trim();
                }
            }
        }
    }
}
