using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Windows.Forms;

namespace WorkBuddyLauncher
{
    /// 安装向导风格的头部横幅：蓝色渐变 + 标题/副标题（向导窗口与控制窗口共用）。
    internal sealed class BannerPanel : Panel
    {
        private readonly Label _title = new Label();
        private readonly Label _subtitle = new Label();

        public BannerPanel(string title, string subtitle, int height)
        {
            Dock = DockStyle.Top;
            Height = height;

            // 面板尚未挂到窗体时 Font 会落到英文默认字体（中文回退宋体），
            // 这里显式取系统中文字体，保证横幅文字与页面正文一致
            FontFamily uiFamily = SystemFonts.MessageBoxFont.FontFamily;

            _title.BackColor = Color.Transparent;
            _title.ForeColor = Color.White;
            _title.Font = new Font(uiFamily, 12.5f, FontStyle.Bold);
            _title.SetBounds(24, (height - 44) / 2, Width, 26);
            _title.Text = title;
            Controls.Add(_title);

            _subtitle.BackColor = Color.Transparent;
            _subtitle.ForeColor = Color.FromArgb(222, 240, 253);
            _subtitle.Font = new Font(uiFamily, 9f);
            _subtitle.SetBounds(_title.Left, _title.Bottom + 2, Width, 18);
            _subtitle.Text = subtitle;
            Controls.Add(_subtitle);

            Resize += delegate
            {
                _title.Width = Width - _title.Left - 16;
                _subtitle.Width = _title.Width;
            };
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            base.OnPaint(e);
            using (var brush = new LinearGradientBrush(ClientRectangle,
                Color.FromArgb(56, 114, 176), Color.FromArgb(118, 172, 222),
                LinearGradientMode.Horizontal))
            {
                e.Graphics.FillRectangle(brush, ClientRectangle);
            }
        }
    }
}
