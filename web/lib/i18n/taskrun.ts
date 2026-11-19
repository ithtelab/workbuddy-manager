/**
 * 「成长任务一键执行」面板输出行的本地化（issue #19 · 多语言补漏）。
 *
 * 面板回显的是**上游脚本 task_runner.py 的原始 stdout**：\`/api/task-run\` 直接把
 * 子进程输出原样塞进 lines，界面上就是 <pre> 一贴。那批日志文案写死在上游脚本里，
 * 而它是**另一个仓库**（以 volume 挂载进来）：改上游会被它的升级覆盖，把译文写回
 * 数据库留痕更是错的。所以本地化放在**展示层** —— 这里按「模板 → 译文」逐行匹配，
 * 命中就按当前语言重排，输出与存储的原文都不动。
 *
 * 三点取舍：
 *   · 未命中原样返回：上游改文案或新增行时，最坏是继续显示中文原文，
 *     而不会出现空行、键名或丢日志 —— 日志完整性优先于翻译率；
 *   · 动态片段（uid8 / 任务码 / 计数 / id 来源 / 异常文本）原样保留：
 *     它们是数据，翻过去反而不便与上游日志对照；
 *   · 简体中文（源语言）下译文模板就是上游原文，输出与改造前逐字一致。
 *
 * 只匹配**完整一行**（^…$）：task_code 与上游 msg 都是自由文本，宽松匹配有把行内
 * 普通内容当模板吞掉的风险。
 */
import {t as tStatic, tp as tpStatic} from './index';

/** 一行日志的匹配规则：命中即按 build 重排；顺序有意义（具体者在前）。 */
type LineRule = [RegExp, (m: RegExpMatchArray) => string];

/** 上游 task_runner 的通用行前缀：\`[task_runner] <uid8> <task_code>: \`。 */
const RUNNER = '\\[task_runner\\] (\\S+) (\\S+): ';

/** 补上 \`^…$\` 与通用前缀，避免每条规则重复一长串转义。 */
function runner(tail: string): RegExp {
  return new RegExp('^' + RUNNER + tail + '$');
}

