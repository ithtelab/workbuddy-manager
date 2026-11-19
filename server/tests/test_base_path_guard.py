"""子路径部署（basePath）的「漏一处就 404」清单（PR #60）。

## 为什么需要这条守卫

Next.js 的 `basePath` 只自动改写 `next/link`、`next/router` 与 `_next` 静态资源；
**管不到**裸的 `window.location.href`、原生 `fetch('/api/...')`、原生 `<a href>`、
axios 的 `baseURL`，以及 `metadata` 里的图标/清单地址。这些地方在子路径部署下会
把用户送到域名根（通常属于另一个站点）→ 表现为「点一下就 404」。

单测跑不了 TS，所以这里做**源码级**检查：把这份清单钉住，漏一处就红。数量少、
判据明确，比"靠人记得"可靠（本次评审就抓到一处漏网：`layout.tsx` 的 favicon /
manifest 仍是根路径——实测子路径构建产物里是 `href="/favicon/..."`）。

## 实测证据（评审时手工做过，留档）

用 `NEXT_PUBLIC_BASE_PATH=/wm` 构建后扫产物 HTML 里的所有绝对路径：
修复前 favicon / manifest 五项不带前缀，其余全带；修复后**无一条不带前缀**，
且不设该变量重新构建时产物里**没有任何 `/wm`**（根路径部署行为不变）。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ROOT = Path(__file__).resolve().parents[2]
_WEB = _ROOT / 'web'


def _src(rel: str) -> str:
    return (_WEB / rel).read_text(encoding='utf-8')



class PublicPageRedirectTest(unittest.TestCase):
    """公开页不能被全局 401 处理踢去登录页（评审实测到的真问题）。

    红包抽奖页 `/claim` 是给**没有账号**的人看的（收到链接的同事朋友）。它会加载
    `/api/me`（AuthProvider 校验一次会话），未登录时 401 —— 而 `lib/api.ts` 的全局
    401 处理会直接把人送到 `/login`。结果是：这一页在匿名访客眼里就是登录页，
    「不注册也能领」直接落空。
    """

    def test_api_client_exempts_public_pages(self) -> None:
        src = _src('lib/api.ts')
        self.assertIn('publicPaths', src, '没有公开页清单，401 会把访客踢去登录页')
        self.assertIn('${BASE_PATH}/claim', src, '抽奖页不在白名单里')

    def test_claim_page_lives_outside_the_guarded_layout(self) -> None:
        """`(main)/` 那组布局会校验登录态，公开页必须放在它外面。"""
        self.assertTrue((_WEB / 'app' / 'claim' / 'page.tsx').is_file())
        self.assertFalse((_WEB / 'app' / '(main)' / 'claim').exists(),
                         '抽奖页挪进了 (main)/，匿名访客会被挡在登录页')

class MetadataPathsTest(unittest.TestCase):
    """`metadata` 里的图标 / 清单地址必须带前缀（评审抓到的那处漏网）。"""

    def test_layout_icons_and_manifest_use_with_base_path(self) -> None:
        src = _src('app/layout.tsx')
        block = src[src.index('manifest:'):src.index('};', src.index('manifest:'))]
        for path in ('/favicon/site.webmanifest', '/favicon/favicon-32x32.png',
                     '/favicon/favicon-16x16.png', '/favicon/favicon.ico',
                     '/favicon/apple-touch-icon.png'):
            with self.subTest(path=path):
                self.assertIn(f"withBasePath('{path}')", block,
                              f'{path} 没走 withBasePath —— 子路径部署下会请求到域名根')

    def test_manifest_internal_icons_are_relative(self) -> None:
        """清单内部的图标用相对路径：它相对**清单自己的 URL** 解析。

        于是「清单地址带前缀」+「内部相对路径」两件事配合，图标才落在
        `<前缀>/favicon/` 下（PR 的改动 + 本次补漏合起来才成立）。
        """
        import json
        data = json.loads(_src('public/favicon/site.webmanifest'))
        for icon in data.get('icons', []):
            with self.subTest(src=icon.get('src')):
                self.assertFalse(str(icon.get('src', '')).startswith('/'),
                                 '清单内的图标不该写成绝对路径')


class NavigationPathsTest(unittest.TestCase):
    """站内跳转 / 请求入口必须带前缀（PR 列举的那份清单）。"""

    def test_browser_redirects_use_base_path(self) -> None:
        """整页跳转的目标要么直接带 BASE_PATH，要么来自本文件里带 BASE_PATH 的变量。

        第二种是常态（`const loginPath = \`${BASE_PATH}/login\`` 之后再赋值），所以
        判据是「赋值语句里出现 BASE_PATH，或它引用的标识符在本文件里被赋予了带
        BASE_PATH 的值」—— 只看赋值语句本身会误报（第一版就误报了 loginPath）。
        """
        # 设置页的外壳在 `layout.tsx`（批次 4 起 Tab 变成子路由，见那个文件顶部）
        for rel in ('lib/auth-context.tsx', 'lib/api.ts', 'app/(main)/settings/layout.tsx'):
            src = _src(rel)
            for m in re.finditer(r'window\.location\.href\s*=\s*([^;\n]+)', src):
                expr = m.group(1).strip()
                with self.subTest(file=rel, expr=expr[:40]):
                    if 'BASE_PATH' in expr:
                        continue
                    # 模板字面量形态：`${loginPath}?expired=1` —— 取里面**引用到的
                    # 标识符**逐个判定。附加的只是固定查询串（`?expired=1`），
                    # 不引入新的跳转目标，所以这里只要求被引用的变量带前缀。
                    if expr.startswith('`'):
                        names = re.findall(r'\$\{([A-Za-z_$][\w$]*)\}', expr)
                        self.assertTrue(names, f'看不懂的模板跳转表达式：{expr}')
                        for name in names:
                            self.assertRegex(
                                src, name + r'\s*=\s*[^;\n]*BASE_PATH',
                                f'整页跳转用的 {name} 没带部署前缀 —— 子路径下会跳到域名根')
                        continue
                    var = re.match(r'([A-Za-z_$][\w$]*)', expr)
                    self.assertIsNotNone(var, f'看不懂的跳转表达式：{expr}')
                    name = var.group(1)
                    self.assertRegex(
                        src, name + r'\s*=\s*[^;\n]*BASE_PATH',
                        f'整页跳转用的 {name} 没带部署前缀 —— 子路径下会跳到域名根')

    def test_axios_base_url_uses_base_path(self) -> None:
        src = _src('lib/api.ts')
        self.assertTrue(re.search(r'baseURL:\s*[^\n]*BASE_PATH', src),
                        'axios baseURL 没带前缀：所有接口调用都会打到域名根')

    def test_keys_page_base_url_includes_prefix(self) -> None:
        """密钥页展示的「接入地址」也要带前缀，否则用户照抄会 404。"""
        src = _src('app/(main)/keys/page.tsx')
        self.assertIn('BASE_PATH', src)


class ServerSideBasePathTest(unittest.TestCase):
    """服务端只在自己**发出绝对地址**时才需要前缀（反代会把前缀剥掉）。"""

    def test_rsc_redirect_prepends_prefix(self) -> None:
        src = (_ROOT / 'server' / 'main.py').read_text(encoding='utf-8')
        self.assertIn("RedirectResponse(f'{config.BASE_PATH}{page}'", src,
                      'RSC 兜底重定向没带前缀 —— 用户会被送到域名根')

    def test_base_path_is_normalised(self) -> None:
        """`WB_BASE_PATH` 的归一化：去首尾斜杠后统一成 `/xxx` 形态，空则空串。"""
        src = (_ROOT / 'server' / 'config.py').read_text(encoding='utf-8')
        self.assertIn("_env('WB_BASE_PATH', '').strip('/')", src)
        self.assertIn("BASE_PATH = f'/{_BASE_PATH_RAW}' if _BASE_PATH_RAW else ''", src)


if __name__ == '__main__':
    unittest.main()
