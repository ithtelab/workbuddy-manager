// 删除规则的探针（dev/test-delete-rules.ps1 会编译并运行它）。
//
// 为什么要有：卸载是**不可逆**的动作，而它的失效方式恰恰是「看起来一切正常」——
// issue #178 就是这么来的：程序装进了用户自己的 D:\workspace，卸载把那个目录连同
// 用户的资料一起删了。这条规则（只删安装清单里登记的 + 程序自有目录）必须能脱离
// 图形界面反复验证。
//
// 探针只调用 Maintenance.DeleteProgramFiles 与 InstallManifest，不碰快捷方式、
// 注册表、服务——那些不在本文件的验证范围内。
using System;
using System.Collections.Generic;
using System.IO;

namespace WorkBuddyLauncher
{
    internal static class DeleteRulesProbe
    {
        private static int _failed;

        private static void Check(bool ok, string label)
        {
            Console.WriteLine((ok ? "  OK   " : "  FAIL ") + label);
            if (!ok) { _failed++; }
        }

        private static void Write(string path, string text)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            File.WriteAllText(path, text);
        }

        private static string NewDir(string root, string name)
        {
            string dir = Path.Combine(root, name);
            Directory.CreateDirectory(dir);
            return dir;
        }

        public static int Main(string[] args)
        {
            string root = Path.Combine(Path.GetTempPath(), "wb-delete-rules-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(root);
            Console.WriteLine("临时根目录: " + root);

            try
            {
                RunScenarios(root);
            }
            finally
            {
                try { Directory.Delete(root, true); } catch { }
            }

            Console.WriteLine();
            if (_failed > 0)
            {
                Console.WriteLine("FAILED: " + _failed + " 项不符合预期");
                return 1;
            }
            Console.WriteLine("ALL CHECKS PASSED");
            return 0;
        }

        private static void RunScenarios(string root)
        {
            // ── 场景 1：真实安装目录 + 用户自己的文件（issue #178 的复现形状）──
            Console.WriteLine();
            Console.WriteLine("[1] 卸载只删自己的文件，用户资料必须活着");
            string inst = NewDir(root, "install1");
            Write(Path.Combine(inst, "server", "main.py"), "version='9.9.9'\n");
            Write(Path.Combine(inst, "windows", "start.cmd"), "@echo off\n");
            Write(Path.Combine(inst, "WorkBuddyManager.exe"), "MZ\n");
            Write(Path.Combine(inst, ".env"), "WB_MANAGER_PORT=7864\n");
            Write(Path.Combine(inst, "data", "manager.db"), "sqlite\n");
            InstallManifest.Write(inst, new[]
            {
                "server\\main.py", "windows\\start.cmd", "WorkBuddyManager.exe",
            });
            // 用户自己的东西（跟程序无关，装在了同一个目录里）
            Write(Path.Combine(inst, "我的资料.docx"), "user data\n");
            Write(Path.Combine(inst, "notes", "readme.txt"), "user notes\n");

            Maintenance.DeletionReport report = Maintenance.DeleteProgramFiles(inst, false, null, null);
            Check(report.Refused == null, "不是被拒绝（这个目录确实是安装根）");
            Check(!File.Exists(Path.Combine(inst, "server", "main.py")), "程序文件（清单里的）被删掉");
            Check(!File.Exists(Path.Combine(inst, "WorkBuddyManager.exe")), "启动器被删掉");
            Check(!File.Exists(Path.Combine(inst, ".env")), "不保留数据时 .env 被删掉");
            Check(File.Exists(Path.Combine(inst, "我的资料.docx")), "★ 用户文档活着");
            Check(File.Exists(Path.Combine(inst, "notes", "readme.txt")), "★ 用户子目录里的文件活着");
            Check(Directory.Exists(Path.Combine(inst, "notes")), "★ 含用户文件的目录本身不被删除");
            Check(Directory.Exists(inst), "★ 安装根目录不被删除");
            Check(report.KeptFiles == 2, "报告里如实记下保留了 2 个文件（实际 " + report.KeptFiles + "）");

            // ── 场景 2：保留数据模式 ──
            Console.WriteLine();
            Console.WriteLine("[2] 保留数据模式：数据活着、程序文件走人、用户文件也活着");
            string inst2 = NewDir(root, "install2");
            Write(Path.Combine(inst2, "server", "main.py"), "x\n");
            Write(Path.Combine(inst2, ".env"), "port\n");
            Write(Path.Combine(inst2, "data", "manager.db"), "db\n");
            Write(Path.Combine(inst2, "upstream", "auths", "workbuddy-a.json"), "{}\n");
            Write(Path.Combine(inst2, "user.txt"), "mine\n");
            InstallManifest.Write(inst2, new[] { "server\\main.py" });

            Maintenance.DeleteProgramFiles(inst2, true, null, null);
            Check(!File.Exists(Path.Combine(inst2, "server", "main.py")), "程序文件被删");
            Check(File.Exists(Path.Combine(inst2, ".env")), "保留数据时 .env 活着");
            Check(File.Exists(Path.Combine(inst2, "data", "manager.db")), "保留数据时 data/ 活着");
            Check(File.Exists(Path.Combine(inst2, "upstream", "auths", "workbuddy-a.json")), "保留数据时账号文件活着");
            Check(File.Exists(Path.Combine(inst2, "user.txt")), "★ 用户文件照样活着");

            // ── 场景 3：不是安装目录 → 拒绝，一个字节都不动 ──
            Console.WriteLine();
            Console.WriteLine("[3] 在别人的目录里点卸载：拒绝，且不删任何文件");
            string foreign = NewDir(root, "workspace");
            Write(Path.Combine(foreign, "毕业论文.docx"), "precious\n");
            Write(Path.Combine(foreign, "src", "main.c"), "int main(){}\n");

            Maintenance.DeletionReport refused = Maintenance.DeleteProgramFiles(foreign, false, null, null);
            Check(refused.Refused != null, "返回了拒绝原因");
            Check(refused.Deleted == 0, "一个文件都没删");
            Check(File.Exists(Path.Combine(foreign, "毕业论文.docx")), "★ 用户文档还在");
            Check(File.Exists(Path.Combine(foreign, "src", "main.c")), "★ 用户的代码还在");

            // ── 场景 4：清单登记的、但不在自有目录白名单里的文件 ──
            Console.WriteLine();
            Console.WriteLine("[4] 清单里登记过的文件（即使放在自有目录之外）也会被清掉");
            string inst4 = NewDir(root, "install4");
            Write(Path.Combine(inst4, "server", "main.py"), "x\n");
            Write(Path.Combine(inst4, "custom", "extra.bin"), "app file\n");
            Write(Path.Combine(inst4, "custom", "keep.bin"), "user file\n");
            InstallManifest.Write(inst4, new[] { "server\\main.py", "custom\\extra.bin" });

            Maintenance.DeleteProgramFiles(inst4, false, null, null);
            Check(!File.Exists(Path.Combine(inst4, "custom", "extra.bin")), "清单里的 extra.bin 被删");
            Check(File.Exists(Path.Combine(inst4, "custom", "keep.bin")), "★ 没登记的同目录文件被保留");

            // ── 场景 5：连清单都没了（老版本装的）→ 只靠自有目录白名单 ──
            Console.WriteLine();
            Console.WriteLine("[5] 没有清单时：自有目录照删，别人的文件照留");
            string inst5 = NewDir(root, "install5");
            Write(Path.Combine(inst5, "server", "main.py"), "x\n");
            Write(Path.Combine(inst5, "web", "out", "index.html"), "<html>\n");
            Write(Path.Combine(inst5, "家庭照片.jpg"), "photo\n");

            Maintenance.DeleteProgramFiles(inst5, false, null, null);
            Check(!File.Exists(Path.Combine(inst5, "web", "out", "index.html")), "自有目录 web/ 被清理");
            Check(File.Exists(Path.Combine(inst5, "家庭照片.jpg")), "★ 用户文件仍然安全");
        }
    }
}
