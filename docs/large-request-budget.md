# 大上下文：从「逐请求轮换」改为「账号级预算 + 会话迁移」

> 关联：`0044-large-request-rotation.patch`（引入逐请求轮换）、
> `0046-large-request-cache-friendly.patch`（v2.11.9 改为默认关闭）、
> 本文件对应的 `0048-large-request-budget.patch`。

## 为什么不再用逐请求轮换

v2.11.9 关掉轮换的理由成立：**上游前缀缓存按账号存放**，换号后整段上下文都算未命中。

同时，逐请求轮换想挡的「画像」在健康账号上也普遍存在。同一号在 **120 秒窗口内承接
`>50 万 token` 请求**的最大条数（本机 6473 条样本）：

| 账号 | 窗口峰值 | 状态 |
| --- | --- | --- |
| 40f52337 | 18 | 曾被限制 |
| be4767fd | 15 | 正常 |
| 41836698 | 15 | 正常 |
| 60ee35ad | 15 | 正常 |
| 65ff542a | 14 | 正常 |
| 80be4b49 | 14 | 曾被限制 |

封与不封，峰值同量级 —— 轮换买到的保护无法证实，代价（丢缓存）却是确定的。

## 新方案：粘性不动，只对「热号」让一次

- 会话粘性完全保留（每条对话固定一个号，前缀缓存照常命中）
- 每个账号维护一个滑动窗口（默认 **120s**）内的大请求计数
- 计数达到预算（默认 **6 条**）后，**下一条大请求**不再给它 → 选另一个号 → **会话迁移过去并重新绑定**
- 迁移只发生一次：之后该对话固定在新号上，继续命中新号的前缀缓存
- 窗口滑过后旧号自动恢复

也就是说：90% 的请求完全不受影响；只有「一个号在 2 分钟内被压了 6 条以上超大请求」时才分流。

## 配置

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `large_request_bytes` | `2097152`（2 MiB ≈ 52 万 token） | 判定「大请求」的阈值；`<=0` 关闭整条策略 |
| `large_request_budget` | `6` | 窗口内每号最多承接几条大请求；`<=0` 关闭预算（退回 legacy 轮换） |
| `large_request_budget_window` | `"120s"` | 预算的滑动窗口 |
| `large_request_cooldown` | `"0s"` | legacy 逐请求轮换；仅当预算 <=0 时生效 |

推荐配置（本 PR 默认值）：

```json
"pool": {
  "large_request_bytes": 2097152,
  "large_request_budget": 6,
  "large_request_budget_window": "120s",
  "large_request_cooldown": "0s"
}
```

## 阈值为什么是 2 MiB

现场 6 个被限制账号的最后几条请求都是 **61–65 万 token**（≈2.4–2.6 MB）：

| 阈值 | ≈ token | 相对 61–65 万 | 效果 |
| --- | --- | --- | --- |
| 256 KiB | ≈ 6.5 万 | 远低于 | 正常长对话也被判大请求，缓存损失大 |
| **2 MiB** | **≈ 52 万** | **低于** | 封号形态的请求仍被计数/分流，更小的对话不受影响 |
| 3 MiB | ≈ 78 万 | 高于 | 封号形态的请求不再被识别 |

## 测试

| 用例 | 覆盖 |
| --- | --- |
| `TestLargeBudgetDefaults` | 默认阈值/预算/窗口/legacy 关闭；单条大请求不排除 |
| `TestLargeBudgetExhaustion` | 5 条不排除、第 6 条排除、窗口过后恢复并清理 |
| `TestLargeExclusionsWindowLegacyRotation` | 显式关预算 + 设 cooldown 时退回逐请求轮换 |
| `TestLargeExclusionsDisabled` | 阈值 <=0 时整条策略关闭 |
| `TestChatLargeBudgetMigratesAfterExhausted` | 预算 1 时：首条绑定 A，第二条迁移到 B 并重新绑定 |
| `TestChatLargeRequestKeepsStickyByDefault` | 默认配置下大请求仍写会话绑定（缓存保留） |

补丁在 v2.11.9（`aa7048a`）上 `git apply` 干净应用；`go build ./...` 与
`go test ./internal/server/ ./cmd/server/` 全绿。