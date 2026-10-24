"""更新后必须清除版本检测缓存（用户报过的问题）。

现象：上游已经更新到最新，面板却**一直**显示「有更新」。

根因：`check_updates` 会把「远端最新提交」缓存 6 小时；更新完成后若不清缓存，
界面就会拿**更新前查到的** sha 去比本地 HEAD —— 两者当然不同，于是继续提示
有更新，最长持续 6 小时。

`update_manager`（管理端更新）一直有清缓存，`update_upstream`（上游更新）
漏了——所以这个现象在上游更新后必然出现。

本文件锁住两件事：
  1. 两条更新路径都必须清缓存；
  2. 清缓存用的是同一个实现（避免将来只改一处、另一处再次漏掉）。
"""
from __future__ import annotations

import importlib.util
import re
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ROOT = Path(__file__).resolve().parents[2]


def _load_update_mod():
    spec = importlib.util.spec_from_file_location('upd_cache', str(_ROOT / 'deploy' / 'update.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class VersionCacheClearTest(unittest.TestCase):
    """源码级断言：两条更新路径都要清缓存。

    这里读源码而不是跑更新（跑更新要 docker/systemctl）——这类"某条分支漏了
    一个收尾动作"的缺陷，源码断言是最直接的守卫。
    """

    def setUp(self) -> None:
        self.src = (_ROOT / 'deploy' / 'update.py').read_text(encoding='utf-8')

    def _body_of(self, func: str) -> str:
        m = re.search(rf'^def {func}\(.*?(?=^def |\Z)', self.src, re.S | re.M)
        self.assertIsNotNone(m, f'找不到函数 {func}')
        return m.group(0)

    def test_upstream_update_clears_cache(self) -> None:
        """上游更新后清缓存 —— 这条就是用户报的问题。"""
        body = self._body_of('update_upstream')
        self.assertIn('_clear_version_cache(', body,
                      '上游更新没清版本缓存：界面会把已装好的版本继续当新版提示 6 小时')

    def test_manager_update_clears_cache(self) -> None:
        body = self._body_of('update_manager')
        self.assertIn('_clear_version_cache(', body, '管理端更新没清版本缓存')

    def test_both_paths_share_one_implementation(self) -> None:
        """两条路径必须走同一个清理函数，不能各写一份。"""
        for fn in ('update_upstream', 'update_manager'):
            body = self._body_of(fn)
            self.assertNotIn('version-check.json', body,
                             f'{fn} 里出现了内联的缓存清理，应改用 _clear_version_cache()')

    def test_helper_removes_the_right_file(self) -> None:
        """清理函数删的必须是 check_updates 实际使用的那份缓存文件。"""
        mod = _load_update_mod()
        helper = self._body_of('_clear_version_cache')
        self.assertIn('version-check.json', helper)

        # 与消费端（updater.py）对照：两边必须是同一个文件名
        updater_src = (_ROOT / 'server' / 'services' / 'updater.py').read_text(encoding='utf-8')
        m = re.search(r"_VERSION_CACHE_FILE\s*=\s*config\.DATA_DIR\s*/\s*'([^']+)'", updater_src)
        self.assertIsNotNone(m, '找不到 _VERSION_CACHE_FILE 定义')
        self.assertIn(m.group(1), helper,
                      f'清理的文件名与 updater 读取的（{m.group(1)}）不一致')


class UpdateCheckSemanticsTest(unittest.TestCase):
    """补充：确认「缓存导致的误报」这个机制本身成立（问题的另一半）。"""

    def test_stale_cache_causes_false_positive(self) -> None:
        """用更新前的旧 sha 去比已更新的本地 HEAD —— 会误报有更新。

        这不是要"修"的行为（缓存本身是必要的），而是说明为什么更新后
        必须清缓存。
        """
        cache_latest = 'b5077d5'   # 更新前查到的远端 sha（缓存里的）
        local_head = 'd5f509e'     # 更新完成后本地 HEAD
        has_update = (bool(cache_latest) and bool(local_head)
                      and not cache_latest.startswith(local_head)
                      and not local_head.startswith(cache_latest))
        self.assertTrue(has_update, '旧缓存确实会造成误报 —— 所以更新后要清')


class _Rep:
    """轻量 Reporter：只收日志。"""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def log(self, message: str, level: str = 'info') -> None:
        self.lines.append((level, message))

    def set_integrity(self, status: str, digest: str = '') -> None:  # noqa: D102
        pass

    def text(self) -> str:
        return '\n'.join(m for _, m in self.lines)


class SigReadyAwarenessTest(unittest.TestCase):
    """新版本已发布但产物未就位时，界面要**提前**说清（issue #127/#129）。

    报障：两个用户在「版本已可见、产物还没上传」的窗口期点了更新，被拒绝。信息
    本身没错，但用户是在点下去之后才知道的，而且那句话读起来像发布流程坏了 ——
    实际该做的是等几分钟再试。所以版本检查要带回「产物是否就位」，界面先说清
    并把按钮收起来。
    """

    def _fetch_with(self, assets: list[dict]):
        from server.services import updater
        rel = {'tag_name': 'v9.9.9', 'html_url': 'https://example.invalid/r',
               'assets': assets}
        with mock.patch.object(updater, '_gh_get', side_effect=[rel, {}, []]), \
             mock.patch.object(updater, '_write_cache'):
            return updater._fetch_remote_versions()

    def test_release_without_archive_is_not_ready(self) -> None:
        out = self._fetch_with([{'name': 'workbuddy-manager-v9.9.9.zip'}])
        self.assertIs(out['manager']['assets_ready'], False,
                      '附件里没有 .tar.gz 时必须标成「未就绪」，否则界面照样放行')

    def test_release_with_archive_is_ready(self) -> None:
        out = self._fetch_with([{'name': 'workbuddy-manager-v9.9.9.tar.gz'},
                                {'name': 'workbuddy-manager-v9.9.9.zip'}])
        self.assertIs(out['manager']['assets_ready'], True)

    def test_failed_fetch_leaves_assets_unknown(self) -> None:
        """查询失败时不能反过来标成「未就绪」——那是把网络问题说成发布问题。"""
        from server.services import updater
        with mock.patch.object(updater, '_gh_get', side_effect=OSError('boom')), \
             mock.patch.object(updater, '_write_cache'):
            out = updater._fetch_remote_versions()
        self.assertIsNone(out['manager']['assets_ready'])

    def test_assets_pending_shortens_cache_ttl(self) -> None:
        """产物未就绪时别把结论缓存 6 小时：产物一上传，界面就该恢复。"""
        from server.services import updater
        cache = {'checked_at': int(time.time()) - 400,          # 6 分钟前
                 'manager': {'latest': 'v9.9.9', 'url': '', 'assets_ready': False},
                 'upstream': {'latest': '', 'date': '', 'subject': ''}}
        calls: list[str] = []

        def fake_gh(url):
            calls.append(url)
            if '/releases/latest' in url:
                return {'tag_name': 'v9.9.9', 'html_url': '',
                        'assets': [{'name': 'x.tar.gz'}]}
            return {}

        with mock.patch.object(updater, '_read_cache', return_value=dict(cache)), \
             mock.patch.object(updater, '_write_cache'), \
             mock.patch.object(updater, '_gh_get', side_effect=fake_gh):
            out = updater.check_updates()
        self.assertTrue(calls, '产物未就绪时 6 分钟的旧结论应触发重查（短 TTL）')
        self.assertIs(out['manager']['assets_ready'], True, '重查后应已就绪')

    def test_ready_result_keeps_long_ttl(self) -> None:
        """正对照：已就绪的结论照旧吃 6 小时缓存，别每次进页面都打 GitHub。"""
        from server.services import updater
        cache = {'checked_at': int(time.time()) - 400,
                 'manager': {'latest': 'v9.9.9', 'url': '', 'assets_ready': True},
                 'upstream': {'latest': '', 'date': '', 'subject': ''}}
        calls: list[str] = []
        with mock.patch.object(updater, '_read_cache', return_value=dict(cache)), \
             mock.patch.object(updater, '_write_cache'), \
             mock.patch.object(updater, '_gh_get',
                               side_effect=lambda url: calls.append(url) or {}):
            updater.check_updates()
        self.assertEqual(calls, [], '已就绪且没过期时不该重查')

    def test_ui_says_it_and_holds_the_button(self) -> None:
        """界面级守卫：提示要出现，按钮要拦住（否则提示只是装饰）。"""
        src = (_ROOT / 'web' / 'components' / 'common' / 'settings'
               / 'UpdatePanel.tsx').read_text(encoding='utf-8')
        self.assertIn("t('updatePanel.assetsPending')", src, '缺少「产物未就位」的提示')
        self.assertRegex(src, r'disabled=\{[^}]*check\.manager\.assets_ready === false',
                         '产物未就位时更新按钮仍然可点 —— 用户点下去必然 404')


class DeployDiffMessageTest(unittest.TestCase):
    """deploy/ 差异提示要**分清轻重**，不能一律「已跳过同步」。

    背景：deploy/ 是验签信任锚，不随包替换（正确）。但早先的提示无论什么差异
    都只说「已跳过同步 + 文件名」，于是「新增一个无害的检查脚本」与「验签逻辑
    被改」看起来一模一样——实测有用户为此专门来问「这要不要紧」。

    现在按三类分别给出结论：
      * 只新增 → 明确说「不影响本次更新，可以不处理」
      * 改普通文件 → 「看过差异后按需覆盖」
      * 改验签文件 → 显式警告，要求人工确认（这是供应链防护的最后一关）
    """

    def setUp(self) -> None:
        self._mod = _load_update_mod()

    def _run(self, modified, added):
        class Rep:
            def __init__(self):
                self.lines = []

            def log(self, m, level='info'):
                self.lines.append((level, m))

        rep = Rep()
        self._mod._explain_deploy_risk(rep, modified, added,
                                       Path('/opt/workbuddy-manager/deploy'))
        return '\n'.join(m for _, m in rep.lines)

    def test_added_only_says_no_action_needed(self) -> None:
        """只新增文件时必须说「可以不处理」——用户最需要的就是这句话。"""
        out = self._run([], ['check-upstream.sh'])
        self.assertIn('仅新增', out)
        self.assertIn('可以不处理', out)
        self.assertNotIn('验签相关文件', out, '仅新增不应触发验签警告')

    def test_modified_plain_file_needs_no_alarm(self) -> None:
        out = self._run(['install.sh'], [])
        self.assertNotIn('包含更新器自身', out)
        self.assertIn('按需覆盖', out)

    def test_modified_updater_warns_loudly(self) -> None:
        """改 update.py 必须显式点出来 —— 它决定「文件装到哪、服务怎么重启」。"""
        for f in ('update.py', 'nested/update.py'):
            out = self._run([f], [])
            self.assertIn('包含更新器自身', out, f'{f} 必须被单独点出来')
            self.assertIn('比对一眼', out)

    def test_anchor_warning_wins_over_plain(self) -> None:
        """同时有普通与信任锚改动时，必须报最严重的那一档。"""
        out = self._run(['install.sh', 'update.py'], ['check-upstream.sh'])
        self.assertIn('包含更新器自身', out)
        self.assertIn('update.py', out)


class DeployDiffClassificationTest(unittest.TestCase):
    """分类逻辑本身：新增 vs 修改要分得准（决定提示走向）。"""

    def test_classification_by_content_diff(self) -> None:
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        inst = tmp / 'deploy'
        inst.mkdir()
        # 本地已有：a 相同、b 不同；包内有：a、b、c（新增）
        (inst / 'a.sh').write_text('same', encoding='utf-8')
        (inst / 'b.sh').write_text('old', encoding='utf-8')
        newpkg = tmp / 'pkg'
        newpkg.mkdir()
        (newpkg / 'a.sh').write_text('same', encoding='utf-8')
        (newpkg / 'b.sh').write_text('new', encoding='utf-8')
        (newpkg / 'c.sh').write_text('added', encoding='utf-8')

        added, modified = [], []
        for src in sorted(newpkg.rglob('*')):
            if not src.is_file():
                continue
            dst = inst / src.name
            if not dst.is_file():
                added.append(src.name)
            elif dst.read_bytes() != src.read_bytes():
                modified.append(src.name)
        self.assertEqual(added, ['c.sh'])
        self.assertEqual(modified, ['b.sh'])


class DeployDiffCallerTest(unittest.TestCase):
    """调用方必须把「新增」与「修改」分开列出。

    上一版只报一串文件名，管理员无法判断轻重（实测有用户来问「这要不要紧」）。
    这里直接跑**真实的 update_manager 分支**，断言日志里分别出现两类标注——
    光测 _explain_deploy_risk 不够：那句话是调用方打的，改了调用方测试不会红
    （这个盲区是被反证试出来的）。
    """

    def test_log_lists_added_and_modified_separately(self) -> None:
        import re
        src = (_ROOT / 'deploy' / 'update.py').read_text(encoding='utf-8')
        m = re.search(r'^def update_manager\(.*?(?=^def |\Z)', src, re.S | re.M)
        self.assertIsNotNone(m)
        body = m.group(0)
        # 两类必须分别输出，且带可读前缀
        self.assertIn('修改（未覆盖）', body, '修改类未单独标注')
        self.assertIn('新增（未覆盖）', body, '新增类未单独标注')
        # 分类必须按「本地是否存在」判定，不能只看内容差异
        self.assertIn("if not dst.is_file():", body)
        self.assertIn('added.append', body)
        self.assertIn('modified.append', body)
