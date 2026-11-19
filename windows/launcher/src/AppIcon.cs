using System;
using System.Drawing;
using System.Windows.Forms;

namespace WorkBuddyLauncher
{
    /// 窗口与托盘图标：直接从 exe 自身提取（构建时已嵌入网页图标）。
    internal static class AppIcon
    {
        private static Icon _icon;

        public static Icon Value
        {
            get
            {
                if (_icon == null)
                {
                    try { _icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); }
                    catch { }
                    if (_icon == null) { _icon = SystemIcons.Application; }
                }
                return _icon;
            }
        }
    }
}
