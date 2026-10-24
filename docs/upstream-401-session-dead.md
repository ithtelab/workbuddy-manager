# HTTP 401：先刷新，刷新失败才禁用（登录态自愈）

## 现场（2026-10-24）

面板里三个账号「积分余额一直是上游快照，刷新也不更新」。排查发现根因不在积分：
这三个号的 **access token 在上游已不被接受**——chat 与计费接口都返回 APISIX 的
HTML `401 Authorization Required`，`refreshToken` 刷新同样 401。

**这三个号最终都是靠重新扫码登录恢复的**（其中两个已重扫、一个待重扫）；
它们的会话被上游吊销，refresh 救不回来。这一点必须说清楚，避免把"重扫"误记成
"刷新自愈"。

## 为什么仍然要改 401 的处理

旧逻辑把这种 401 归进通用 `ErrClient`（「只换号不罚号」）：

- 死号留在池里被反复选中，每次 401 白换一次号；
- `/status` 仍显示健康（`healthy` 是假健康）；
- 面板刷新积分失败后静默回退到上游快照，用户只看到「刷新不动」。

所以 401 需要被识别为「该账号凭证不被接受」的终态。

## 但 401 不能一律禁用

`access token` 失效 ≠ 会话失效。OAuth 的常规情形是：**AT 过期/被作废，而
refreshToken 仍然有效**——这时正确动作是刷新一次，而不是把账号打进禁用。

因此本补丁分两级：

1. `Classify`：**非 JSON 信封**的 401（APISIX HTML 页 / 空体）→ `ErrSessionDead`；
   带 JSON `code` 的 401 保持既有语义（`12153` 仍由 `sessionDeadRule` 命中，
   其余按业务层各层判定，不因状态码一刀切）。
2. `applyErrorPolicy(ErrSessionDead)`：
   - `12153`（业务信封，会话过期）→ 直接禁用；
   - 401 → **先同步 `RefreshToken()` 试一次**：
     - 成功 → 保存新 token + 30s 短冷却，**不禁用**（下个请求用新 token）；
     - 失败 → 禁用，理由 `401 unauthorized (refresh also failed), re-login required`。

## 验证（实机，2026-10-24 21:00）

| 场景 | 构造方式 | 结果 |
| --- | --- | --- |
| AT 失效、RT 有效 | 取一个**正常登录中**的账号，把 auth 文件里的 `accessToken` 改成无效值（保留 RT） | 请求 503，账号**未禁用**、冷却 30s，**auth 文件里的 AT 被自动刷新**，随后该 token 直接请求上游返回业务响应（认证通过） |
| AT、RT 都失效 | 新建合成账号，`accessToken`/`refreshToken` 均为伪造值 | 命中 401 → 刷新失败 → `disabled=true`，理由 `401 unauthorized (refresh also failed), re-login required` |
| 回归 | 既有 `TestClassify` 用例 | JSON 401 仍归 `ErrClient`，未改判 |

三个真实账号（ccc / 拱辰 / 小章鱼）当时的 RT 都已失效，**不在上表第一行的自愈范围内**，
必须重新扫码——补丁只是让这类账号在被选中时快速禁用并给出准确理由。

## 测试

新增 `internal/upstream/session_dead_401_test.go`：HTML 401 / 空体 401 / 12153 /
JSON 401（保持 `ErrClient`）/ 403 HTML（仍 WAF）/ 400 HTML（仍 `ErrClient`）/ 200。
`go test ./internal/upstream/ ./internal/server/` 全过。