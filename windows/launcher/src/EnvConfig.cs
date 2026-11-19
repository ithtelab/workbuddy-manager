using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.RegularExpressions;

namespace WorkBuddyLauncher
{
    /// .env 中管理端监听地址（WB_MANAGER_HOST / WB_MANAGER_PORT）的读写与校验。
    internal static class EnvConfig
    {
        public const string DefaultHost = "127.0.0.1";
        public const int DefaultPort = 7864;

        private static readonly Regex HostLine = new Regex(@"^\s*WB_MANAGER_HOST\s*=\s*(\S+)");
        private static readonly Regex PortLine = new Regex(@"^\s*WB_MANAGER_PORT\s*=\s*(\S+)");

        public static void ReadHostPort(string envPath, out string host, out int port)
        {
            host = DefaultHost;
            port = DefaultPort;
            if (!File.Exists(envPath)) { return; }
            foreach (string line in File.ReadAllLines(envPath, Encoding.UTF8))
            {
                Match m = HostLine.Match(line);
                if (m.Success)
                {
                    host = m.Groups[1].Value;
                    continue;
                }
                m = PortLine.Match(line);
                if (m.Success)
                {
                    int parsed;
                    if (int.TryParse(m.Groups[1].Value, out parsed)) { port = parsed; }
                }
            }
        }

        public static bool TryValidateHost(string host, out string error)
        {
            error = null;
            if (string.IsNullOrWhiteSpace(host))
            {
                error = "IP 地址不能为空。";
                return false;
            }
            host = host.Trim();
            if (string.Equals(host, "localhost", StringComparison.OrdinalIgnoreCase)) { return true; }
            IPAddress parsed;
            if (!IPAddress.TryParse(host, out parsed))
            {
                error = "IP 地址格式不正确（示例：127.0.0.1、0.0.0.0 或 ::）";
                return false;
            }
            return true;
        }

        public static bool TryValidatePort(string text, out int port, out string error)
        {
            error = null;
            if (!int.TryParse((text ?? string.Empty).Trim(), out port) || port < 1 || port > 65535)
            {
                error = "端口必须是 1–65535 之间的整数。";
                return false;
            }
            return true;
        }

        /// 写回 .env：逐行替换已有配置，缺失则追加；其余内容与格式保持原样。
        public static void WriteHostPort(string envPath, string host, int port)
        {
            var lines = new List<string>();
            if (File.Exists(envPath)) { lines.AddRange(File.ReadAllLines(envPath, Encoding.UTF8)); }

            bool hostSet = false;
            bool portSet = false;
            for (int i = 0; i < lines.Count; i++)
            {
                if (HostLine.IsMatch(lines[i]))
                {
                    lines[i] = "WB_MANAGER_HOST=" + host;
                    hostSet = true;
                }
                else if (PortLine.IsMatch(lines[i]))
                {
                    lines[i] = "WB_MANAGER_PORT=" + port;
                    portSet = true;
                }
            }
            if (!hostSet) { lines.Add("WB_MANAGER_HOST=" + host); }
            if (!portSet) { lines.Add("WB_MANAGER_PORT=" + port); }

            File.WriteAllLines(envPath, lines.ToArray(), new UTF8Encoding(false));
        }

        /// 探测与打开浏览器用的主机：把监听地址归一化为**本机可连接**的地址。
        ///
        /// 通配地址不能拿去连：`0.0.0.0` 与 `::` 只是绑定语义，拼进 URL 就是
        /// `http://:::7864` 这种打不开的地址。两者都归到 127.0.0.1 —— `::` 由
        /// server/serve.py 绑成**双栈**（关掉 Windows 默认的 v6only），IPv4 回环
        /// 必然可连。
        ///
        /// 规则：
        ///   · `0.0.0.0` / `*`  → 127.0.0.1
        ///   · `::` / `::0`     → 127.0.0.1（双栈监听，IPv4 可达）
        ///   · `localhost` / 空 → 127.0.0.1
        ///   · 其余 IPv6 地址   → 加方括号（URL 与 HttpWebRequest 的要求）
        ///   · 其余 IPv4 地址   → 原样
        /// <summary>
        /// 监听地址是否属于「所有网卡」的通配写法。
        ///
        /// 用来把界面上的本机地址补一句「监听所有网卡」——否则用户看到
        /// `http://127.0.0.1:7864/` 会以为 0.0.0.0 没生效（issue #368 的现场）。
        /// </summary>
        public static bool IsWildcard(string host)
        {
            string bare = StripBrackets((host ?? string.Empty).Trim());
            return bare == "0.0.0.0" || bare == "*" || bare == "::" || bare == "::0";
        }

        public static string ToConnectHost(string host)
        {
            host = (host ?? string.Empty).Trim();
            if (host.Length == 0) { return DefaultHost; }
            string bare = StripBrackets(host);

            if (bare == "0.0.0.0" || bare == "*") { return "127.0.0.1"; }
            if (bare == "::" || bare == "::0") { return "127.0.0.1"; }
            if (string.Equals(bare, "localhost", StringComparison.OrdinalIgnoreCase)) { return "127.0.0.1"; }

            IPAddress parsed;
            if (IPAddress.TryParse(bare, out parsed) && parsed.AddressFamily == AddressFamily.InterNetworkV6)
            {
                // 已带方括号的保持原样，避免套两层
                return host[0] == '[' ? host : "[" + bare + "]";
            }
            return bare;
        }

        /// 去掉 `[::1]` 这种 URL 形态的方括号，便于交给 IPAddress 解析。
        public static string StripBrackets(string host)
        {
            host = (host ?? string.Empty).Trim();
            if (host.Length >= 2 && host[0] == '[' && host[host.Length - 1] == ']')
            {
                return host.Substring(1, host.Length - 2);
            }
            return host;
        }

        /// 监听地址 → 试绑定用的 IP（端口占用判断用）。
        /// 通配地址必须映射到对应的 Any（否则「服务监听 0.0.0.0，却去试绑 127.0.0.1」
        /// 会误判成端口空闲）。
        public static IPAddress ToBindAddress(string host)
        {
            string bare = StripBrackets(host);
            if (bare.Length == 0) { return IPAddress.Loopback; }
            if (bare == "0.0.0.0" || bare == "*") { return IPAddress.Any; }
            if (bare == "::" || bare == "::0") { return IPAddress.IPv6Any; }
            if (string.Equals(bare, "localhost", StringComparison.OrdinalIgnoreCase)) { return IPAddress.Loopback; }
            IPAddress parsed;
            if (IPAddress.TryParse(bare, out parsed)) { return parsed; }
            return IPAddress.Loopback;
        }
    }
}
