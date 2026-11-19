"""「本次更新」公告弹窗的源码级不变量。

弹窗本身是前端组件（无法在后端套件里渲染），但几条**用户可见的行为**可以
机械校验，且写错时的表现都很隐蔽：

1. 每个版本只弹一次（localStorage 记录已读版本号）——漏了就会每次刷新都弹；
2. 拿不到更新日志时不弹（弹窗不该成为新的报错来源）；
3. 只在主布局（已登录区域）挂载，登录页不弹；
4. 弹窗里必须带「使用边界」那段免责声明，且文案与 README 一致
   （两处各写一份必然漂移）。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ROOT = Path(__file__).resolve().parents[2]
_DIALOG = _ROOT / 'web' / 'components' / 'common' / 'layout' / 'WhatsNewDialog.tsx'
_LAYOUT = _ROOT / 'web' / 'app' / '(main)' / 'layout.tsx'
_LOCALES = _ROOT / 'web' / 'lib' / 'i18n' / 'locales'


class WhatsNewDialogTest(unittest.TestCase):
    def _src(self) -> str:
        self.assertTrue(_DIALOG.is_file(), f'缺少 {_DIALOG}')
        return _DIALOG.read_text(encoding='utf-8')

    def test_shows_once_per_version(self) -> None:
        src = self._src()
        self.assertIn("wb.whats-new.seen", src, '没有记录已读版本 —— 每次刷新都会弹')
        self.assertIn('readSeen() === res.current', src,
                      '判断条件必须是「已读版本 == 当前版本」')

    def test_silent_when_changelog_unavailable(self) -> None:
        src = self._src()
        self.assertIn('!res.available', src, '拿不到更新日志时应当不弹')
        self.assertIn('.catch(', src)

    def test_mounted_in_main_layout_only(self) -> None:
        layout = _LAYOUT.read_text(encoding='utf-8')
        self.assertIn('WhatsNewDialog', layout, '没有挂到主布局')
        login = _ROOT / 'web' / 'app' / 'login' / 'page.tsx'
        if login.is_file():
            self.assertNotIn('WhatsNewDialog', login.read_text(encoding='utf-8'),
                             '登录页不该弹公告')

    def test_carries_usage_boundary(self) -> None:
        src = self._src()
        self.assertIn("t('whatsNew.boundary')", src, '弹窗里没有使用边界声明')
        self.assertIn("t('whatsNew.disclaimerLink')", src, '没有指向完整免责声明')


class WhatsNewCopyTest(unittest.TestCase):
    """五种语言的公告文案必须齐全，且边界声明与 README 同义。"""

    REQUIRED = ('title', 'titleFallback', 'thanks', 'boundaryTitle',
                'boundary', 'disclaimerLink', 'fullChangelog',
                'starTitle', 'starBody', 'starLink')

    def test_star_block_links_to_the_repo(self) -> None:
        """2k Stars 模块必须同时给出仓库地址与 Star 入口，且都指向本仓库。"""
        src = _DIALOG.read_text(encoding='utf-8')
        self.assertIn('whatsNew.starTitle', src, '弹窗里没有 Star 里程碑')
        self.assertIn('whatsNew.starLink', src, '没有 Star 按钮')
        self.assertIn('https://github.com/ithtelab/workbuddy-manager', src)
        # 仓库地址只写一份常量：两处各写一份必然会漂移
        self.assertIn('const REPO_URL', src)
        # GitHub 已把 stargazer / watcher 列表收窄为仓库管理员与协作者可见，
        # `/stargazers` 对其他人一律 404 —— 已登录 GitHub 的普通账号也一样，
        # 是权限墙不是登录墙。Star 落点必须是仓库主页。
        self.assertNotIn('/stargazers', src,
                         'Star 按钮不能指向 /stargazers：非本仓库协作者访问一律 404')

    def test_layout_keeps_header_and_footer_fixed(self) -> None:
        """只有更新内容可以滚 —— 头部、致谢/免责声明、页脚都得固定。

        此前是整块内容一起滚：12 条记录把弹窗顶到 774px，Star 与免责声明要滚到
        底才看得到，页脚按钮还会跟着内容跑出视口。
        """
        src = _DIALOG.read_text(encoding='utf-8')
        # 查实际使用（<DialogBody），注释里解释「为什么不用它」不算
        self.assertNotIn('<DialogBody', src,
                         '不能再用 DialogBody(Radix ScrollArea)：它的 Viewport 用 size-full，'
                         '在 flex 列里会算出内容高度，内容压到页脚上')
        self.assertIn('overflow-y-auto', src, '更新内容区没有独立滚动')
        # 三段固定区各自在滚动容器之外，且被 border-t / border-b 分隔
        self.assertIn('min-h-0 flex-1', src)

    def test_narrow_screen_uses_short_boundary(self) -> None:
        """窄屏（<640px）只显示免责声明摘要：全文展开会吃掉一半正文区。"""
        src = _DIALOG.read_text(encoding='utf-8')
        self.assertIn("t('whatsNew.boundaryShort')", src)
        self.assertIn('sm:hidden', src)
        self.assertIn('hidden sm:inline', src)

    def test_title_does_not_double_the_v_prefix(self) -> None:
        """标题曾经显示成 `vv2.11.2`：current 自带 v，模板里又加了一个。"""
        src = _DIALOG.read_text(encoding='utf-8')
        self.assertIn("t('whatsNew.titlePlain')", src,
                      '标题应使用不含占位 v 的文案，版本号单独渲染')
        self.assertIn('v{version}', src, '`v` 应由界面加在归一后的版本号前面')

    def test_version_match_ignores_v_prefix(self) -> None:
        """`/api/system/changelog` 的 current 是 `v2.11.1`，CHANGELOG 段标题是
        `[2.11.1]` —— 严格相等会让弹窗一条更新内容都不显示。"""
        src = _DIALOG.read_text(encoding='utf-8')
        self.assertNotIn('v.version === data?.current', src,
                         '版本号必须去掉 v 前缀再比（与 ChangelogPanel 同口径）')
        self.assertIn("replace(/^v/i", src, '没有对版本号做 v 前缀归一')
        self.assertIn('!v.unreleased', src, '「未发布」段不该被当成当前版本')

    def test_repo_links_are_external_safe(self) -> None:
        src = _DIALOG.read_text(encoding='utf-8')
        self.assertGreaterEqual(src.count('rel="noreferrer"'), 3,
                                '站外链接必须带 rel=noreferrer')

    def test_all_locales_have_keys(self) -> None:
        import json
        for name in ('zh-CN', 'zh-TW', 'en', 'ja', 'ko'):
            data = json.loads((_LOCALES / f'{name}.json').read_text(encoding='utf-8'))
            block = data.get('whatsNew')
            self.assertIsInstance(block, dict, f'{name} 缺 whatsNew 段')
            for key in self.REQUIRED:
                self.assertTrue(str(block.get(key) or '').strip(),
                                f'{name}.whatsNew.{key} 为空')

    def test_boundary_matches_readme_terms(self) -> None:
        """README 首屏与弹窗的四类禁止项必须一致，避免两处说法漂移。"""
        import json
        zh = json.loads((_LOCALES / 'zh-CN.json').read_text(encoding='utf-8'))
        boundary = zh['whatsNew']['boundary']
        self.assertIn('本人及亲属', boundary)
        for term in ('批量注册', '账号交易', '接码', '绕过平台风控', '盈利'):
            self.assertIn(term, boundary, f'边界声明漏了「{term}」')

    def test_no_duplicate_keys_after_insert(self) -> None:
        src = (_LOCALES / 'zh-CN.json').read_text(encoding='utf-8')
        self.assertEqual(src.count('"whatsNew"'), 1, 'whatsNew 段出现多次')


if __name__ == '__main__':
    unittest.main()
