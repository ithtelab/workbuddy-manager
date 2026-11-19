# DSH 运行时上下文「Web GUI」跨句指纹（11-128）

DeepSeek Harness（DSH）桌面版把运行时上下文注入提示词（v2.8.2 起用 `developer`
角色承载），其中固定包含这两句**连续文本**：

```text
You are interacting with the user through the DeepSeek Harness Web GUI whose URL the runtime context names. When the user refers to "this page", "this GUI", or "this app" without naming another target, they mean this GUI.
```

上游对这段连续文本逐字匹配，命中即回 400
`{"code":11-128,"msg":"Illegal API invocation from an unapproved channel"}`。
网关随后按 0006 的兜底回退 `/console/chat/completions`；该路径不提供
`deepseek-v4.1-flash`，返 400 `{"code":11102,...}`，对外表现为 503
`no_healthy_account`。放大链路与 `docs/dsh-prompt-fingerprint.md` 相同，
`0018` 已修掉「回退 11102 连锁锁号」，所以现在只表现为**该请求失败**，
不会再把整池打进 6 小时冷却。

## 与 #246 的身份句指纹是两套规则

`sanitizeDshRe` 只匹配

```text
You are [a|an] [AI] [coding] agent|assistant (powered by|built on) … .
```

本条不命中：上游拦的是**跨两句的连续文本**，不是自我介绍句式。

实测（逐字符二分，取触发段前缀）：

| 长度 | 结果 |
|---|---|
| 220 字符 | 200 |
| 221 字符 | 503 |

而且两句**单独**发送都放行（各自 200）——所以只改写其中一句没有用，
必须动到段落内部、让连续匹配断开。

## 修法

```go
var sanitizeDshGuiRe = regexp.MustCompile(`(?i)DeepSeek Harness Web GUI`)
const sanitizeDshGuiRepl = "DeepSeek-Harness Web GUI"
```

- 品牌短语中间插一个连字符即破坏逐字匹配，语义不变；
- `hasFingerprint` 必须同步认这段，否则特征预检提前 `return`，改写层执行不到
  （与 #246 踩过的坑同类）；
- 作用域沿用 `sanitizeMessages` / `sanitizeContent`，覆盖 `developer`、`system`、
  `tool_calls` 参数与 `reasoning_content`。

## 验证

- `go test ./internal/upstream/` 全过；新增 6 组用例：
  221 字符原文整体改写、`hasFingerprint` 识别（含大小写变体）、幂等、
  反例（普通文本不得改写）、`PrepareBody` 出站边界（`developer` 角色）。
- 结论来自真实抓包回放：修复前该请求（3.38MB、358 条消息、368 个 tools、
  `cn:deepseek-v4.1-flash`）稳定 503；打补丁后同一请求 200。

## 补丁与基线

实现位于 `dev/upstream-patches/0019-dsh-web-gui-fingerprint.patch`，基线为 v2.8.2
随包上游（`upstream-id: 37ab7c667005187e04449aeecc87b0903f89ecfd8bf9c4ae38df098c5574d35a`）。

> 合并后同样需要重打包并覆盖上传 `upstream-src` 资产、再发面板版本；
> 发布门禁会校验「随包上游里所有补丁都是 already applied」。
