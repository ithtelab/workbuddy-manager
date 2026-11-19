using System;
using System.Drawing;
using System.Windows.Forms;

namespace WorkBuddyLauncher
{
    /// JDK 式安装向导骨架：顶部渐变横幅 + 白色内容区 + 底部「上一步/下一步/取消」导航条。
    /// 页面由子类以 Panel 形式挂进内容区，用 ShowPage 切换。
    internal abstract class WizardForm : Form
    {
        private readonly Panel _content = new Panel();
        private readonly Panel _footer = new Panel();
        protected readonly BannerPanel Banner;
        protected Button BackButton = new Button();
        protected Button NextButton = new Button();
        protected Button BtnCancel = new Button();

        protected int ContentWidth
        {
            get { return _content.ClientSize.Width; }
        }

        protected WizardForm(string bannerTitle, string bannerSubtitle)
        {
            Text = bannerTitle;
            Icon = AppIcon.Value;
            Font = SystemFonts.MessageBoxFont;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            MinimizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(504, 400);
            BackColor = Color.White;

            _content.Dock = DockStyle.Fill;
            _content.BackColor = Color.White;
            Controls.Add(_content);

            _footer.Dock = DockStyle.Bottom;
            _footer.Height = 48;
            _footer.BackColor = SystemColors.Control;
            _footer.Paint += delegate(object sender, PaintEventArgs e)
            {
                using (var pen = new Pen(Color.FromArgb(210, 214, 219)))
                {
                    e.Graphics.DrawLine(pen, 0, 0, _footer.Width, 0);
                }
            };
            Controls.Add(_footer);

            BackButton.Text = "< 上一步(&B)";
            BackButton.SetBounds(250, 10, 88, 27);
            BackButton.Click += delegate { OnBack(); };
            _footer.Controls.Add(BackButton);

            NextButton.Text = "下一步(&N) >";
            NextButton.SetBounds(346, 10, 88, 27);
            NextButton.Click += delegate { OnNext(); };
            _footer.Controls.Add(NextButton);

            BtnCancel.Text = "取消";
            BtnCancel.SetBounds(442, 10, 56, 27);
            BtnCancel.Click += delegate { OnCancel(); };
            _footer.Controls.Add(BtnCancel);

            Banner = new BannerPanel(bannerTitle, bannerSubtitle, 92);
            Controls.Add(Banner);

            AcceptButton = NextButton;
            CancelButton = BtnCancel;
        }

        /// 取消：Application.Run 模式下设置 DialogResult 不会自动关窗，必须显式 Close。
        protected virtual void OnCancel()
        {
            DialogResult = DialogResult.Cancel;
            Close();
        }

        /// 新建一个挂进内容区的页面（大小随内容区，初始隐藏）。
        protected Panel MakePage()
        {
            var page = new Panel();
            page.Location = new Point(0, 0);
            page.Size = _content.ClientSize;
            page.BackColor = Color.White;
            page.Visible = false;
            _content.Controls.Add(page);
            return page;
        }

        /// 切换显示某个页面。
        protected void ShowPage(Control page)
        {
            foreach (Control child in _content.Controls)
            {
                child.Visible = child == page;
            }
        }

        /// 页内标题文字（向导各页的左上小标题）。
        protected Label MakePageTitle(Control page, string text, int y)
        {
            var label = new Label();
            label.Font = new Font(Font.FontFamily, 11.5f, FontStyle.Bold);
            label.SetBounds(28, y, ContentWidth - 56, 30);
            label.Text = text;
            page.Controls.Add(label);
            return label;
        }

        /// 页内正文文字。
        protected Label MakePageText(Control page, string text, int y, int height)
        {
            var label = new Label();
            label.SetBounds(28, y, ContentWidth - 56, height);
            label.Text = text;
            page.Controls.Add(label);
            return label;
        }

        protected abstract void OnBack();

        protected abstract void OnNext();
    }
}