const RULES: LineRule[] = [
  // ── 上游拉取失败回落内置表（场景 / 专家 / 技能 / 案例 / 主题）──────
  // 主语（场景清单 / 专家市场 …）与 id 来源一样是**数据**，走短语表。
  [/^\[warn\] (.+?)拉取失败\((.*)\)，回落内置表$/, (m) =>
    tStatic('tasks.runLogWarnFallback', {what: tpStatic(m[1]), err: m[2]})],
  [/^\[warn\] COS 专家清单失败\((.*)\)$/, (m) =>
    tStatic('tasks.runLogWarnCosExperts', {err: m[1]})],

  // ── 领奖 400 降级 web 域 ────────────────────────────────────────
  [runner('claim (\\d+) (.*?) -> 降级 web 域'), (m) =>
    tStatic('tasks.runLogClaimFallback', {uid: m[1], code: m[2], status: m[3], msg: m[4]})],

  // ── query 阶段的跳过分支 ────────────────────────────────────────
  // 「已领，跳过(非映射任务)」与下面带计数的「已领，跳过」同前缀不同尾，
  // 放在前面是为了以后放宽规则时不会互相抢占（两条现在都能各自命中）。
  [runner('query claimed -> 已领，跳过\\(非映射任务\\)'), (m) =>
    tStatic('tasks.runLogClaimedUnmapped', {uid: m[1], code: m[2]})],
  [runner('query 非映射任务，skip'), (m) =>
    tStatic('tasks.runLogNotMapped', {uid: m[1], code: m[2]})],
  [runner('query (\\S+) -> 非映射任务，skip'), (m) =>
    tStatic('tasks.runLogNotMappedStatus', {uid: m[1], code: m[2], status: m[3]})],
  [runner('query 任务不存在'), (m) =>
    tStatic('tasks.runLogTaskMissing', {uid: m[1], code: m[2]})],
  [runner('query (\\S+)\\((\\d+)/(\\d+)\\) -> 不可伪造\\((.*)\\)，skip'), (m) =>
    tStatic('tasks.runLogUnforgeable', {
      uid: m[1], code: m[2], status: m[3], cur: m[4], target: m[5],
      reason: tpStatic(m[6]),
    })],
  [runner('query (\\S+)\\((\\d+)/(\\d+)\\) -> 未 completed，only_claim 跳过'), (m) =>
    tStatic('tasks.runLogOnlyClaimNotDone', {
      uid: m[1], code: m[2], status: m[3], cur: m[4], target: m[5],
    })],
  [runner('query (\\S+)\\((\\d+)/(\\d+)\\) -> only_claim dry-run 跳过'), (m) =>
    tStatic('tasks.runLogOnlyClaimDryRun', {
      uid: m[1], code: m[2], status: m[3], cur: m[4], target: m[5],
    })],
  [runner('query claimed\\((\\d+)/(\\d+)\\) -> 已领，跳过'), (m) =>
    tStatic('tasks.runLogClaimed', {uid: m[1], code: m[2], cur: m[3], target: m[4]})],
  [runner('query (\\S+)\\((\\d+)/(\\d+)\\) -> 可领\\(claim\\)，dry-run 跳过'), (m) =>
    tStatic('tasks.runLogClaimableDryRun', {
      uid: m[1], code: m[2], status: m[3], cur: m[4], target: m[5],
    })],
  [runner('query in_progress\\((\\d+)/(\\d+)\\) -> 非夜猫窗口\\(23-08 CST\\)，skip pending'), (m) =>
    tStatic('tasks.runLogNightCatClosed', {uid: m[1], code: m[2], cur: m[3], target: m[4]})],
  [runner('query in_progress\\((\\d+)/(\\d+)\\) -> 夜猫窗口内，可补 1 次，dry-run 跳过'), (m) =>
    tStatic('tasks.runLogNightCatOpen', {uid: m[1], code: m[2], cur: m[3], target: m[4]})],
  [runner('query (\\S+)\\((\\d+)/(\\d+)\\) -> 可点亮 need=(\\d+) id源=(.+?) ids=(\\[.*\\])，dry-run 跳过'), (m) =>
    tStatic('tasks.runLogLightUpDryRun', {
      uid: m[1], code: m[2], status: m[3], cur: m[4], target: m[5],
      need: m[6], src: tpStatic(m[7]), ids: m[8],
    })],

  // ── 点亮（写操作）阶段 ──────────────────────────────────────────
  [runner('accept (\\d+) (.*)'), (m) =>
    tStatic('tasks.runLogAccept', {uid: m[1], code: m[2], status: m[3], msg: m[4]})],
  [runner('report 无需上报（(\\d+)/(\\d+)）'), (m) =>
    tStatic('tasks.runLogReportNotNeeded', {uid: m[1], code: m[2], cur: m[3], target: m[4]})],
  [runner('report (\\d+)/(\\d+) (\\d+) code=(\\S+) buddy5 失败 -> 降级单发 (\\d+) code=(\\S+)'), (m) =>
    tStatic('tasks.runLogBuddy5Fallback', {
      uid: m[1], code: m[2], i: m[3], need: m[4], status: m[5],
      sc: m[6], status2: m[7], sc2: m[8],
    })],
  [runner('report 无可用对象 id，skip'), (m) =>
    tStatic('tasks.runLogNoObjectId', {uid: m[1], code: m[2]})],
  [runner('report 未达 target（(\\d+)/(\\d+)），WARN 待下次'), (m) =>
    tStatic('tasks.runLogBelowTarget', {uid: m[1], code: m[2], cur: m[3], target: m[4]})],

  // ── 上游 2026-09-23（ee3c694 / 9a26ae7）「对象 id 池不够」的两行 ────
  // 成长任务续作（Sequential_Tasks_2/3）引入了「专家市场」这类**动态对象池**：
  // 池子为空、或数量不够本轮目标时会各打一行。`kind` 是任务类型标识（数据，
  // 原样保留，与上面 `runLogLightUpKindDryRun` 的处理一致）。
  [runner('(\\S+) 无可用对象 id，WARN 跳过点亮'), (m) =>
    tStatic('tasks.runLogNoObjectIdSkipLight', {uid: m[1], code: m[2], kind: m[3]})],
  [runner('可用对象 id (\\d+) < 需 (\\d+)，本轮按可用数上报'), (m) =>
    tStatic('tasks.runLogIdsFewerThanNeeded', {
      uid: m[1], code: m[2], have: m[3], need: m[4]})],

  // ── 账号级：任务列表拉取失败 / 全量批量警告 / 国际版跳过 ──────────
  [/^ERR: \[task_runner\] (\S+) query list_tasks 失败: (.*)$/, (m) =>
    tStatic('tasks.runLogListTasksFailed', {uid: m[1], err: m[2]})],
  [/^WARN: 全量批量 \+ --yes 未限定 --only，注意 54 号批量——请确认 Hermes 决策后再跑$/, () =>
    tStatic('tasks.runLogBulkWarning')],
  [/^\[skip\] (\S+) global realm 不适用 CN 任务$/, (m) =>
    tStatic('tasks.runLogGlobalSkip', {uid: m[1]})],

  // ── 上游 2026-09-17（7e43884）新增/改写的行 ────────────────────────
  // 这一批来自上游的「accept 登记验证与重试」「小程序成长任务接入」
  // （school_season 校园日 / minichat 等）与 first_buddy 领养链路。
  // 顺序：具体者在前，`{status}` 这类通配形态放最后，免得把细节行吞掉。

  // 领取结果（含「已领」变体；{ast} 是上游的状态枚举，属数据）
  // 注意顺序：带进度 `status/cur` 的那条**必须在前** ——
  // `(\S+)` 能把 `in_progress/3` 整个吞下，放后面就永远轮不到它。
  [runner('(\\S+)/(\\d+) -> claimed（本轮已入账）'), (m) =>
    tStatic('tasks.runLogClaimedThisRoundProgress', {
      uid: m[1], code: m[2], status: m[3], cur: m[4]})],
  [runner('(\\S+) -> claimed（本轮已入账）'), (m) =>
    tStatic('tasks.runLogClaimedThisRound', {uid: m[1], code: m[2], status: m[3]})],

  // 点亮结果三分支：只有前缀「（未变化）/（点亮）/（部分点亮…）」是文案
  // 顺序同样是「部分点亮」在前 —— 它比「（点亮）」更长，但 `（点亮）` 那条用的是
  // `$` 锚定整行，两者不会互相吞；这里保持具体者优先的习惯。
  [runner('(\\S+) -> (\\S+)/(\\S+)（部分点亮，未达 target）'), (m) =>
    tStatic('tasks.runLogLightPartial', {
      uid: m[1], code: m[2], status: m[3], after: m[4], afterProg: m[5]})],
  [runner('(\\S+) -> (\\S+)/(\\S+)（未变化）'), (m) =>
    tStatic('tasks.runLogLightNoChange', {
      uid: m[1], code: m[2], status: m[3], after: m[4], afterProg: m[5]})],
  [runner('(\\S+) -> (\\S+)/(\\S+)（点亮）'), (m) =>
    tStatic('tasks.runLogLightDone', {
      uid: m[1], code: m[2], status: m[3], after: m[4], afterProg: m[5]})],

  // accept 登记验证与重试（上游 c793ae3）
  // 上游原文：`accept 尝试{N} {http状态} status={返回的status} 回读={回读到的accept_status}{' -> 生效' if ok}`
  // 「-> 生效」只在回读确认登记成功时出现。它**不是**自由文本，而是上游写死的
  // 固定短语，所以走短语表（tpStatic）而不是硬编码一个占位值 —— 早先我传的是
  // `'1'`/`''`，界面上会直接显示「回读=accepted1」，那个 1 毫无意义。
  // 注意捕获组序号：runner() 先占了 uid/code 两个，故 attempt 从 3 起。
  [runner('accept 尝试(\\d+) (\\d+) status=(\\S+) 回读=(\\S+)( -> 生效)?'), (m) =>
    tStatic('tasks.runLogAcceptVerify', {
      uid: m[1], code: m[2], attempt: m[3], st: m[4], status: m[5], readback: m[6],
      ok: m[7] ? tpStatic('-> 生效') : ''})],
  [runner('accept 未登记生效，本轮跳过待下次'), (m) =>
    tStatic('tasks.runLogAcceptNotRegistered', {uid: m[1], code: m[2]})],

  // 小程序成长任务（school_season 校园日 / minichat 等）
  [runner('query (\\S+)\\((\\d+)/(\\d+)\\) -> 可点亮\\((\\S+)\\)，dry-run 跳过'), (m) =>
    tStatic('tasks.runLogLightUpKindDryRun', {
      uid: m[1], code: m[2], status: m[3], cur: m[4], target: m[5], kind: m[6]})],
  [runner('query (\\S+)\\((\\d+)/(\\d+)\\) -> 已完成/已领，跳过'), (m) =>
    tStatic('tasks.runLogAlreadyDone', {
      uid: m[1], code: m[2], status: m[3], cur: m[4], target: m[5]})],
  // 上游 2026-09-24（06878fd / 1580a86，成长任务续作）新增：任务还没到上线时间。
  // 解锁时间是数据（可能是 "?"）——用 `(.+?)` 而不是 `(\S+)`：上游给的是时间戳
  // 形态，含空格时 `\S+` 会匹配不上，那一行就退回中文原文（不报错，只是没翻译）。
  [runner('query locked（解锁 (.+?)）-> 未到上线时间，跳过'), (m) =>
    tStatic('tasks.runLogTaskLocked', {uid: m[1], code: m[2], unlock: m[3]})],
  [runner('query (\\S+) -> school 未映射\\(人工/未知\\)，skip'), (m) =>
    tStatic('tasks.runLogSchoolUnmapped', {uid: m[1], code: m[2], status: m[3]})],
  [runner('report (\\S+) code=(\\S+) 前置解锁'), (m) =>
    tStatic('tasks.runLogPreUnlock', {uid: m[1], code: m[2], status: m[3], sc: m[4]})],
  [runner('viewed 激活 (\\S+)'), (m) =>
    tStatic('tasks.runLogViewedActivated', {uid: m[1], code: m[2], status: m[3]})],
  [runner('viewed 失败: (.*)'), (m) =>
    tStatic('tasks.runLogViewedFailed', {uid: m[1], code: m[2], err: m[3]})],
  [runner('report 失败 #(\\d+): (.*)'), (m) =>
    tStatic('tasks.runLogReportFailed', {uid: m[1], code: m[2], i: m[3], err: m[4]})],
  [runner('claim 失败: (.*)（可稍后补领）'), (m) =>
    tStatic('tasks.runLogClaimFailed', {uid: m[1], code: m[2], err: m[3]})],

  // mp（小程序）口径的分支
  [runner('mp list_tasks 失败: (.*)'), (m) =>
    tStatic('tasks.runLogMpListFailed', {uid: m[1], code: m[2], err: m[3]})],
  [runner('mp 口径任务不存在，skip'), (m) =>
    tStatic('tasks.runLogMpTaskMissing', {uid: m[1], code: m[2]})],
  [runner('only_claim 跳过（未 completed）'), (m) =>
    tStatic('tasks.runLogOnlyClaimNotCompleted', {uid: m[1], code: m[2]})],

  // 账号级（注意：这两条的账号位是 uid8，后面接的是固定词而非任务码）
  [/^\[task_runner\] (\S+) school 活动非进行期（in_period=false），school 段跳过$/, (m) =>
    tStatic('tasks.runLogSchoolNotInPeriod', {uid: m[1]})],
  [/^\[task_runner\] (\S+) school\/tasks 拉取失败: (.*)$/, (m) =>
    tStatic('tasks.runLogSchoolFetchFailed', {uid: m[1], err: m[2]})],
  [/^\[task_runner\] (\S+) (\S+): mp 查询失败: (.*)$/, (m) =>
    tStatic('tasks.runLogMpQueryFailed', {uid: m[1], code: m[2], err: m[3]})],

  // ── 后端子进程看护自己追加的行（server/services/taskrun.py）──────
  // 后端保持中文原文（\`test_taskrun.py\` 的卡死用例按「卡死」断言），
  // 翻译只发生在展示层。
  [/^!! 运行超过 (\d+) 小时上限，已终止$/, (m) =>
    tStatic('tasks.runLogKilledTotal', {hours: m[1]})],
  [/^!! 已 (\d+)s 无输出，判定卡死并终止$/, (m) =>
    tStatic('tasks.runLogKilledIdle', {idle: m[1]})],
  [/^!! 已手动停止$/, () => tStatic('tasks.runLogManuallyStopped')],
];

/**
 * 把一行面板输出翻译成当前语言；不是已知模板就原样返回。
 *
 * 行首缩进单独保留：上游的 [warn] 行带两空格缩进，译文模板里不带，
 * 免得每种语言都要重复一遍空白。
 */
export function translateRunLine(line: string): string {
  const m = /^(\s*)([\s\S]*)$/.exec(line);
  const indent = m ? m[1] : '';
  const body = m ? m[2] : line;
  for (const [pattern, build] of RULES) {
    const hit = pattern.exec(body);
    if (hit) return indent + build(hit);
  }
  return line;
}
