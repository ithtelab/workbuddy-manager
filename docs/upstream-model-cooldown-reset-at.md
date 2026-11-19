# 6004 模型级限流重置时间持久与台账保留（0056）

## 现象

用户在调用 `hy4free` 触发上游 6004（超额限流，如提示「将在明日 06:01 重置」）后：
1. 经过 `soft_rate_max`（默认 2 小时）软冷却窗口后，或发生定时签到（`ReenableIfCredits`）/ 账号级冷却时，上游网关 `/status` 接口的 `rate_limited_models` 条目消失（变成空/正常状态）。
2. 监控面板（如 OmniPanel）据此误以为账号已恢复，显示为“可用（绿点）”。
3. 但实际向网关发起调用时，上游再次拒绝并返回 6004，导致调用失败并重新进入 2 小时冷却循环。

## 根因

在 `internal/pool` 中存在两处导致模型级限流提前失忆的问题：
1. **Until 截断过期误删**：`modelCooldown` 结构体记录了被截断的试探冷却时间 `Until` 与上游真实重置墙钟 `ResetAt`。历史代码在 `rateLimitedModelsLocked`（状态台账）、`pruneExpiredModelCooldowns`（惰性清理）、`modelCooled`（选号过滤）以及 `persist.go`（状态落盘/恢复）中，**均单纯以 `now.Before(mc.Until)` 作为唯一的有效性判据**。当 `now >= mc.Until` 时，即使 `now < mc.ResetAt`，记录仍被误判为过期并丢弃。
2. **签到解冻与账号级冷却抹除台账**：`transition.go` 的 `clearCoolingLocked()` 以及 `cooldown.go` 中的 `Cooldown` / `CooldownSoftRate` 无条件执行 `e.modelCooldowns = nil`。由于每日签到会在查得余额后调用 `ReenableIfCredits` -> `reviveCoolingLocked` -> `clearCoolingLocked`，导致签到成功后账号的 6004 独立模型限流直接被清零。事实上，签到获得积分仅能解除“余额不足（402）”导致的账号级冷却，上游免费模型（`hy4free`）的频控重置时间是由腾讯服务端控制的，与账号拥有的积分毫无关系；且 `healthyForModel` 优先检查账号级 `healthy(now)`，保留活跃的模型限流并不会造成模型豁免泄漏。

## 修法（0056）

1. 在 `modelCooldown` 引入统一的生命周期方法：
   - `active(now)`：只要 `ResetAt` 在未来或 `Until` 在未来，均视为仍在冷却中；
   - `effectiveUntil(now)`：若 `Until` 窗口已过但 `ResetAt` 仍在未来，有效截止延伸到 `ResetAt`。
2. 状态接口 `rateLimitedModelsLocked`、选号判定 `modelCooled`、内存清理 `pruneExpiredModelCooldowns` 与持久化 `persist.go` 全链路对齐 `active(now)`。
3. `clearCoolingLocked`：仅清空账号级冷却（`until`、`coolKind`、`reason`、`softStreak`），模型冷却改为调用 `pruneExpiredModelCooldowns(time.Now())` 惰性清理过期条目，保留活跃的模型级限流；仅在账号彻底被禁用的终态（`disableLocked`）显式清空。
4. `cooldown.go`：`Cooldown` 与 `CooldownSoftRate` 避免粗暴置空 `modelCooldowns`，改为仅 prune 过期条目。
5. 补充测试：更新 `TestModelCooldownUntilExpiredButResetAtFutureKeepsStatusAndCooldown`、`TestModelCooldownPersistWhenUntilExpiredButResetAtFuture`、`TestModelCooldownsRetainedOnRevive` 与 `TestTransitionReviveClearsCoolingKeepsBreaker`。
