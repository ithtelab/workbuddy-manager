# 管理面作用域化 API Token（设计）

> 状态：**已实现（P0 + P1）**。落地位置：`server/tokensvc.py`（生成 / 解析 / 校验）、
> `server/routers/tokens.py`（CRUD）、`security.current_user` 的 Bearer 分支与
> `require_session_admin`（高危接口仅会话）、前端「设置 → 访问令牌」面板。
> 代理线路与全局出口代理（`/api/proxy-routes*`、`/api/settings/proxy*`）同样仅会话：
> 它们既决定流量从哪个出口出去，也是凭据载体。
> 本文档同时是该能力的**设计说明**与**安全约束清单**——改动前请先读「安全考量」
> 与「接口」两节。
>
> 目标是把「脚本 / CI 免交互调用管理面」做成一个**安全、最小权限、可吊销、
> 可审计**的一等能力，而不是退回「用户名密码 + 会话 Cookie」。

## 为什么需要

管理面 `/api/*` 目前的唯一凭据是**会话 Cookie**：`POST /api/login` 拿 `wb_session`，
浏览器带上它。对浏览器这没问题，但对**机器客户端**很别扭：

- 每次都要登录、维护 cookie jar、处理 401 重登；
- 密码要存在 CI 的 secret 里，且**共享**（谁用都是同一个管理员账号）；
- 会话有总时长 / 空闲超时，长时间运行的 cron / 守护进程要反复重登；
- 无法按用途分别授权、无法单独吊销「某一台机器上的那一份」。

网关 `/v1/*` 的密钥（`wbk_`）**不能**替代它：那套密钥只授权**模型调用**，
与后台权限无关（这是刻意的——见下）。

## 必须避免重蹈的覆辙

2026-09-14 的安全事件中，`users.json` 里曾经存在一个 `api_keys` 数组：
`security.current_user` 只要在请求头里看到命中该数组的 `X-API-Key`，就**直接授予
admin**。它没有任何代码写入、没有管理界面，唯一作用就是一条提权后门；而它在前面的
路径穿越漏洞里与 `secret` 一起泄露，直接导致生产站管理员被改密码。

**这份设计的每一条约束都是从那次事故倒推出来的**：新凭据必须
**存在数据库**（不随配置文件泄露）、**只存哈希**、**有明确 scope**、**可吊销**、
**全程审计**、**明文只在创建时返回一次**，且**管理凭据的接口只用会话鉴权**。

## 目标 / 非目标

**目标**

- 机器客户端可长期、免交互地调用 `/api/*`（只读或管理员）。
- 最小权限：至少两级 scope（只读 / 管理员）。
- 可吊销、可过期、可审计；泄露了能定位「是哪一份、用在哪儿」。
- 纯新增，不改变现有会话鉴权行为。

**非目标**

- 不替代网关密钥（数据面）——两者授权的东西不同，不要合并。
- 不做 OAuth / SSO / 多租户。
- 不做每 token 的调用量配额（后续可加）。

## 数据模型

```sql
CREATE TABLE IF NOT EXISTS api_tokens (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT    NOT NULL,            -- 用途备注，如 "ci-deploy"
  token_hash   TEXT    NOT NULL,            -- SHA-256(明文)
  prefix       TEXT    NOT NULL,            -- 明文前 12 字符，用于定位与展示
  scope        TEXT    NOT NULL,            -- 'readonly' | 'admin'
  enabled      INTEGER NOT NULL DEFAULT 1,
  expires_at   INTEGER,                     -- epoch 秒；NULL = 永不过期
  created_at   INTEGER NOT NULL,
  created_by   TEXT    NOT NULL DEFAULT '', -- 创建者用户名（审计用）
  last_used_at INTEGER,
  last_used_ip TEXT
);
CREATE INDEX IF NOT EXISTS idx_tokens_prefix ON api_tokens(prefix);
```

- 明文形如 `wbt_<43 字符 base64url>`。前缀 `wbt_` 与网关密钥 `wbk_` **刻意区分**，
  避免两类凭据在同一处被混用。
- 定位方式与网关密钥一致：**先按 `prefix` 取出候选行，再对哈希做常量时间比较**
  （`secrets.compare_digest`），不全表扫描、不用 `==` 比哈希。
- **token 不绑定用户**。鉴权时角色由 `scope` 决定（`readonly` → viewer，
  `admin` → admin），而不是回查某个用户的角色——否则「删用户 / 降权」会连带
  影响 token，语义混乱；`created_by` 只用于审计。

## 鉴权解析

`security.current_user` 增加一条分支（保持 Cookie 路径行为完全不变）：

1. 有会话 Cookie → 现有逻辑，返回。
2. 否则看 `Authorization: Bearer wbt_...`（也接受 `X-API-Key`，仅作兼容别名，
   值必须带 `wbt_` 前缀）：
   - `resolve(token)`：按 `prefix` 定位 + `compare_digest` 校验哈希；
   - 命中后检查 `enabled` 与 `expires_at`，任一不过 → 401（**不泄露**是「不存在」
     还是「已过期」，只记审计）；
   - 通过 → 返回 `{'username': f'token:{name}', 'role': scope_to_role(scope)}`。
3. 都没有 → 401。

