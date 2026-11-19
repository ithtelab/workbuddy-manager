"""原生 workbuddy2api 的运行时兼容。"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.services import updater, wb2api  # noqa: E402


class _Process:
    returncode = 0

    async def communicate(self):
        return b'', b''


class NativeUpstreamRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_restart_uses_native_stop_and_start_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            start = root / 'start-workbuddy2api.cmd'
            stop = root / 'stop-workbuddy2api.cmd'
            start.touch()
            stop.touch()
            calls: list[tuple] = []

            async def fake_exec(*args, **kwargs):
                calls.append(args)
                return _Process()

            with mock.patch.object(config, 'WB2API_MODE', 'native'), \
                    mock.patch.object(config, 'WB2API_START_SCRIPT', start), \
                    mock.patch.object(config, 'WB2API_STOP_SCRIPT', stop), \
                    mock.patch.object(asyncio, 'create_subprocess_exec', side_effect=fake_exec):
                ok, message = await wb2api.restart_container()

            self.assertTrue(ok, message)
            self.assertEqual(len(calls), 2, calls)
            self.assertEqual(Path(calls[0][-1]), stop)
            self.assertEqual(Path(calls[1][-1]), start)
            self.assertIn('原生', message)

    def test_logs_prefer_native_log_file_and_tail_limit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / 'server.log'
            log.write_text('one\ntwo\nthree\n', encoding='utf-8')
            with mock.patch.object(config, 'WB2API_MODE', 'native'), \
                    mock.patch.object(config, 'WB2API_LOG_FILE', log), \
                    mock.patch('subprocess.run') as docker:
                lines = wb2api.read_container_logs(limit=2)

            self.assertEqual(lines, ['two', 'three'])
            docker.assert_not_called()

    async def test_hanging_script_times_out_and_is_killed(self) -> None:
        """脚本挂住时必须**超时返回失败**，不能永远等下去。

        评审发现：原实现是裸 `await proc.communicate()`，没有任何超时。脚本一旦挂住
        （等交互输入、端口被占用、启动时卡在依赖上），这个协程就永不返回，而 reload
        的状态机会一直停在 `running=True` —— 后续所有「保存配置后自动重载」都
        **静默失效**（不报错、不重试），用户只会觉得「改了配置没生效」。

        所以这里钉两件事：超时后返回失败（而不是挂住），且进程确实被结束掉
        （否则会留下孤儿进程继续占着端口）。
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            start = root / 'start.cmd'
            stop = root / 'stop.cmd'
            start.touch()
            stop.touch()
            killed = {'yes': False}

            class _HangProc:
                returncode = None

                def kill(self):
                    killed['yes'] = True

                async def communicate(self):
                    await asyncio.sleep(30)      # 模拟永不结束

            async def fake_exec(*args, **kwargs):
                return _HangProc()

            with mock.patch.object(config, 'WB2API_MODE', 'native'), \
                    mock.patch.object(config, 'WB2API_START_SCRIPT', start), \
                    mock.patch.object(config, 'WB2API_STOP_SCRIPT', stop), \
                    mock.patch.object(wb2api, '_NATIVE_RESTART_TIMEOUT', 0.2), \
                    mock.patch.object(asyncio, 'create_subprocess_exec', side_effect=fake_exec):
                ok, message = await asyncio.wait_for(wb2api.restart_container(), timeout=10)

            self.assertFalse(ok, '脚本挂住却报成功')
            self.assertIn('未结束', message)
            self.assertTrue(killed['yes'], '超时后没有结束子进程（会留下孤儿进程）')

    def test_upstream_dir_has_a_single_source(self) -> None:
        """上游目录只能有一处推导口径（评审发现两处会分歧）。

        评审发现 `config.UPSTREAM_DIR`（native 模式新增，回退到 config.json 所在目录）
        与 `updater._upstream_dir()`（原有，回退到 auths 的父目录）是**两套独立推导**。
        默认配置下巧合一致，但只要用户单独调整 `WB_AUTH_DIR` 或 `WB_UPSTREAM_CONFIG`
        中的一个，两者就指向不同目录，且没有任何报错：

          · 原生启停脚本按 config 那份找；
          · 任务脚本与「更新上游」按 updater 那份找。

        结果是一半功能落在 A 目录、另一半落在 B 目录。这条测试钉住「只有一份口径」。
        """
        # 让两个来源指向不同目录，看 updater 是否仍然跟随 config
        with mock.patch.object(config, 'UPSTREAM_DIR', Path('/fake/upstream/from-config')):
            self.assertEqual(updater._upstream_dir(), Path('/fake/upstream/from-config'),
                             'updater 没有复用 config.UPSTREAM_DIR —— 又变成两套口径了')

    def test_shipped_script_templates_exist(self) -> None:
        """文档让用户指向的启停脚本，仓库里必须真的提供模板（评审发现）。

        评审发现：README 与 .env.example 都让用户把 WB2API_START_SCRIPT /
        WB2API_STOP_SCRIPT 指向 `start-workbuddy2api.cmd` / `stop-workbuddy2api.cmd`，
        但**上游只发布 Docker 部署、没有这两个脚本**，我们此前也没提供 ——
        用户照着文档配置会直接得到「未找到原生启停脚本」，而且不知道该写什么。

        现在 deploy/windows-native/ 提供了一对可直接改用的模板。这条测试钉住
        「模板还在 + 文档仍然指向它们」，避免哪天被当成无用文件删掉。
        """
        root = Path(__file__).resolve().parents[2]
        tpl = root / 'deploy' / 'windows-native'
        for name in ('start-workbuddy2api.cmd', 'stop-workbuddy2api.cmd', 'README.md'):
            with self.subTest(name=name):
                self.assertTrue((tpl / name).is_file(),
                                f'缺少 {name} —— 文档让用户指向它，却没有模板可用')

        # 文档仍要能走到那个目录，否则模板等于没提供。
        # 链路随文档重构调整过一次：Windows 原生那一节从 README 搬进了部署指南
        # （README 精简成落地页，只留入口），所以现在检查的是整条链路：
        #   README → deploy/README.md → deploy/windows-native/
        for doc in ('README.md', 'README.en.md'):
            with self.subTest(doc=doc):
                text = (root / doc).read_text(encoding='utf-8')
                self.assertIn('deploy/README.md', text,
                              f'{doc} 没有指向部署指南（Windows 原生那节在里面）')
        deploy_doc = (root / 'deploy' / 'README.md').read_text(encoding='utf-8')
        self.assertIn('windows-native', deploy_doc,
                      '部署指南没有指向启停脚本模板目录（模板等于没提供）')

    def test_start_template_returns_immediately(self) -> None:
        """启动脚本模板必须用 `start /b` 那种后台方式 —— 前台运行会让重启超时。

        管理端的语义是「调用脚本 → 等它结束 → 认为重启完成」。模板若在前台一直跑
        `wb2api.exe`，管理端会等到 60 秒超时才报失败，而上游其实已经起来了 ——
        这种「功能其实正常、界面报错」的形态最容易让人误判。
        """
        root = Path(__file__).resolve().parents[2]
        text = (root / 'deploy' / 'windows-native' / 'start-workbuddy2api.cmd').read_text(
            encoding='utf-8')
        self.assertIn('start ', text, '启动模板没有用 start 后台拉起')
        self.assertIn('/b', text, '启动模板没有用 /b（后台，不新开窗口）')
        # 日志要落到文件，否则「任务记录」采集不到
        self.assertIn('server.err.log', text, '启动模板没有把日志重定向到文件')

    def test_stop_template_tolerates_not_running(self) -> None:
        """停止模板在「进程本来就没跑」时要返回成功。

        否则管理端重启流程会卡在这个前置步骤上整体失败 —— 而上游没在跑
        正是重启前的正常状态。

        断言的是**行为**（先探测再退出 0、且有可读提示），不是某个具体的中文
        字符串：批处理脚本必须保持纯 ASCII（cmd.exe 按系统 ANSI 代码页解析，
        非 ASCII 注释会让脚本解析错乱，见 test_windows_scripts.py），因此提示
        文案只能是英文。锚在 'is not running' 上，改文案时会一起提醒更新这里。
        """
        root = Path(__file__).resolve().parents[2]
        text = (root / 'deploy' / 'windows-native' / 'stop-workbuddy2api.cmd').read_text(
            encoding='utf-8')
        self.assertIn('is not running', text, '停止模板没有处理「本来就没跑」的情况')
        self.assertIn('exit /b 0', text, '停止模板在未运行时没有返回成功')
        # 「先探测、再决定」的结构：探测必须在退出之前，否则会把没在跑的当失败
        probe = text.index('tasklist')
        not_running = text.index('is not running')
        self.assertLess(probe, not_running, '没有先探测进程就断言「没在运行」')

    def test_native_restart_reports_missing_scripts(self) -> None:
        """脚本不存在时要明确报出缺哪个（而不是等到执行才报个含糊错误）。"""
        async def main():
            with mock.patch.object(config, 'WB2API_MODE', 'native'), \
                    mock.patch.object(config, 'WB2API_START_SCRIPT', Path('/nope/start.cmd')), \
                    mock.patch.object(config, 'WB2API_STOP_SCRIPT', Path('/nope/stop.cmd')):
                return await wb2api.restart_container()

        ok, message = asyncio.run(main())
        self.assertFalse(ok)
        self.assertIn('未找到原生启停脚本', message)
        self.assertIn('stop.cmd', message)

    def test_windows_rejects_linux_only_one_click_update(self) -> None:
        with mock.patch.object(updater.os, 'name', 'nt'), \
                mock.patch.object(config, 'WB2API_MODE', 'native'), \
                mock.patch.object(updater, '_lock_active', return_value=True):
            ok, message = updater.start_update('manager')

        self.assertFalse(ok)
        self.assertIn('Windows', message)
        self.assertNotIn('已有更新任务', message)


if __name__ == '__main__':
    unittest.main()
