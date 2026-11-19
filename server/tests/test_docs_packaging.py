"""发布包里的文档：只装用户文档，且与 README 的引用一致。

## 为什么需要这条

CI 打包发布时此前是 `cp -r docs`（**整目录**进包），于是维护者/开发用的文档
一并进了每一个用户的安装目录：发布流程手册、签名密钥管理、i18n 说明、命名兼容
笔记、上游修复记录、功能提案、62KB 的设计与实现方案。用户打开 `docs/` 看到的是
一堆和他无关的东西；更要紧的是这条路过一直在：**以后有人把内部细节写进这些文档，
它会被静默分发给所有部署**（发布包里那份还留在用户机器上，收不回来）。

改成白名单（`.github/workflows/release.yml` 的 `docs-pack` 段）之后，两种错误
会悄悄发生，所以在这里双向钉住：

1. **加了新用户文档、忘了进名单** —— README 链过去，用户包里却没有那份文件，
   用户点开是死链；
2. **把内部文档加进名单** —— 又回到「内部文档进用户机器」的老路。

判据用「**引用闭包**」而不是手写两份清单：从 README（中英两份）出发，沿着
docs/*.md 之间的相互引用一直走到底，得到的集合就是「用户能顺着文档摸到的」——
它必须与白名单**完全相等**。

> 这也意味着加一个新用户文档只要在 README 里链一下就够了；若某篇用户文档
> 链到了内部文档（例如 features.md 链了 release-signing.md），那篇就自动成为
> 用户文档、必须进名单 —— 想把它留在包外，就该去掉那个链接。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / '.github' / 'workflows' / 'release.yml'
_DOCS = _ROOT / 'docs'


def _linked_docs(text: str) -> set[str]:
    """正文里指向 docs/ 下文件的链接（含同目录内的相对写法）。"""
    out: set[str] = set()
    for target in re.findall(r'\]\(([^)]+)\)', text):
        target = target.split('#')[0].strip()
        if target.startswith('docs/'):
            out.add(Path(target).name)
        elif re.fullmatch(r'[^/\\]+\.md', target):
            out.add(target)
    return {name for name in out if (_DOCS / name).is_file()}


def _closure() -> set[str]:
    """从 README 出发的引用闭包：用户顺着文档能摸到的 docs/*.md。"""
    roots = ['README.md', 'README.en.md']
    seen: set[str] = set()
    frontier = set()
    for name in roots:
        frontier |= _linked_docs((_ROOT / name).read_text(encoding='utf-8'))
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.add(name)
        for linked in _linked_docs((_DOCS / name).read_text(encoding='utf-8')):
            if linked not in seen:
                frontier.add(linked)
    return seen


def _pack_block() -> str:
    """workflow 里 docs-pack:begin / end 之间的那一段（打包白名单就在里面）。"""
    text = _WORKFLOW.read_text(encoding='utf-8')
    begin = text.index('docs-pack:begin')
    end = text.index('docs-pack:end', begin)
    return text[begin:end]


def _whitelist() -> list[str]:
    block = _pack_block()
    m = re.search(r'DOCS_LIST="([^"]+)"', block)
    assert m, '打包步骤里找不到 DOCS_LIST（白名单被改名或删了？）'
    return m.group(1).split()


class DocsPackagingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.whitelist = _whitelist()
        cls.closure = _closure()

    def test_parsing_is_not_vacuous(self) -> None:
        """解析要真的抓到东西：抓不到就全是「空集等于空集」的假绿。"""
        self.assertGreaterEqual(len(self.whitelist), 5, '白名单没解析出来')
        self.assertGreaterEqual(len(self.closure), 5, 'README 的文档引用没解析出来')

    def test_all_user_docs_are_shipped(self) -> None:
        """README 能摸到的文档都必须进包（否则用户机器上是死链）。"""
        missing = sorted(self.closure - set(self.whitelist))
        self.assertEqual(missing, [],
                         f'这些用户文档没进发布包（README 链着它们）：{missing}')

    def test_no_internal_docs_are_shipped(self) -> None:
        """不在闭包里的就是内部文档：不许进包。"""
        internal = sorted(set(self.whitelist) - self.closure)
        self.assertEqual(internal, [],
                         f'这些文档 README 摸不到（内部文档），不该进发布包：{internal}')

    def test_whitelisted_files_exist(self) -> None:
        missing = [n for n in self.whitelist if not (_DOCS / n).is_file()]
        self.assertEqual(missing, [], f'白名单里的文件不存在：{missing}')

    def test_images_are_shipped(self) -> None:
        """截图是 README 的一部分：少了它们用户看到的是裂图。"""
        self.assertIn('docs/images', _pack_block(), '发布包没带截图目录')

    def test_no_blanket_docs_copy(self) -> None:
        """整目录拷贝的老写法不许回来（它会把内部文档一并装进用户机器）。

        只放行 `docs/images` 那一行：截图是用户文档的一部分，本来就该整目录拷。
        """
        bad = [line.strip() for line in _WORKFLOW.read_text(encoding='utf-8').splitlines()
               if re.search(r'cp\s+-r\s+docs\b', line) and 'docs/images' not in line]
        self.assertEqual(bad, [], f'又出现了整目录拷贝 docs —— 内部文档会静默进包：{bad}')

    def test_reverse_check_present(self) -> None:
        """打包时的反向核对（包内不得出现计划外文件）不许被删掉。"""
        block = _pack_block()
        self.assertIn('计划外文件', block, '打包步骤丢了「包内 docs 只许是名单里的」核对')
        self.assertIn('$STAGE/docs', block)


if __name__ == '__main__':
    unittest.main()
