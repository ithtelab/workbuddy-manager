"""更新日志解析（CHANGELOG.md → 结构化数据）测试。

界面「更新日志」页依赖这里的解析结果，格式为项目固定的三级结构：
`## [版本] - 日期` / `### 分类` / `- 条目`（两级缩进视为子项）。
"""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server.services import changelog

SAMPLE = """# 更新日志

<!-- 说明 -->

## [未发布]

### 安全

- 修复 **device_token** 明文下发
- 子项示例
  - 这里是子项内容

### 计划中

- 等上游稳定后一起发版

---

## [1.0.15] - 2026-09-13

### 修复

- 请求体上限改为跟随配置 `server.max_body_mb`
  补充的续行说明

---

## [1.0.0] - 2026-08-01

### 新增

- 首个版本
"""


class ParseChangelogTest(unittest.TestCase):
    def test_versions_in_order(self) -> None:
        versions = changelog.parse_changelog(SAMPLE)
        self.assertEqual([v['version'] for v in versions], ['未发布', '1.0.15', '1.0.0'])

    def test_unreleased_flag_and_date(self) -> None:
        versions = changelog.parse_changelog(SAMPLE)
        self.assertTrue(versions[0]['unreleased'])
        self.assertEqual(versions[0]['date'], '')
        self.assertFalse(versions[1]['unreleased'])
        self.assertEqual(versions[1]['date'], '2026-09-13')

    def test_sections_and_items(self) -> None:
        versions = changelog.parse_changelog(SAMPLE)
        unreleased = versions[0]
        self.assertEqual([s['title'] for s in unreleased['sections']], ['安全', '计划中'])
        sec = unreleased['sections'][0]
        self.assertEqual(len(sec['items']), 3)
        self.assertEqual(sec['items'][0]['level'], 0)
        self.assertEqual(sec['items'][0]['text'], '修复 **device_token** 明文下发')
        # 两个空格缩进视为子项
        self.assertEqual(sec['items'][2]['level'], 1)
        self.assertEqual(sec['items'][2]['text'], '这里是子项内容')

    def test_continuation_line_is_joined(self) -> None:
        versions = changelog.parse_changelog(SAMPLE)
        v15 = versions[1]
        item = v15['sections'][0]['items'][0]
        self.assertEqual(item['text'], '请求体上限改为跟随配置 `server.max_body_mb` 补充的续行说明')

    def test_separator_and_top_matter_ignored(self) -> None:
        versions = changelog.parse_changelog(SAMPLE)
        # `---` 与文件头（标题/注释）不应产生版本或条目
        self.assertEqual(len(versions), 3)
        for v in versions:
            for s in v['sections']:
                for it in s['items']:
                    self.assertNotIn('---', it['text'])

    def test_empty_and_garbage_input(self) -> None:
        self.assertEqual(changelog.parse_changelog(''), [])
        self.assertEqual(changelog.parse_changelog(None), [])  # type: ignore[arg-type]
        # 没有版本标题时，正文不归属任何版本
        self.assertEqual(changelog.parse_changelog('- 游离条目\n### 无主分类'), [])


