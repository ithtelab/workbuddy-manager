"""把「活动任务」的原始日志行按任务聚合成一列（issue #189）。

## 为什么需要

账号页「活动任务」回显的是上游 `task_runner.py` 的原始 stdout：一行一个动作，
任务名是 `Sequential_Tasks_2` 这类内部标识。用户原话是「纯单词，不知道做了什么
任务、哪些没做」——他要的是**按任务**看：

    小程序选中专家并对话    已领取
    夜猫对话（23–08）       待处理    可点亮 need=1 …
    某个新任务              跳过      非映射任务

## 为什么放在后端

分类是**领域判断**（这个任务到底做完了没有），不是文案翻译。放后端可以用真实日志
样本写用例，而前端只负责把 `line` 用既有的逐行译文渲染出来（`lib/i18n/taskrun.ts`）。
翻译仍然只在展示层做，日志原文不改。

## 状态口径（四档，与用户提议一致）

  · `claimed` 已领取：已领 / 已完成 / 本轮已入账
  · `pending` 待处理：可点亮 / 可领 / 未完成 / 未达标 / 未解锁 / 进行中
  · `skipped` 跳过：非映射 / 不可伪造（人工）/ 无可用对象 id / 登记未生效 / dry-run
  · `failed`  失败：任何带「失败」的行（「buddy5 失败 -> 降级单发」除外 —— 那是
              降级后成功的记录，标成失败会让人白折腾）

同一条任务可能打出多行（query → accept → report → claim），聚合取**最需要行动**
的那一档：failed > claimed > pending > skipped；同档取最后一行（最新状态）。
"""
from __future__ import annotations

import re

# [task_runner] <uid8> <task_code>: <tail>
_RUNNER = re.compile(r'^\[task_runner\] (\S+) (\S+): (.*)$')

# 账号级消息（不是某个任务）：它们的「任务码」位其实是固定词，不能聚合进列表。
_ACCOUNT_LEVEL_TAILS = (
    'school 活动非进行期',
    'school/tasks 拉取失败',
)

# 顺序有意义：先排掉「看着像失败、其实是降级成功」的那条。
_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # 降级成功（buddy5 失败 → 单发成功）：不是失败，按「已做」记 pending
    (re.compile(r'buddy5 失败 -> 降级单发'), 'pending'),
    (re.compile(r'失败'), 'failed'),
    # `claim 200 ok(credit=+300 energy=+5)` 是本轮真正入账的收尾行，但它既没有
    # 「已领」也没有「claimed」字样，早先落到默认 pending —— 结果是**刚领完的
    # 任务在摘要素里仍显示「待处理」**（实测一轮 +1700 分，列表全标待处理）。
    # 必须排在 pending 之前：同一任务前面还有 query/accept/report 等 pending 行。
    (re.compile(r'已领|已完成|claimed|claim \d+ ok'), 'claimed'),
    (re.compile(r'未达 target|未 completed|可点亮|可领|locked|dry-run|夜猫'
                r'|in_progress|无需上报|前置解锁|部分点亮|（点亮）|未变化'), 'pending'),
    # `query 任务不存在` 是「该账号没下发这条任务」，不是待办：标 skipped 才不会
    # 让人以为还能做。mp 口径那句自带 skip，本来就走这里。
    (re.compile(r'跳过|skip|非映射|未映射|不可伪造|无可用对象 id|未登记生效'
                r'|only_claim|未到上线时间|任务不存在'), 'skipped'),
)

# 状态选择优先级：一条任务打出多行时，取**结论最重**的那档。
# claimed 高于 pending 是因为「可点亮 → 已领取」是同一次运行的正常收尾；
# 反过来把 pending 留在上面会让已领完的任务仍显示「待处理」。
_PRIORITY = {'failed': 3, 'claimed': 2, 'pending': 1, 'skipped': 0}

# 展示顺序：与优先级**不同** —— 用户最想知道「还剩哪些没做」，所以 pending 在
# claimed 前面（已领取的看一眼就够，待处理的是行动项）。
_ORDER = {'failed': 3, 'pending': 2, 'claimed': 1, 'skipped': 0}


def classify(tail: str) -> str:
    """一行日志尾段 → 状态。认不出的按 `pending`（保守：宁可让人多看一眼）。"""
    for pattern, state in _RULES:
        if pattern.search(tail):
            return state
    return 'pending'


def summarize(lines: list[str]) -> list[dict]:
    """原始日志行 → 按任务聚合的列表（失败/待处理在前，同档按任务码排序）。

    每条：`{'code', 'state', 'line', 'count'}`（状态的**文案**由前端按语言渲染，
    后端只给机器可读的枚举，避免同一句话两处维护）。
    `line` 是该任务**最有代表性**的一条原始行（未翻译），前端按当前语言渲染。
    """
    best: dict[str, dict] = {}
    for raw in lines:
        m = _RUNNER.match(str(raw).strip())
        if not m:
            continue
        _uid, code, tail = m.group(1), m.group(2), m.group(3)
        if tail.startswith(_ACCOUNT_LEVEL_TAILS):
            continue
        state = classify(tail)
        entry = best.get(code)
        if entry is None:
            best[code] = {'code': code, 'state': state, 'line': str(raw).strip(),
                          'count': 1}
            continue
        entry['count'] += 1
        # 优先级更高，或同档但更靠后（最新）→ 换成这条做代表
        if _PRIORITY[state] >= _PRIORITY[entry['state']]:
            entry['state'] = state
            entry['line'] = str(raw).strip()

    out = list(best.values())
    out.sort(key=lambda it: (-_ORDER[it['state']], it['code']))
    return out
