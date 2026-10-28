# 11102 负缓存：自愈 + 首次冷却 5 分钟

## 现象

```
503 {"code":"no_healthy_account",
     "message":"{\"code\":11102,\"msg\":\"model [deepseek-v4.1-flash] service info not found\",...}"}
```

同一时刻用**全部 20 个账号直连上游**该模型，**全部 200**（文本与图片都正常）—— 模型没有不可用。

## 根因（两层放大）

1. **瞬时 11102**：上游会间歇性对个别请求返回 `11102 model [...] service info not found`
   （模型注册表抖动 / 灰度切换）。实测：同一账号同一模型前后几秒，一个 11102、一个 200。
2. **6 小时起跳的负缓存**：`BlockModelBackoff` 对 `(账号, 模型)` 写负缓存，TTL 从 **6h**
   起、指数翻倍封顶 24h。一次抖动就会在轮转中给**每个账号**各写一条：
   - 第一个账号 11102 → 6h；
   - 换号 → 第二个账号同样的瞬时故障 → 再 6h；
   - 轮转走完一圈 → 全池写满 → **半天 503**，而模型早已恢复。

现场：`deepseek-v4.1-flash` 20/20 账号被写、`glm-5.3-flash` 6/20 被写，到期 16:32–22:36。

## 修复（patch 0031）

1. **首次冷却 6h → 5 分钟**（`modelBlockBaseTTL`），并把 `modelBlockShift` 6 → 12，
   保证「真·下线」的模型仍能按 5m→10m→20m… 指数升到 24h 封顶，而抖动只痛 5 分钟。
2. **`internal/pool/cooldown.go`**：新增 `ClearModelBlocks(model) int` —— 清掉所有账号上
   该模型的 11102 负缓存（只清 `Reason` 以 `11102` 开头的条目；6004 有自己的重置墙钟语义，不碰）。
3. **`internal/server/handler.go`**：选号失败分支自愈 —— 当 `AvailableUIDsForModel(model) == 0`
   时清一次负缓存、重置 `tried`、重试选号。**只清一次**（`modelBlocksCleared`），
   防止模型真下线时死循环；命中时打 WARN：
   `all accounts blocked by 11102 negative cache, cleared N and retrying`。
4. **测试**：新增 `TestClearModelBlocks`（清两条 / 重复清 0 / 6004 不被清 / 空模型名）；
   `TestBlockModelBackoffExponentialTtl` 的二次 TTL 期望改为由常量推导（不再硬编码 12h）。

## 行为边界

- 模型**真的**下线：自愈清一次后仍 11102，最终 503 并**透传上游原文**
  （`11102 model [...] service info not found`），客户端可直接判断是模型名问题；
  连续命中会把该账号的 TTL 指数抬到 24h。
- 部分账号缺该模型（合法情形）：`AvailableUIDsForModel > 0`，不触发自愈，仍按原负缓存避让。

## 验证

| 项 | 结果 |
| --- | --- |
| 单测 | `go test ./internal/pool/ ./internal/server/` 全过；`gofmt` 干净 |
| 注入 20 条负缓存 | 日志 `cleared 20 and retrying` → **200**，落盘 0 条 |
| 清理旧 6h 条目后压测 | 30 次「流式 + 图片」请求 **30/30 200**，结束后负缓存 0 条 |
| 抖动期观察 | 旧条目存在期间请求仍 200（未全池挡死），自愈未被触发 |