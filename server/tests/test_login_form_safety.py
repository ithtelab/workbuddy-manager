"""登录表单的两道保险：hydrate 前不许原生提交，且原生提交也不带 query 参数。

为什么值得一条测试：静态导出的登录页在 JS 到位之前就是一个**原生表单**。
默认提交方式是 GET，于是「脚本没加载完就点了登录 / 按了回车」会把
`?username=…&password=…` 拼到地址栏 —— 密码随后进入浏览记录、反向代理访问
日志与 Referer。这个问题不会报错、界面也照常跳转，只有翻日志才看得见。

修法有两层，都要钉住：
  1. 提交按钮在挂载前 disabled（点不动 → 不会原生提交）；
  2. `<form method="post">` 作为兜底（真被提交也只是一次 405，密码留在请求体里）。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_LOGIN = _ROOT / 'web' / 'app' / '(auth)' / 'login' / 'page.tsx'


class LoginFormSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.src = _LOGIN.read_text(encoding='utf-8')

    def test_form_declares_post_method(self) -> None:
        form = re.search(r'<form\b[^>]*>', self.src)
        self.assertIsNotNone(form, '找不到登录表单')
        assert form is not None
        self.assertIn('method="post"', form.group(0),
                      '登录表单没有声明 method="post"：JS 未就绪时原生提交会把密码拼进 URL')

    def test_submit_button_is_gated_on_hydration(self) -> None:
        self.assertRegex(self.src, r'const \[ready,\s*setReady\]\s*=\s*useState',
                         '登录页缺少「已挂载」状态')
        self.assertRegex(self.src, r'useEffect\(\(\)\s*=>\s*setReady\(true\)',
                         '「已挂载」状态没有在挂载后置位')
        button = re.search(r'<Button\s+type="submit"[^>]*>', self.src)
        self.assertIsNotNone(button, '找不到提交按钮')
        assert button is not None
        self.assertIn('!ready', button.group(0),
                      '提交按钮没有以「已挂载」为条件禁用：hydrate 前点击会走原生 GET 提交')


if __name__ == '__main__':
    unittest.main()
