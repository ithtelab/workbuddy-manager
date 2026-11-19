# QQ 机器人通知与开放接口（AstrBot）

> 状态：**通知通道已实现**（`server/services/qq.py` + `watchdog` 配置项 `qq` +
> 设置页「页面监控 → QQ 通知」）。**AstrBot 本身尚未部署**——本文同时是
> 部署评估与开放接口操作说明，按需再落地。
>
> 结论先说：**可行**。管理端不需要公网回调、不需要持有 QQ 的 AppID/Secret，
> 只需要一个能访问到的 AstrBot 地址、一把开发者访问密钥（`abk_...`）和一个
> UMO。控制方向复用管理面已有的 `wbt_` 作用域 Token，不新造凭据体系。

## 1. 整体架构

```
                    ┌────────────────────────────┐
  页面改版 / 可领 ──▶│  workbuddy-manager（aws2） │
                    │  watchdog.run_once()       │
                    └───────┬────────────┬───────┘
                            │            │
                 SMTP 直发  │            │  POST {base}/api/v1/im/message
                            ▼            ▼  Authorization: Bearer abk_...
                         邮箱        ┌──────────┐
                                     │ AstrBot  │──WebSocket──▶ QQ 开放平台
                                     │ :6185    │◀──事件──────  QQ 群 / 私聊
                                     └──────────┘
```

两条通知通道互相独立：邮件发失败不影响 QQ，QQ 发失败也不影响状态落库与界面。

## 2. AstrBot 侧部署评估

### 2.1 版本与能力

- HTTP OpenAPI 从 **4.18** 起提供；建议直接用 **4.26+**（4.26 把后端迁到
  FastAPI，并补了 Webhook 模式下「无 `msg_id` 也能群主动推送」）。
- 鉴权：`Authorization: Bearer abk_...`（也接受 `X-API-Key`）。
- 密钥在 WebUI「设置 → OpenAPI」创建，scope 里必须有 **`im`**；否则返回
  `403 Insufficient API key scope`。
- 自助核对：`GET /api/v1/im/bots` 能列出平台适配器 id；`GET /api/v1/openapi.json`
  是当前实例的真实协议（版本差异以它为准）。

### 2.2 部署方式（评估，不执行）

Docker 是最省事的方式，关键是**端口不要直接暴露公网**：

```yaml
services:
  astrbot:
    image: soulter/astrbot:latest
    restart: unless-stopped
    ports:
      - "127.0.0.1:6185:6185"   # 只绑本机；管理端同机时用 127.0.0.1
    volumes:
      - ./data:/AstrBot/data
```

- 管理端与 AstrBot **同机**：`base_url` 填 `http://127.0.0.1:6185`，流量不出网。
- **跨机**：只走内网/隧道，别把 6185 开到公网；确需公网时必须加反代 + TLS +
  强口令，并限制来源 IP。
- AstrBot 面板口令、OpenAPI 密钥都按「等于 QQ 机器人控制权」对待。

### 2.3 QQ 官方机器人的硬限制

这些限制在 QQ 开放平台侧，AstrBot 改不了：

| 限制 | 影响 |
|---|---|
| 需要 AppID / AppSecret | 在 QQ 开放平台创建机器人后填入 AstrBot |
| **IP 白名单** | 填 AstrBot 的**出口公网 IP**；NAT 出口漂移会失效 |
| 只能加入**自己是群主**的群 | 不能进别人的群做通知 |
| 沙箱测试对象 ≤ 20 人 | 正式使用需要过审 / 上线 |
| 「允许机器人主动在群聊内发言」 | 群主动推送必须打开 |
| 「获取群内全部消息」 | 需要接收全量群消息时打开 |
| 被动回复窗口（约 5 分钟 / 5 条） | 只影响「收到消息后再回」，不影响主动推送 |
| 主动推送有频率/权限限制 | 失败码以 QQ 平台回执为准（多为权限或频率类） |

**WebSocket 模式的一个坑**：群主动推送依赖 AstrBot **内存里缓存的 `msg_id`**。
进程重启后缓存清空，在下一次收到该群消息之前，群推送会被跳过（日志类似
`No cached msg_id for session ... skip send_by_session`）。私聊（C2C）主动推送
不需要 `msg_id`。若不能接受这个不确定性，用 **Webhook 模式 + 4.26+**。

### 2.4 UMO：消息发给谁

UMO（`unified_msg_origin`）格式：

```
平台适配器id:消息类型:会话ID
```

