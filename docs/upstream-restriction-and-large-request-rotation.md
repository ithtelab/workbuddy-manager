# 上游风控限制不再永久禁用 + 大上下文请求逐请求轮换

两个改动解决的是同一类现场问题：**账号池被上游风控逐个打掉，而网关自己的处理把
「临时限制」变成了「永久出池」。**

---

## 一、11140「账号异常行为」的处理（0043 → 0047 已改为硬禁用）

### 现场

CN 账号调用 `/v2/chat/completions` 返回：

```json
{"code":11140,"msg":"request illegal","requestId":"...",
 "displayMsg":{"zh":"账号存在异常行为，已被限制使用","en":"Your account has been restricted due to unusual activity"},
 "displayTips":{"zh":"系统检测到该账号存在异常使用行为，如需申诉请提交反馈"},
 "actions":["EDIT_INPUT","SUBMIT_FEEDBACK"]}
```

判定依据（同一现场实测）：

| 观察 | 结论 |
| --- | --- |
| 23 个账号全部同出口 IP，其中 17 个正常 200 | 不是 IP 被封 |
| 无 UA、无会话头、正文 `ping` 的极简请求也返回 11140 | 不是逐请求的 UA/指纹校验 |
| 非 chat 接口（模型目录）仍 200 | 凭证没坏，限制只作用在 chat |
| 同一账号数小时后恢复 200 | 限制会自行解除，可申诉 |

而 `applyErrorPolicy` 把 `request illegal` 一律 `Disable`，并写
`re-login required` —— 6 个还能恢复的号被永久踢出池子，面板显示「已封禁，需重新登录」。

### 改动

- 新增 `upstream.IsUpstreamRestriction(body)`：**只认带风控话术的 11140**
  （`unusual activity` / `异常行为` / `異常行為` / `abnormal`）。
- 命中时按账号级软冷却 45 分钟，理由 `upstream restriction (code 11140, http 403, appealable)`，
  **（2026-11-02 起由 0047 改为硬禁用，见下方「0047 的修正」）**
  **不 Disable、不清凭证**。
- 不带风控话术的 `request illegal`（历史 global `auth_forbidden` 语义）**行为不变**，仍硬禁用。

### 验证

| 项 | 结果 |
| --- | --- |
| 线上：12 条大请求（必然踩到 2 个受限号） | **12/12 200**；2 个号进入 **45 分钟软冷却**，`disabled: 0` |
| 单测 | `TestIsUpstreamRestriction`（5 例）、`TestApplyErrorPolicyUpstreamRestrictionCools`、`TestApplyErrorPolicyPlainRequestIllegalStillDisables` |

---


### 0047 的修正（issue #376）

0043 的软冷却上线后，现场反馈账号会一直在「冷却中」循环、面板看不出哪个号被封，也
不会提示去申诉。0047 把 11140 + 风控话术恢复为**硬禁用**，理由写成
`account banned by upstream (…, 账号存在异常行为/已被限制使用, appeal required,
re-login ineffective)`：面板据此显示「账号被封禁」、移出重登队列（与 #361 同一口径），
不再每 45 分钟对已封账号重复发一次请求。不带风控话术的 `request illegal` 行为不变。

0047 依赖 0043（修改的是 0043 引入的分支与用例），必须在 0043 之后应用。

## 二、大上下文请求逐请求轮换（0044）

### 现场

被封的 6 个号，最后几条请求形态完全一致：

```
key=38 / UA=deepseek-harness / 客户端 IP 222.85.110.6
单请求 prompt = 610k–650k token（≈2.4–6 MB 报文，含 tool 截图）
间隔 7–14 秒连发，且在账号池里轮换
```

也就是说：**单个号在几十秒内被连续压了多个超大上下文请求**，上游把这种形态判为
「异常使用行为」。除了「别让一个号短时间吃这么多大请求」，没有更低风险的改法。

### 改动

`config.pool` 新增两个键：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `large_request_bytes` | `262144`（256 KiB） | 请求体 ≥ 该值视为大上下文请求；`<=0` 关闭 |
| `large_request_cooldown` | `"0s"`（关闭） | 大于 0 才启用逐请求轮换，窗口内不再把大请求给同一个号 |

`large_request_cooldown` 大于 0 时，请求体超阈值会：

1. **放弃会话粘性**（不按会话绑号，成功后也不写绑定）；
2. 选号时排除窗口内已承接过大请求的号 → **逐请求轮换**；
3. 被选中即登记（无论成败），保证窗口内不复用；
4. 若所有号都在窗口内，退化为普通轮换，避免直接 503。

默认 `"0s"` 时完全不轮换，大请求与小请求一样按会话粘性选号。原因见下节。

### 修正：默认关闭轮换（0046，2026-11-01）

0044 上线当天，长上下文的缓存命中率从 97% 掉到 50–70%。同一账号池、同一模型实测：

| 请求体 | 连发两次的结果 |
| --- | --- |
| 120 KB | 第一次命中 0，第二次命中 27776（全部前缀） |
| 205 KB | 第一次命中 27904（上一轮前缀），第二次命中 47488 |
| 300 KB（触发轮换） | 两次都命中 0 |

上游的前缀缓存按账号存放（与 `server/services/accountlog.py` 的说明一致）：
换号后整段上下文都算未命中，输入侧按全量计费。300 KB 的请求体在长对话里很常见，
轮换等于让每一个多轮长对话都反复重算前缀。

风控侧的收益则没有被验证：0044 只是把同一批大请求分摊到更多账号，总请求量与内容
不变。因此默认关闭轮换，把选择权交给运维：确实观察到 11140 限制时再把
`large_request_cooldown` 设为 `"90s"`，接受这些请求的缓存代价。

### 验证

| 项 | 结果 |
| --- | --- |
| 连发 10 条 300 KB 请求（轮换开启） | 10 个不同账号、各 1 次，全部 200 |
| 连发 10 条小请求（同 UA） | 仍集中 1 个账号（粘性未变） |
| 单测 | `TestLargeRotationOffByDefault` / `TestLargeExclusionsWindow` / `TestLargeExclusionsDisabled` |
| 端到端 | `TestChatLargeRequestKeepsStickyByDefault`（默认写绑定）/ `TestChatLargeRequestRotationDoesNotBind`（显式开启后不写） |

---

## 应用方式

```bash
cd upstream   # v2.11.6 源码根
git apply /path/to/0043-upstream-restriction-soft-cooldown.patch
git apply /path/to/0044-large-request-rotation.patch
git apply /path/to/0047-11140-ban-state.patch
git apply /path/to/0046-large-request-cache-friendly.patch
go build ./... && go test ./internal/server/ ./internal/upstream/
```

0043 与 0044 互相独立（0044 不依赖 0043）；0046 修正 0044 的默认值，必须在 0044 之后应用；0047 修正 0043 的 11140 处理，必须在 0043 之后应用。

## 未纳入本 PR 的本地改动

本部署另有 `MaxRotate 3→6` 与「401 先刷新再计数」两项本地增强，与本次两个缺陷无关，
未包含在本 PR 中。