class LoadChangelogTest(unittest.TestCase):
    def test_missing_file_reports_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / 'CHANGELOG.md'
            with mock.patch.object(changelog.config, 'ROOT', Path(tmp)):
                data = changelog.load_changelog()
            self.assertFalse(data['available'])
            self.assertIn('未找到更新日志文件', data['error'])
            self.assertIn(missing.name, data['error'])
            self.assertEqual(data['versions'], [])

    def test_loads_and_limits_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocks = []
            for i in range(changelog._MAX_VERSIONS + 5):
                blocks.append(f'## [1.{i}.0] - 2026-01-01\n\n### 新增\n\n- 第 {i} 项\n')
            (Path(tmp) / 'CHANGELOG.md').write_text('\n'.join(blocks), encoding='utf-8')
            with mock.patch.object(changelog.config, 'ROOT', Path(tmp)):
                data = changelog.load_changelog()
            self.assertTrue(data['available'])
            self.assertEqual(len(data['versions']), changelog._MAX_VERSIONS)
            self.assertEqual(data['total'], changelog._MAX_VERSIONS + 5)
            self.assertTrue(data['truncated'])
            # 保留的是文件开头的（最新）版本
            self.assertEqual(data['versions'][0]['version'], '1.0.0')

    def test_read_error_reports_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'CHANGELOG.md'
            path.write_text('## [1.0.0]\n\n### 新增\n\n- x\n', encoding='utf-8')
            with mock.patch.object(changelog.config, 'ROOT', Path(tmp)), \
                    mock.patch.object(Path, 'read_text', side_effect=OSError('boom')):
                data = changelog.load_changelog()
            self.assertFalse(data['available'])
            self.assertIn('读取更新日志失败', data['error'])

    def test_falls_back_to_server_copy(self) -> None:
        """老部署升级后根目录可能没有 CHANGELOG.md，此时用 server/ 里的副本。

        这不是假想场景：v1.0.16 之前的更新器只替换 server/、web/out/、
        deploy/ 与 .version，不碰根目录文件——实测真有部署报「未找到更新日志
        文件」，且因为已是最新版、再点更新也不会补上。发布打包因此会在
        server/ 放一份副本，这里锁定该回退必须生效。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'server').mkdir()
            (root / 'server' / 'CHANGELOG.md').write_text(
                '## [9.9.9] - 2026-01-01\n\n### 修复\n\n- 来自 server 副本\n',
                encoding='utf-8',
            )
            with mock.patch.object(changelog.config, 'ROOT', root):
                data = changelog.load_changelog()
            self.assertTrue(data['available'])
            self.assertEqual(data['versions'][0]['version'], '9.9.9')
            self.assertTrue(data['path'].endswith(str(Path('server') / 'CHANGELOG.md')))

    def test_root_copy_wins_over_server_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'server').mkdir()
            (root / 'CHANGELOG.md').write_text(
                '## [8.8.8] - 2026-01-01\n\n### 新增\n\n- 根目录优先\n', encoding='utf-8')
            (root / 'server' / 'CHANGELOG.md').write_text(
                '## [9.9.9] - 2026-01-01\n\n### 新增\n\n- server 副本\n', encoding='utf-8')
            with mock.patch.object(changelog.config, 'ROOT', root):
                data = changelog.load_changelog()
            self.assertEqual(data['versions'][0]['version'], '8.8.8')
            self.assertFalse(data['path'].endswith(str(Path('server') / 'CHANGELOG.md')))


class ChangelogOrderTest(unittest.TestCase):
    """段落顺序：版本必须**从新到旧**，且 [未发布] 只能有一个、排在最前。

    这是 v1.0.72 发版后发现的问题：文件里当时存在**两个** `## [未发布]` 段落，
    发版时把下面那个改成了 `## [1.0.72]`，于是 1.0.72 落在了 1.0.71 后面 ——
    界面「更新日志」页按文件顺序渲染，用户看到的最新版本排在旧版本下面。
    顺序错了不会有任何报错，只会让每一个人看到的列表都是乱的，所以钉一条。
    """

    ROOT_CHANGELOG = Path(__file__).resolve().parents[2] / 'CHANGELOG.md'

    @staticmethod
    def _key(version: str) -> tuple[int, ...]:
        return tuple(int(x) for x in re.findall(r'\d+', version))

    def _headings(self) -> list[str]:
        text = self.ROOT_CHANGELOG.read_text(encoding='utf-8')
        return re.findall(r'^## \[([^\]]+)\]', text, re.M)

    def test_versions_are_newest_first(self) -> None:
        heads = self._headings()
        self.assertGreater(len(heads), 50, f'只解析到 {len(heads)} 个版本段落，解析可能失效')
        released = [h for h in heads if h not in ('未发布', 'Unreleased')]
        keys = [self._key(v) for v in released]
        self.assertEqual(
            keys, sorted(keys, reverse=True),
            '版本段落没有按从新到旧排列，界面上的更新日志会顺序错乱：'
            + ', '.join(released[:6]),
        )

    def test_only_one_unreleased_section_and_it_is_first(self) -> None:
        heads = self._headings()
        unreleased = [i for i, h in enumerate(heads) if h in ('未发布', 'Unreleased')]
        self.assertLessEqual(
            len(unreleased), 1,
            f'有 {len(unreleased)} 个「未发布」段落 —— 发版时很容易改错那一个'
            f'（v1.0.72 就是这么排到 1.0.71 后面的）',
        )
        if unreleased:
            self.assertEqual(unreleased[0], 0, '「未发布」段落必须排在最前面')


class ChangelogIsUserFacingTest(unittest.TestCase):
    """更新日志必须**面向用户**，不能写内部实现细节。

    这份文件不只是给仓库看的：Release 页面的正文与应用内「更新日志」页展示的都是
    它的原文，读者是使用者而不是维护者。

    为什么要有这条守卫：维护者（尤其是我）很容易把它写成排障笔记 —— 函数名、模块
    路径、测试名、「我错在哪」「反向验证」这类叙事，对用户毫无意义，还会把「升级后
    我要做什么」这个真正有用的信息淹掉。这事已经发生多次（1.0.42 ~ 1.0.49 全线
    存在），靠自觉不可靠，所以用测试钉住。

    允许的例外：**界面上真实出现过的报错原文**要照抄（用户就是拿它来搜的），因此按
    「该词是否出现在用户可见的提示文本里」放行，而不是一刀切禁词。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = (Path(__file__).resolve().parents[2] / 'CHANGELOG.md').read_text(encoding='utf-8')

    def _sections(self) -> list[tuple[str, str]]:
        """按版本切段，返回 [(版本号, 正文)]。"""
        out: list[tuple[str, str]] = []
        cur_ver, buf = '', []
        for line in self.text.splitlines():
            m = re.match(r'^##\s*\[([^\]]+)\]', line)
            if m:
                if cur_ver:
                    out.append((cur_ver, '\n'.join(buf)))
                cur_ver, buf = m.group(1), []
            elif cur_ver:
                buf.append(line)
        if cur_ver:
            out.append((cur_ver, '\n'.join(buf)))
        return out

    @staticmethod
    def _user_visible_strings() -> set[str]:
        """扫服务端源码里**会被回显给用户**的中文短句（用于例外判定）。"""
        root = Path(__file__).resolve().parents[1]
        visible: set[str] = set()
        for path in root.rglob('*.py'):
            if 'tests' in path.parts:
                continue
            try:
                src = path.read_text(encoding='utf-8')
            except OSError:
                continue
            for lit in re.findall(r"'([^'\n]{6,160})'", src) + re.findall(r'"([^"\n]{6,160})"', src):
                if re.search(r'[\u4e00-\u9fff]', lit):
                    visible.add(lit)
        return visible

    def test_sections_parsed(self) -> None:
        """先确认切段有效，否则下面几条会在空集合上「通过」。"""
        secs = self._sections()
        self.assertGreater(len(secs), 10, '没切出版本段落，守卫会空转')
        self.assertIn('1.0.49', [v for v, _ in secs])

    def test_no_content_before_the_first_version_heading(self) -> None:
        """第一个 `## [版本]` 之前不能有**条目**（审查发现的空档）。

        为什么需要：切段是按 `## [x.y.z]` 分的，于是**标题之前**的内容不属于任何
        段落 —— 上面几条检查全都看不到它，写了等于没写。实测就这么漏过一次：
        加条目时替换字符串把 `## [未发布]` 那行一起吞了，两段条目挂在文件顶部没有
        任何版本归属，而守卫全绿。

        （文件头部的说明文字允许存在：它在第一个 `---` 之前，这里只看分隔线之后。）
        """
        head = self.text.split('\n---\n', 1)[0]
        body = self.text[len(head):]
        before = body.split('\n## [', 1)[0]
        loose = [ln for ln in before.splitlines()
                 if ln.startswith(('- ', '### ')) or re.match(r'^\d+\.\s', ln)]
        self.assertEqual(loose, [],
                         '第一个版本标题之前出现了条目/小节 —— 它们不属于任何版本段落：'
                         + '; '.join(loose[:3]))

    def test_no_internal_identifiers(self) -> None:
        """不得出现函数名、模块路径、下划线开头的内部名。"""
        bad = re.compile(
            r'`(?:[a-z_][a-z0-9_]*\(\)'                                # 函数调用
            r'|[a-z_][a-z0-9_]*/[a-z_][a-z0-9_]*\.(?:py|go|ts|tsx)'    # 文件路径
            r'|_[a-z][a-z0-9_]*)'                                      # 内部名
            r'`'
        )
        offenders = [f'{ver}: {m.group(0)}'
                     for ver, body in self._sections() for m in bad.finditer(body)]
        self.assertEqual(offenders, [],
                         '更新日志里有内部标识符（用户看不懂）：' + '; '.join(offenders[:8]))

    def test_no_debugging_narrative(self) -> None:
        """不得出现排障过程式叙事 —— 那是开发笔记，不是变更说明。"""
        bad = re.compile(
            r'我错在哪|我当时|我写的那条|我图省事|差点装成|自审发现|自审时|'
            r'反向验证|测试没抓住|用例变红|断言以|patch 目标|'
            r'根因是[^，。]{0,10}我|回归测试|单测|测试用例'
        )
        offenders = [f'{ver}: {m.group(0)}'
                     for ver, body in self._sections() for m in bad.finditer(body)]
        self.assertEqual(offenders, [],
                         '更新日志里有排障叙事（应改成面向用户的描述）：'
                         + '; '.join(offenders[:8]))

    def test_encoder_terms_only_when_quoting_a_visible_error(self) -> None:
        """并发 / 框架术语只在「引用用户看到的报错」时才允许出现。"""
        visible = self._user_visible_strings()
        terms = ['事件循环', '线程池', 'asyncio', 'to_thread', 'create_task',
                 'get_running_loop']
        offenders = []
        for ver, body in self._sections():
            for term in terms:
                if term in body and not any(term in s for s in visible):
                    offenders.append(f'{ver}: {term}')
        self.assertEqual(offenders, [],
                         '这些术语只该用于引用界面报错原文：' + '; '.join(offenders[:8]))

    def test_guard_patterns_are_not_vacuous(self) -> None:
        """确认两条正则真的能匹配 —— 免得写错了却一直「通过」。"""
        ident = re.compile(r'`(?:[a-z_][a-z0-9_]*\(\)|_[a-z][a-z0-9_]*)`')
        self.assertTrue(ident.search('修了 `_prepare()` 与 `a/b.py`'),
                        '内部标识符正则失配')
        self.assertTrue(re.search(r'反向验证', '已做反向验证'), '排障叙事正则失配')

    def test_user_visible_scan_found_something(self) -> None:
        """例外判定依赖「扫到用户可见文案」，扫不到就说明它失效了。"""
        self.assertGreater(len(self._user_visible_strings()), 50,
                           '没扫到用户可见的中文提示，例外判定会误伤')

    def test_released_versions_have_sections(self) -> None:
        """每个已发布段落都要有分类标题（否则页面渲染成一片裸文本）。"""
        blocks = re.split(r'^## \[[\d.]+\] - \d{4}-\d{2}-\d{2}$', self.text, flags=re.M)[1:]
        self.assertGreater(len(blocks), 10, '没切出版本段落')
        for i, block in enumerate(blocks):
            self.assertIn('###', block, f'第 {i + 1} 个版本段落没有分类标题')


