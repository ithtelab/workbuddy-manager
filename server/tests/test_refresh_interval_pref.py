"""抓包页「刷新间隔」偏好的行为测试（前端逻辑，借 Node 执行）。

为什么值得有：这个偏好看着只有一个「读」和一个「写」，不可能出错 —— 但它有三个
容易踩的坑：

- **localStorage 抛错**（隐私模式 / 被禁用）→ 读的地方必须吞掉异常，否则页面
  白屏；
- **存了非法值**（上一版留下的、手工改的）→ 必须回到默认，不能变成 ``NaN`` 塞进
  ``setInterval``（那等于每 0 毫秒刷一次，把后端打爆）；
- **默认值漂移** → 默认必须是 20 秒（产品约定）。

为什么用 Python 包一层：本仓库的测试套件是 Python 的（``pytest server/tests/``），
而这段逻辑在前端。没有 Node（或版本太旧）时**跳过**而不是失败 —— 后端开发者不该
因为机器上没装 Node 就跑不了整个测试套件。

跑的是 ``web/lib/refresh-interval.test.mjs``，它直接 import ``.ts`` 源码（Node ≥ 22.6
的 type stripping），因此测的是**真实实现**，不是复制一份逻辑。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / 'web' / 'lib' / 'refresh-interval.test.mjs'
_NODE = shutil.which('node')

# 只跑 .mjs，不碰 .ts —— Node 的 type stripping 从 22.6 起才有
_MIN_MAJOR = 22


def _node_major() -> int | None:
    if not _NODE:
        return None
    try:
        out = subprocess.run([_NODE, '--version'], capture_output=True,
                             text=True, timeout=15).stdout.strip()
        return int(out.lstrip('v').split('.')[0])
    except Exception:  # noqa: BLE001
        return None


_MAJOR = _node_major()


class RefreshIntervalPrefTest(unittest.TestCase):
    @unittest.skipUnless(_NODE, '未安装 node，跳过前端逻辑测试')
    @unittest.skipUnless(_MAJOR is not None and _MAJOR >= _MIN_MAJOR,
                         f'需要 node ≥ {_MIN_MAJOR}（type stripping），当前 {_MAJOR}')
    def test_refresh_interval_pref(self) -> None:
        self.assertTrue(_SCRIPT.is_file(), f'缺少测试脚本: {_SCRIPT}')
        proc = subprocess.run(
            [_NODE, '--experimental-strip-types', str(_SCRIPT)],
            capture_output=True, text=True, timeout=120, cwd=str(_ROOT),
        )
        out = (proc.stdout or '') + (proc.stderr or '')
        self.assertEqual(proc.returncode, 0, f'刷新间隔偏好的行为不符合预期：\n{out}')
        self.assertIn('all passed', out, f'脚本没有跑到通过：\n{out}')
