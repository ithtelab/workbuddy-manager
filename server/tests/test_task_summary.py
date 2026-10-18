"""活动任务按任务聚合（issue #189）。

输入是上游 `task_runner.py` 的原始 stdout（面板原样回显的那批行），输出是
「任务 → 状态」的一列摘要。这里用**真实形态**的行做样本（格式见
`web/lib/i18n/taskrun.ts` 的规则表，那就是上游文案的清单）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services.task_summary import classify, summarize  # noqa: E402


class ClassifyTest(unittest.TestCase):
    def test_four_states(self) -> None:
        cases = [
            ('query claimed(5/5) -> 已领，跳过', 'claimed'),
            ('query 5/5 -> 已完成/已领，跳过', 'claimed'),
            ('5/5 -> claimed（本轮已入账）', 'claimed'),
            # 本轮真正入账的收尾行：没有「已领/claimed」字样，靠 `claim N ok` 识别。
            ('claim 200 ok(credit=+300 energy=+5)', 'claimed'),
            ('claim 200 already_claimed(credit=+0 energy=+0)', 'claimed'),
            ('query 1/5 -> 可点亮 need=4 id源=专家市场 ids=[ex_1]，dry-run 跳过',
             'pending'),
            ('query 3/5 -> 可领(claim)，dry-run 跳过', 'pending'),
            ('query 1/5 -> 未 completed，only_claim 跳过', 'pending'),
            ('query locked（解锁 2026-09-25 00:00）-> 未到上线时间，跳过', 'pending'),
            ('query in_progress(3/5) -> 非夜猫窗口(23-08 CST)，skip pending', 'pending'),
            ('query 非映射任务，skip', 'skipped'),
            ('query 任务不存在', 'skipped'),
            ('query mp 口径任务不存在，skip', 'skipped'),
            ('query 1/1 -> 不可伪造(需真实支付)，skip', 'skipped'),
            ('report 无可用对象 id，skip', 'skipped'),
            ('accept 未登记生效，本轮跳过待下次', 'skipped'),
            ('viewed 失败: http 400', 'failed'),
            ('report 失败 #2: 500', 'failed'),
            ('claim 失败: 404（可稍后补领）', 'failed'),
        ]
        for tail, want in cases:
            with self.subTest(tail=tail):
                self.assertEqual(classify(tail), want)

    def test_fallback_is_not_failure(self) -> None:
        """「buddy5 失败 -> 降级单发」是降级成功，不能标成失败（否则会白折腾）。"""
        self.assertEqual(classify('1/1 200 code=0 buddy5 失败 -> 降级单发 200 code=0'),
                         'pending')

    def test_unknown_tail_is_pending(self) -> None:
        """上游随时会加新文案：认不出按待处理（宁可多看一眼，也别谎报完成）。"""
        self.assertEqual(classify('上游新加的一句谁也没见过的话'), 'pending')


class SummarizeTest(unittest.TestCase):
    def test_aggregate_by_task_with_precedence(self) -> None:
        lines = [
            '[task_runner] u1 first_buddy: query 1/1 -> 可点亮 need=1 id源=无 ids=[]，dry-run 跳过',
            '[task_runner] u1 first_buddy: 1/1 -> claimed（本轮已入账）',
            '[task_runner] u1 Sequential_Tasks_2: query 1/1 -> 已完成/已领，跳过',
            '[task_runner] u1 Sequential_Tasks_3: query 2/5 -> 可点亮 need=3 id源=专家 ids=[ex_1]，dry-run 跳过',
            '[task_runner] u1 night: query 非映射任务，skip',
            '[task_runner] u1 chat_5: report 失败 #1: 500',
        ]
        out = summarize(lines)
        by_code = {it['code']: it for it in out}

        self.assertEqual(by_code['first_buddy']['state'], 'claimed')
        self.assertEqual(by_code['first_buddy']['count'], 2)
        self.assertEqual(by_code['Sequential_Tasks_2']['state'], 'claimed')
        self.assertEqual(by_code['Sequential_Tasks_3']['state'], 'pending')
        self.assertEqual(by_code['night']['state'], 'skipped')
        self.assertEqual(by_code['chat_5']['state'], 'failed')

        # 排序：失败 → 待处理 → 已领取 → 跳过；同档按任务码（ASCII 序）
        self.assertEqual([it['code'] for it in out],
                         ['chat_5', 'Sequential_Tasks_3', 'Sequential_Tasks_2',
                          'first_buddy', 'night'])

        # 代表行是「最需要看」的那条：chat_5 只有失败行，first_buddy 取已领取那条
        self.assertIn('report 失败', by_code['chat_5']['line'])
        self.assertIn('claimed', by_code['first_buddy']['line'])

    def test_claim_ok_marks_task_claimed(self) -> None:
        """真实一轮：query 未完成 → accept → report → claim ok，最终必须显示已领取。

        这正是线上反馈的场景：一轮 +1700 分，摘要列表却全标「待处理」。
        """
        lines = [
            '[task_runner] u1 Buddy_App: query not_accepted(0/1) -> 未 completed，only_claim 跳过',
            '[task_runner] u1 Buddy_App: accept 尝试1 200 status=accepted 回读=accepted-> 生效',
            '[task_runner] u1 Buddy_App: report 1/1 200 code=0 buddy5 id=cb_x',
            '[task_runner] u1 Buddy_App: query re-read 1/1 accept_status=completed',
            '[task_runner] u1 Buddy_App: claim 200 ok(credit=+100 energy=+5)',
        ]
        out = summarize(lines)
        self.assertEqual(out[0]['state'], 'claimed')
        self.assertIn('claim 200 ok', out[0]['line'])

    def test_higher_priority_wins_regardless_of_order(self) -> None:
        """先成功、后失败（或反之）都要以失败为准 —— 顺序不该改变结论。"""
        lines = [
            '[task_runner] u1 t: 1/1 -> claimed（本轮已入账）',
            '[task_runner] u1 t: claim 失败: 404（可稍后补领）',
        ]
        out = summarize(lines)
        self.assertEqual(out[0]['state'], 'failed')
        self.assertEqual(out[0]['count'], 2)

    def test_account_level_lines_are_ignored(self) -> None:
        """账号级消息的「任务码」位是固定词，不能变成一行假任务。"""
        lines = [
            '[task_runner] u1 school 活动非进行期（in_period=false），school 段跳过',
            '[task_runner] u1 school/tasks 拉取失败: timeout',
            'ERR: [task_runner] u1 query list_tasks 失败: 500',
            'WARN: 全量批量 + --yes 未限定 --only，注意 54 号批量——请确认 Hermes 决策后再跑',
            '[skip] u1 global realm 不适用 CN 任务',
            '!! 已手动停止',
        ]
        self.assertEqual(summarize(lines), [])

    def test_empty(self) -> None:
        self.assertEqual(summarize([]), [])


if __name__ == '__main__':
    unittest.main()