if __name__ == '__main__':
    unittest.main()


class NoConflictMarkersTest(unittest.TestCase):
    """任何文件里都不许留下合并冲突标记。

    为什么值得一条守卫：CHANGELOG.md 的冲突标记**不报错、也不影响解析**——
    它只是被解析器当成普通文本忽略掉，于是带着 `<<<<<<< HEAD` 的更新日志会被
    CI 抽成 Release 正文、也会出现在应用内的「更新日志」页（真发生过：
    2026-09-27 为 v1.0.73 发版前才发现，标记和**重复的一整段条目**都已经提交）。
    这类问题只有人眼看才会发现，所以钉成机械检查。
    """

    ROOT = Path(__file__).resolve().parents[2]
    # 扫描范围：会被用户看到的文本（文档、代码、脚本、i18n），跳过依赖与产物
    SKIP_DIRS = {'node_modules', '.git', 'out', '.next', '__pycache__',
                 '.shots-account-groups', '.shots-dash-states', '.shots-dash-groups',
                 '.shots-state-honesty', '.login-shots', '.shots-upstreams-ui',
                 '.dash-states', '.dash-groups', '.state-honesty', '.login-poll',
                 '.account-groups', '.pack-test'}
    SUFFIXES = {'.md', '.py', '.ts', '.tsx', '.mjs', '.js', '.json', '.yml', '.yaml',
                '.sh', '.cmd', '.ps1', '.txt', '.example', '.toml', '.cfg', '.ini'}

    def _files(self) -> list[Path]:
        out: list[Path] = []
        for p in self.ROOT.rglob('*'):
            if not p.is_file() or p.suffix not in self.SUFFIXES:
                continue
            parts = set(p.parts)
            if parts & self.SKIP_DIRS or any(part.startswith('.') and part != '.github'
                                             for part in p.relative_to(self.ROOT).parts[:-1]):
                continue
            out.append(p)
        return out

    def test_scan_found_files(self) -> None:
        files = self._files()
        self.assertGreater(len(files), 100, f'只扫到 {len(files)} 个文本文件，扫描逻辑可能失效')

    def test_no_merge_conflict_markers(self) -> None:
        # 用拼接构造，免得这条守卫自己的源码里出现「行首七个尖括号」被自己抓到
        marks = ('<' * 7 + ' ', '>' * 7 + ' ', '=' * 7)
        offenders: list[str] = []
        for p in self._files():
            try:
                text = p.read_text(encoding='utf-8')
            except (UnicodeDecodeError, OSError):
                continue
            for i, line in enumerate(text.splitlines(), 1):
                s = line.rstrip()
                if s.startswith(marks[0]) or s.startswith(marks[1]) or s == marks[2]:
                    offenders.append(f'{p.relative_to(self.ROOT)}:{i}: {s[:40]}')
        self.assertEqual(
            offenders, [],
            '这些文件里残留了合并冲突标记（用户会看到，且解析器不会报错）：\n  '
            + '\n  '.join(offenders),
        )