QQ 官方机器人常见取值：

| 场景 | UMO |
|---|---|
| 群聊 | `qq_official:GroupMessage:<group_openid>` |
| 私聊 | `qq_official:FriendMessage:<openid>` |

- `group_openid` **只能从群消息事件里拿**：先在目标群 @ 一次机器人，再在
  AstrBot 日志 / 会话信息里取；管理端「检测连接」能列出适配器 id，帮助拼前缀。
- 适配器 id 不一定是 `qq_official`（取决于 AstrBot 里的实例名），以
  `/api/v1/im/bots` 的返回为准。
- 若 AstrBot 开了 `unique_session`，会话 ID 会变成
  `sender_openid_group_openid` 之类的复合值；升级到含相关修复的版本后，
  **已保存的 UMO 需要重新取一次**。

## 3. 管理端配置（已实现）

设置页：`/settings/watchdog` → 「QQ 通知（AstrBot）」。

| 字段 | 说明 |
|---|---|
| `enabled` | 只控制**定时任务**是否发 QQ；测试按钮不受它限制 |
| `base_url` | 不带路径自动补 `/api/v1`；也可直接填 `https://host:6185/api/v1` |
| `api_key` | `abk_...`，需要 `im` scope；读取时打码为 `********` |
| `umo` | `平台:消息类型:会话ID` |
| `timeout_seconds` | 3–120 秒 |

验证顺序（推荐）：

1. 点 **检测连接** → 调 `POST /api/watchdog/qq/bots` → 内部请求
   `GET {base}/api/v1/im/bots`。失败通常是地址不通、密钥错、缺 `im` scope。
2. 点 **发送测试消息** → 先验鉴权，再 `POST /im/message`。失败但鉴权通过，
   问题基本在 UMO 或 QQ 侧权限（群主 / 主动发言 / 白名单）。
3. 打开「启用 QQ 通知」并保存。

两个测试按钮都用**表单当前值**，不要求先保存；请求体不落库。

发送逻辑在 `server/services/watchdog.py::send_qq`，HTTP 细节在
`server/services/qq.py`：

- 文本上限 1800 字符，超出截断并加「…（已截断）」；
- 失败返回 `{ok: false, error}`，不抛异常（一轮通知不该因单通道失败中断）；
- 所有错误文本会把 `api_key` 替换成 `***`。

## 4. 开放接口操作（类似管理员密钥）

### 4.1 三种凭据别混

| 前缀 | 归属 | 授权范围 | 用途 |
|---|---|---|---|
| `wbk_` | 本管理端网关 | 模型调用（数据面） | 客户端 / SDK 调 `/v1/*` |
| `wbt_` | 本管理端管理面 | `readonly` / `admin` | 脚本、CI、机器人调 `/api/*` |
| `abk_` | AstrBot | OpenAPI scope（含 `im`） | 管理端 → AstrBot 发消息 |

机器人控制管理端要用的是 **`wbt_`**；`abk_` 只用于 AstrBot。两者不能互换。

### 4.2 创建与管理 Token

管理面 Token 的完整设计见 [`docs/api-tokens.md`](api-tokens.md)，要点：

- 明文只在创建响应里出现**一次**；库中只存 SHA-256 与前缀；
- `readonly` → viewer（写接口一律 403）；`admin` → admin；
- **Token 管理接口只接受会话鉴权**：泄露的 token 不能创建 / 吊销 / 提权另一个
  token；
- 可设过期时间、可停用、可删除；`last_used_at/ip` 可追溯。

```bash
# 创建（只读，给 QQ 机器人）
curl -sS -X POST https://wb.kuruomi.cloud/api/tokens \
  -H 'Content-Type: application/json' \
  -b 'wb_session=...' \
  -d '{"name":"qq-bot-readonly","scope":"readonly"}'
# → {"id":3,"prefix":"wbt_a1b2c3d4e5","scope":"readonly","token":"wbt_...","..."}

# 列表（不含明文）
curl -sS https://wb.kuruomi.cloud/api/tokens -b 'wb_session=...'
```

### 4.3 机器人可调用的接口（建议白名单）

