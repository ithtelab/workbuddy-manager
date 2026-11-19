# 回退路径上的 11102 不得升级为 (账号, 模型) 冷却

issue #245：15 个号的池子跑几小时后全部软冷却、所有请求 503，日志里 `11-128` 与
`11102` 混在一起。看上去像「换接口重试」，实际效果是**把每个账号锁 6 小时**。

## 链条

1. `/v2/chat/completions` 判渠道校验 → 400 `11-128`；
2. 0006 的兜底换到 `/console/chat/completions`；
3. `/console` 本来就不提供这个模型 → 400 `{"code":11102,"msg":"model [...] service info not found"}`；
4. `11102` 走 `BlockModelBackoff`，`modelBlockBaseTTL = 6h`，写进 `modelCooldowns`；
5. 选号侧 `healthyForModel` 自动避开该账号该模型 → 逐个账号重复 → 整池 503。

第 3 步的 11102 与「主路径自己报 11102」语义不同：它只是**这条路径没有这个模型**，
因为我们是因为渠道校验才换过去的。它不构成「该账号缺这个模型」的证据。

## 修法

新增错误种类 `ErrFallbackModelMissing`（`fallback_model_missing`），只在
`ChatStreamContext` 的候选路径循环里、`i > 0`（即回退路径）且分类结果为
`ErrModelBlocked` 时启用：

- **主路径的 11102 不变**：仍然是 `ErrModelBlocked`，(账号, 模型) 负缓存照写——
  那是「该后端确实没有这个模型」的确定性答复。
- handler 侧零动作（不冷却 / 不熔断 / 不 `NoteError`），**仍然轮转**：换个账号
  可能压根不需要回退（渠道校验与账号灰度有关）。
- 2026-10-28 起（`0039-channel-check-request-scope.patch`）：`/v2` 已经明确判了
  渠道校验、`/console` 只报 11102 时，返回**主路径的 11128 原文**，不再让备用路径
  的「无此模型」把真实原因盖掉。请求仍只按请求级处理，不写任何账号冷却。
- 没有降级成 `ErrClient`：`ErrClient` 会喂连败计数（`NoteFailures`，issue #114），
  而连败是**账号健康信号**——这里失败的是路径，不是账号。若照 `ErrClient` 处理，
  渠道校验期间每个账号都会攒连败，攒够阈值照样临时出池。

## 与 #246 的关系

`0017` 修的是**触发源**（DSH 的 system 首句指纹）；本条修的是**放大器**。
两者独立：即使指纹都被净化，其它客户端/其它句式仍可能触发渠道校验，只要回退路径
仍写 6 小时模型冷却，故障就会放大成整池 503。所以两条都要有，且这条不能靠「先把
指纹补全」来替代——指纹是黑名单，永远补不完。

## 验证

- `go build ./...`、`go test ./...`（Go 1.22.5，与 `go.mod` / 发布 CI 同版本）全过。
- 客户端层用例：`/v2` 回 `11-128`、`/console` 回 `11102` → 分类为
  `ErrFallbackModelMissing`，且确实走了两条路径；主路径单独回 `11102` 仍是
  `ErrModelBlocked`（防止降级写宽）。
- handler 层用例：同一现场下断言 `RateLimitedModels` 为空、`ConsecutiveFails == 0`、
  账号不 `cooling` / 不 `disabled`、响应不是 200（本次请求仍失败，但账号回池）。

## 补丁与资产

实现位于 `dev/upstream-patches/0018-console-fallback-model-missing.patch`，基线为
`upstream-src` 资产 + `0016` + `0017`。重打包后（312 个文件，**待维护者上传**）：

```text
size:   907152
sha256: b16c081dc70bb1608846190640401f4fac375f4a9f78e2643d062790abf1da8a
```

> 与 `0017` 一样：合并后必须先重打包并覆盖上传 `upstream-src` 载体 Release，
> 否则发布门禁会报「仍有补丁需要现场应用」并阻断发布。