**`last_used_at` / `last_used_ip` 写库要节流**：距上次记录超过 60 秒才更新，
否则每个请求一次写会把库打热（管理面在总览页会被 30 秒轮询）。

**失败计入登录锁定**：token 校验失败也走既有的按 IP 失败计数，防止拿它当爆破面。
成功则清除该 IP 的失败计数。

## 接口

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| `POST` | `/api/tokens` | **会话（admin）** | 创建，**明文仅此一次返回** |
| `GET` | `/api/tokens` | 会话 | 列表（**不含明文**，含前缀、scope、最后使用） |
| `PATCH` | `/api/tokens/{id}` | **会话（admin）** | 改名 / 启停 / 改 scope / 改过期 |
| `DELETE` | `/api/tokens/{id}` | **会话（admin）** | 删除 |

**关键约束：token 管理接口只接受会话鉴权，不接受 token 鉴权。**
也就是说，一个泄露的 token **不能**用它去创建 / 吊销 / 提权另一个 token——
否则泄露即等于「自助持久化 + 提权」。这是本设计里最重要的一条边界。

请求 / 响应示例：

```jsonc
// POST /api/tokens
{ "name": "ci-deploy", "scope": "readonly", "expires_at": 1767225600 }

// 201
{ "id": 3, "name": "ci-deploy", "prefix": "wbt_a1b2c3d4e5",
  "scope": "readonly", "enabled": true, "expires_at": 1767225600,
  "token": "wbt_xxxxx…",        // 仅此一次
  "created_at": 1764000000 }
```

`PATCH` 语义与 `/api/keys/{id}` 一致：只提交要改的字段（`exclude_unset`）。

## 安全考量

- **只存哈希**，明文只在 `POST` 响应里出现一次；列表接口永不回传。
- **常量时间比较**；定位靠前缀，避免时序侧信道与全表扫描。
- **scope 最小化**：`readonly` 对任何写接口一律 403，且在路由层用同一个
  `require_admin` 依赖统一拦截（不新增第二套判据，避免漂移）。
- **审计**：创建（含 scope / 过期）、吊销、删除、以及**失败的使用**都落
  `audit_logs`；成功的使用靠 `last_used_at/ip` 暴露，不逐条写审计（噪声太大）。
- **绝不出现在日志里**：与现有密钥一样，任何日志 / 错误体都不回显明文或哈希。
- **与 `users.json` 完全无关**：不新增任何「文件里的凭据数组」式的判据。
- **过期与吊销即时生效**：鉴权每次校验（不做长 TTL 缓存）；若将来加缓存，
  吊销/停用必须主动失效。
- 高危接口（如一键更新 `/api/system/update`）建议**额外要求会话**，
  即便调用者持有 `admin` scope 的 token——让「能改代码的入口」只对真人开放。
- 同理，**自动更新开关** `PUT /api/settings/auto-update` 也只对会话开放：打开它
  等于授权面板在无人值守时自行下载安装新版本并重启服务，与一键更新同一风险级别。

## 与前端 / SDK 的衔接

- 前端「设置 → 访问令牌」：创建（弹一次明文并可复制）、列表（名称 / 前缀 /
  scope / 最后使用时间与 IP）、停用 / 删除；创建时按 scope 给出用途说明。
- Go SDK 已提供 `WithAdminToken("wbt_...")`（本设计落地后即可直接使用），
  走 `Authorization: Bearer`，优先级高于 Cookie，且不参与 401 重登。
- 用 QQ 机器人控制管理面时，建议单独建一个 `readonly` token（如
  `qq-bot-readonly`）；可调用的只读端点、写操作的人工确认与高危接口边界见
  [docs/qq-bot.md](qq-bot.md) 的「开放接口操作」一节。

## 兼容与迁移

- 纯新增：不启用 token 时，鉴权路径与现在**逐字节相同**。
- 老版本前端 / SDK 不受影响；旧库启动时自动建表（走既有 `_MIGRATIONS` 机制）。

## 测试计划

- 哈希 / 前缀定位；`compare_digest` 路径；不存在的 token 与错哈希都返回 401。
- **scope 越权**：`readonly` 调任意写接口 → 403（逐路由抽查）。
- **过期 / 停用 / 删除后立即失效**（三种路径都要有用例）。
- **token 不能管理 token**：用 token 鉴权调 `/api/tokens*` → 401/403。
- 审计落库；`last_used` 节流（60 秒内多次请求只写一次）。
- 失败计入按 IP 锁定，成功清除计数。
- 明文不出现在 `GET /api/tokens` 与任何日志里。

## 分期

- **P0**：表 + 鉴权解析 + 创建 / 列表 / 删除 + 审计 + 上述测试。
- **P1**：`PATCH`（启停 / 过期 / scope）、前端页面。
- **P2**：按 token 的调用量统计（可挂到 `/api/stats` 维度里）。

## 未决问题

1. scope 是否需要第三档（如「只读调用日志」），还是保持两级就够？
2. `admin` scope 的 token 是否应**禁止**调用高危及不可逆接口（更新、删账号、
   清日志、改安全策略）？我倾向禁止，让这些只对会话开放。
3. `last_used` 节流窗口取 60s 是否合适（与 `credits` 的 60 秒缓存同口径）。
