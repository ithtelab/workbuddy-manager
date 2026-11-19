using System;
using System.Drawing;
using System.Windows.Forms;

namespace WorkBuddyLauncher
{
    /// 点 X 时的选择对话框：最小化到托盘 / 退出程序 / 取消，可勾选记住选择。
    internal sealed class CloseDialog : Form
    {
        private readonly CheckBox _chkRemember;

        /// 「下次不再询问」勾选状态（Yes=最小化，No=退出，Cancel=取消）。
        public bool RememberChoice
        {
            get { return _chkRemember.Checked; }
        }

        public CloseDialog()
        {
            Text = "关闭 " + LauncherPaths.AppName;
            Icon = AppIcon.Value;
            Font = SystemFonts.MessageBoxFont;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            MinimizeBox = false;
            ShowInTaskbar = false;
            StartPosition = FormStartPosition.CenterParent;
            ClientSize = new Size(400, 156);

            var label = new Label();
            label.Text = "关闭窗口时希望执行什么操作？";
            label.SetBounds(16, 16, 370, 22);
            Controls.Add(label);

            var btnMinimize = new Button();
            btnMinimize.Text = "最小化到托盘";
            btnMinimize.SetBounds(16, 50, 150, 30);
            btnMinimize.DialogResult = DialogResult.Yes;
            Controls.Add(btnMinimize);

            var btnExit = new Button();
            btnExit.Text = "退出程序";
            btnExit.SetBounds(174, 50, 110, 30);
            btnExit.DialogResult = DialogResult.No;
            Controls.Add(btnExit);

            var btnCancel = new Button();
            btnCancel.Text = "取消";
            btnCancel.SetBounds(292, 50, 90, 30);
            btnCancel.DialogResult = DialogResult.Cancel;
            Controls.Add(btnCancel);

            _chkRemember = new CheckBox();
            _chkRemember.Text = "记住我的选择，下次不再询问";
            _chkRemember.SetBounds(16, 96, 370, 22);
            Controls.Add(_chkRemember);

            var hint = new Label();
            hint.Text = "最小化到托盘后，项目服务继续在后台运行。";
            hint.ForeColor = SystemColors.GrayText;
            hint.SetBounds(16, 122, 370, 20);
            Controls.Add(hint);

            AcceptButton = btnMinimize;
            CancelButton = btnCancel;
        }
    }
}