只读（`readonly` 足够）：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/stats` | 用量 / 余额概览 |
| GET | `/api/keys` | 密钥与额度（不含明文） |
| GET | `/api/logs` | 最近调用与错误 |
| GET | `/api/watchdog/status` | 上次检测、判读、通知结果 |
| GET | `/api/accounts` | 账号池状态（按需） |

写操作（`admin`，建议**人工确认**后再调）：

| 方法 | 路径 | 风险 |
|---|---|---|
| POST | `/api/watchdog/run` | 会抓上游页面，可能触发通知 |
| POST | `/api/watchdog/baseline` | 清基线，下一轮不告警 |
| POST | `/api/watchdog/qq/test` | 真发一条 QQ 消息 |

**只允许会话**（任何 token 都调不了，这是刻意设计）：

- `/api/tokens*`（防提权）
- `/api/system/update`（能改代码的入口）
- `/api/settings/proxy*`、`/api/proxy-routes*`（流量出口与凭据载体）
- 账号删除 / 改密、清日志、安全策略、数据库导入导出等不可逆操作

### 4.4 调用示例

```bash
# 只读：查监控状态
curl -sS https://wb.kuruomi.cloud/api/watchdog/status \
  -H "Authorization: Bearer $WBT_TOKEN"
```

```python
# AstrBot 插件里调管理端（只读）
import os

import httpx

WBT_TOKEN = os.environ["WB_ADMIN_TOKEN"]  # 建议放环境变量，不要写进插件源码


async def fetch_watchdog_status() -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            "https://wb.kuruomi.cloud/api/watchdog/status",
            headers={"Authorization": f"Bearer {WBT_TOKEN}"},
        )
        r.raise_for_status()
        return r.json()
```

```python
# AstrBot 插件注册一个最小 Web API（供人工触发只读查询）
from astrbot.api import star
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain


class ManagerBridge(star.Star):
    def __init__(self, context, config=None):
        super().__init__(context, config)
        context.register_web_api(
            "/manager/status", self.handle_status, ["POST"], "查询管理端监控状态"
        )

    async def handle_status(self, **kwargs):
        data = await fetch_watchdog_status()
        return {"ok": True, "data": data}
```

```python
# 主动从 AstrBot 发消息（管理端同款协议，用于反向验证）
await context.send_message(
    "qq_official:GroupMessage:<group_openid>",
    MessageChain([Plain("测试消息")]),
)
```

### 4.5 审计、限流与失败处理

- 管理端所有写操作写 `audit_logs`；token 的使用靠 `last_used_at/ip` 追溯，
  失败鉴权计入按 IP 的登录锁定。
- token 校验失败统一 401，不区分「不存在 / 已过期 / 已停用」；scope 不足 403。
- 机器人侧应实现：401 → 停用该密钥并告警；403 → 说明用了只读 token 调写接口；
  429/5xx → 退避重试，别在 QQ 里刷屏。
- 建议给机器人单独建 token、单独命名（如 `qq-bot-readonly`），泄露时只吊销它。

## 5. 安全边界（必须遵守）

1. **QQ 消息是不可信输入**：不要把消息文本拼进 shell / SQL / URL；控制命令只
   匹配固定动词，并且只接受白名单 `openid`。
2. **写操作要二次确认**：机器人在群里收到「运行检测」后，先回「确认执行？」，
   收到确认再调 `admin` token；高危接口干脆不做。
3. **AstrBot 面板不暴露公网**；OpenAPI 密钥按密码管理，定期轮换。
4. **日志脱敏**：管理端不打印 `wbt_` / `abk_` 明文；AstrBot 侧也要确认日志级别。
5. **群主动推送的合规性**：只在自己当群主的群里发；控制频率，避免触发平台限制。

## 6. 尚未落地 / 需要决策

- AstrBot 部署位置：aws2（与管理端同机，`127.0.0.1` 最省事）还是 tencent；
- QQ 机器人 AppID / AppSecret、目标群与管理员 openid 白名单；
- WebSocket 还是 Webhook（后者群主动推送更可靠，但需要公网回调）；
- 机器人控制做到哪一档：只读查询 / 允许手动跑一轮 / 允许改配置（建议前两档）。

## 7. 相关文件

- `server/services/qq.py`：AstrBot OpenAPI 客户端（发送 / 列机器人 / 脱敏）
- `server/services/watchdog.py`：配置、`send_qq`、`test_qq`、`run_once`
- `server/routers/watchdog.py`：`/api/watchdog/qq/bots`、`/qq/test`
- `web/components/common/settings/WatchdogPanel.tsx`：QQ 通知卡片
- `server/tests/test_watchdog.py`：`QqServiceTest` / `QqConfigTest` /
  `QqRouterOverrideTest` / `QqNotifyTest`
- `docs/api-tokens.md`：管理面 Token 的完整设计与安全约束
