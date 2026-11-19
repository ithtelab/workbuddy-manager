"""模型中心「重新拉取」的结果必须反馈给用户，不能只弹一条「正在拉取」。

与 test_web_dangerous_actions.py 同一思路：这是**结构性**约定，类型系统表达不了
（`reload()` 返回 `Promise<boolean>`，忽略返回值同样能通过 `tsc`），但可以用源码
断言钉住。

原来的写法是：

    void refetch();
    notify.info(t('models.refetching'));

—— 弹出「正在重新拉取模型列表…」之后**不看结果**，无论取到没取到都停在这句话上
（它要等 3.6 秒才自己消失）。用户点完按钮看到的就是「正在拉取」，分不清是在拉、
拉到了、还是压根没拉到；失败时列表照旧显示上一次的缓存，界面看上去一切正常。

所以这里锁四件事：
1. `refetch` 把 `reload()` 的成败**返回**出来（写成 `void` 或加 `.catch()` 都会
   拿不到真实结果——`reload` 内部收掉了异常，永不 reject）；
2. 按钮回调 **await** 这个结果并按成败分别提示（成功/失败两个键都要在）；
3. 「进行中」那条提示在出结果时被**关掉**，而不是与结果提示叠在一起；
4. 不再出现旧的「发出去就不管」的调用形式。

不评判文案措辞（那没法自动判定），键是否存在于五个语言字典由 test_web_i18n.py 管。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ROOT = Path(__file__).resolve().parents[2]
_MODELS = _ROOT / 'web' / 'app' / '(main)' / 'models' / 'page.tsx'


def _strip_comments(src: str) -> str:
    """去掉注释再断言。

    这个文件里恰好有几段注释**引用**了原来的坏写法（`void refetch()`），
    不剥掉的话，解释「为什么改掉」的注释会让守卫自己报错。
    """
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    return re.sub(r'//[^\n]*', '', src)


class ModelsRefetchFeedbackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.src = _strip_comments(_MODELS.read_text(encoding='utf-8'))

    def test_scan_target_is_the_real_file(self) -> None:
        """守卫自检：确认扫到的是真文件，而不是路径写错后的空串。

        路径写错时下面每条断言都会因为「找不到」而失败，但失败原因看着像代码
        回归；先钉一句「文件读到东西了」，才能把两类问题分开。
        """
        self.assertIn('export default function ModelsPage', self.src)
        self.assertIn('const refetch = useCallback', self.src)

    def test_refetch_returns_the_outcome(self) -> None:
        """`refetch` 必须把 `reload()` 的布尔结果返回出去，调用方才可能判成败。"""
        body = self.src.split('const refetch = useCallback', 1)[1].split('}, [reload])', 1)[0]
        self.assertIn('return await reload()', body)
        self.assertIn('Promise<boolean>', body)

    def test_button_awaits_result_and_branches(self) -> None:
        """按钮回调要等结果，并分别给出成功与失败提示（两个键都必须在）。

        切出按钮那一段的做法：从 `onClick={async` 到按钮内第一个 `<Loader2`
        （那已经是图标渲染处，回调早就写完了）。不解析 JSX——这里只需要圈出
        回调体这一块，圈得略大不影响判断。
        """
        handler = self.src.split('onClick={async', 1)[1].split('<Loader2', 1)[0]
        self.assertIn('await refetch()', handler)

        # 成功分支：`if (done)` 到 `else` 之间必须是 ok + 成功键；
        # 失败分支：`else` 之后必须是 err + 失败键。写反了就是假反馈。
        yes, no = handler.split('if (done)', 1)[1].split('else', 1)
        self.assertIn('models.refetchDone', yes)
        self.assertIn('models.refetchFailed', no)
        self.assertIn('notify.ok(', yes)
        self.assertIn('notify.err(', no)
        self.assertNotIn('notify.err(', yes)

    def test_pending_toast_is_replaced(self) -> None:
        """「进行中」那条提示要关掉：留着会与结果提示并存、互相矛盾。"""
        handler = self.src.split('onClick={async', 1)[1].split('<Loader2', 1)[0]
        self.assertIn("notify.info(t('models.refetching'))", handler)
        self.assertIn('notify.dismiss(', handler)

    def test_no_fire_and_forget_call(self) -> None:
        """不再出现旧的「发出去就不管」写法。"""
        self.assertNotIn('void refetch()', self.src)


if __name__ == '__main__':
    unittest.main()
