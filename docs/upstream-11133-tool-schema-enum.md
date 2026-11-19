# Kimi 系模型：工具 schema 的 enum 缺 type 触发 11133，客户端参数错误快速失败

## 现象

请求 `cn:kimi-k3.1-pro` / `cn:kimi-k3-1` / `cn:kimi-k2.5` 一律 503，
`message` 内层是：

```json
{"code":11133,"msg":"Invalid request parameters",
 "extError":{"code":"400001","message":"the request parameters were rejected by the model provider"}}
```

同一份请求换 `deepseek-v4.1-flash` / `glm-5.3-flash` / `minimax-m3` 均 200。

## 根因（确定性复现）

工具参数 schema 里属性**只写 `enum`、不写 `type`** 时，Kimi 系模型严格校验直接 400：

```json
{"type":"object","properties":{"d":{"enum":["x","y"]}}}
```

补上 `type: "string"` 立刻 200。DeepSeek / GLM / MiniMax 接受同一 schema。
DSH 等客户端的整套工具定义里存在此类 schema，于是该模型对这些客户端等于全废。

## 放大链路（第二个缺陷）

11133 归 `ErrClient` → 网关逐账号轮转（每个账号都 400）→ 对外 `503 no_healthy_account`；
且 `ErrClient` 会喂 `NoteFailures`，把健康账号连败计数推高、临时踢出池
（现场实测：连续探测把 `healthy` 从 19 打到 7）。

## 修复

1. **schema 归一化**（`internal/upstream/payload.go`）：出站前给 `tools` / `functions`
   的 `parameters` 里「enum 缺 type」补推断类型（全字符串→string；整数→integer；
   整数+小数→number；布尔→boolean；混合→不改写，交给上游原样报错）。
   与既有 `normalizeToolPatterns` 同口径，属「让请求通过」的安全网。
2. **客户端参数错误快速失败**（`internal/server/handler.go`）：`ErrClient` 且 body 含
   `11133` 时返回 **400 + 上游原文**，不轮转、不进 `applyErrorPolicy`（不喂连败）。

## 验证

| 用例 | 修复前 | 修复后 |
| --- | --- | --- |
| kimi + enum 缺 type | 503 ×3 | **200 ×3** |
| kimi + enum 带 type | 200 | 200 ×3 |
| deepseek 对照 | 200 | 200 |
| 混合 enum（无法推断） | 503（轮转+惩罚账号） | **400 ×5**，池不受损 |
| 上游 11133 次数 | 3 次/请求（轮转） | 1 次/请求 |

单元测试：`internal/upstream/tool_enum_type_test.go`（6 组，含嵌套 items/anyOf/$defs、反例、旧式 `functions[]`）。
`go test ./internal/upstream/ ./internal/server/` 全过。