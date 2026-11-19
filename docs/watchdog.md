# 上游网页改版监控（watchdog）

> 状态：**已实现**。落地位置：`server/services/watchdog.py`（抓取 / 归一化 /
> 判读 / 邮件）、`server/routers/watchdog.py`（`/api/watchdog*`）、
> `server/main.py` 的 lifespan 调度器，以及设置页的「页面监控」
> （`/settings/watchdog`，面板在 `web/components/common/settings/WatchdogPanel.tsx`）。

腾讯改版时**接口常常照旧**，变的是页面结构、入口位置与活动文案。于是会出现两种
静默失效：新活动没人知道；我们依赖的 DOM / 路径被改掉，抓取看起来正常、其实抓空。
本模块盯的是**结构指纹**，不是内容比对。

## 在管理端配置（推荐）

1. 打开「设置 → 页面监控」（`/settings/watchdog`）。默认配置停用，不会抓任何页面。
2. 在「监控目标」里确认默认目标（`plans-usage`，类型「页面 + 前端资源」）。
   「测试 / 探测」按钮一律使用**表单当前值**（不必先保存）；保存仍由页面底部
   的「保存配置」完成。
3. 需要 LLM 判读时，在「LLM 判读」里填 OpenAI 兼容的接口地址、Key 与模型，
   然后「测试 LLM」。失败会自动退化为规则判读，不会漏报。
4. 邮件：填 SMTP 主机 / 端口 / 加密方式 / 账号与收件人，直接点
   「检测邮箱出口」。看到 `出口可用` 再打开「启用邮件通知」并保存，最后
   「发送测试邮件」确认收件箱真的收到了。出口不通时后端会在保存时强制关掉
   邮件开关，并在保存结果里带回原因。
5. QQ（可选）：在「QQ 通知（AstrBot）」里填 AstrBot 地址、开发者访问密钥与
   UMO，先用「检测连接」确认密钥有 `im` 权限，再「发送测试消息」。详见
   [`docs/qq-bot.md`](qq-bot.md)。
6. 点「立即检测」跑一轮：首轮只建立基线（`已建基线`，不告警），之后再变化才会
   出现 `有变化` 与判读结论。改版后误报可用「清空基线」重新开始，下一轮不会告警。

云厂商（AWS / 阿里云 / 腾讯云）常封 25 端口，请用 **465 + SSL** 或
**587 + STARTTLS**；端口探测区会同时列出 465 / 587 / 25 的结果，便于判断是
「出口被封」还是「账号密码不对」。

## 工作方式

```
抓目标页 → 归一化（结构行 + 可见文本行）→ 存指纹
      ├─ 指纹相同 → 结束
      └─ 指纹不同 → diff 摘要 + 关键词命中
                       ├─ LLM 判读（可选）→ {layout_changed, claimable, anomaly, ...}
                       ├─ 逐账号查「活动补偿」状态（只读）
                       ├─ auto_claim 且确有可领 → dailyplan 领一次（幂等）
                       └─ 按 notify_on 发通知：
                            邮件（先探 SMTP 出口）+ QQ（经 AstrBot OpenAPI）
```

归一化刻意**丢弃** `<script>` / `<style>` 内容与注释、`href/src` 的值：bundle
哈希与内联脚本每次发布都变，保留它们会让指纹天天抖。保留的是标签骨架
（`class` / `id` / `data-*` / `role` / `aria-*`）与可见文案——改版信号就在这两处。
`kind=js` 的目标只抽字符串字面量与 `/billing`、`/activity` 这类路径。

`kind=assets`（默认的 plans-usage 用它）会先抓页面骨架，再顺着 `<script src>` /
`<link href>` 抓最多 `asset_limit` 个**自有 CDN** 的前端构建产物，抽出其中的中文
字符串与接口路径一起进指纹。原因是 SPA 的活动文案与「领取」按钮并不在页面
HTML 里，而在 bundle 里；页面 HTML 只反映「有没有发布新版本」。第三方脚本
（如 `res.wx.qq.com`）排在最后，避免占掉名额。`asset_limit=0` 退化为只比骨架。

关键词规则是 LLM 的**退化路径**：没配 LLM、或 LLM 调用失败时，只要结构变了且
命中「领取 / 奖励 / 补偿 / claim / reward …」就告警，并标注 `source=rule`。

## 判定「有积分可领」用的是真实回执

`scan_compensation` 对每个账号调 `tencent.compensation_status`（**只读**），
按回执判断：

- `claimed` / `claimed_at` / `claimed_text` 任一成立 → `already`；
- 否则 `credit_num > 0` 或按钮文案含「领」→ `claimable`；
- 其余 → `unknown`（认不出结构，不冒充可领）。

关键词只能说明页面上有「领取」两个字，不能说明这个账号有得领——两者分开报告。

`auto_claim=true` 时，只有 `claimable > 0` 才调用 `dailyplan.run_task('compensation')`，
与手动「领补偿」同一条实现（幂等：有则领、无则空），不另写协议。

## 邮件出口预检（启用的硬条件）

多数云主机 / 容器 / 办公网封 25/465/587 出站。因此：

- `PUT /config` 里 `mail.enabled=true` 时，先探测**配置的端口**（DNS + TCP +
  等 SMTP 220 banner）；探不通就落库为 `enabled=false`，响应带 `warning` 与
  `mail_egress` 明细；
- 端点没变时不重复探测，沿用 `state.egress`；从没测过视为「未验证」；
- 每次发信前再探一次；探不通则跳过发信，并在 `state.egress` 留下原因——
  界面上要能区分「没有变化」与「有变化但邮件出不去」。

探测只做最小动作（TCP + banner）：再往下走会牵扯证书与凭据，把「出口被封」
和「密码错了」混成一个错误。`ports` 里同时给出 587/465/25 的诊断结果，
方便判断该换哪个端口。

POP3 / IMAP 不参与发信，因此没有接入：本功能只需要 SMTP 出站。

## QQ 通知（AstrBot）

管理端不直连 QQ 官方 API：AppID / AppSecret、access_token 刷新、IP 白名单与
主动消息限制都由 AstrBot 承担，这里只发一次 HTTP：

```
管理端 ──POST {base}/api/v1/im/message──▶ AstrBot ──▶ QQ
        Authorization: Bearer abk_...
        {"umo": "...", "message": [{"type": "plain", "text": "..."}]}
```

配置只有四项：`base_url`（自动补 `/api/v1`）、`api_key`（`abk_...`，需
`im` scope）、`umo`（`平台:消息类型:会话ID`）、`timeout_seconds`。

- **不做出口预检**：QQ 的出口在 AstrBot 那台机器上，探测管理端到 AstrBot 的
  TCP 只能说明「地址通」，说明不了「QQ 能发」。用「检测连接」+「发送测试消息」
  两步验证更准确：前者调 `/im/bots` 验鉴权与 scope，后者真发一条。
- **与邮件互不替代**：两个通道各自判断、各自发送；一个失败不影响另一个。
- 密钥读取时打码（`********`），保存时回传打码值表示「不改」；错误文本会把
  密钥替换成 `***`，不会随异常泄露。
- 群主动推送在 AstrBot WebSocket 模式下依赖**内存里的 msg_id 缓存**（重启后
  首次推送会被跳过），部署与 UMO 细节见 [`docs/qq-bot.md`](qq-bot.md)。

## 接口

所有接口都在 `/api/watchdog` 下，**全部要求管理员**（状态里有账号昵称、
收件地址与打码后的凭据）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/config` | 当前配置（密码 / API Key 打码） |
| PUT | `/config` | 保存配置；邮件启用会先探出口 |
| GET | `/status` | 配置 + 上次运行结果 + `due` / `next_run_at` |
| POST | `/run` | 立即检测一轮（同步返回差异与判读） |
| POST | `/baseline` | 清空快照，下次运行重建基线且不告警 |
| POST | `/mail/egress` | 只探 SMTP 出口，并把结果写进状态 |
| POST | `/mail/test` | 先探出口，再发一封测试邮件 |
| POST | `/qq/bots` | 检测 AstrBot 地址 / 密钥 / scope，列出平台适配器 |
| POST | `/qq/test` | 先验鉴权，再发一条 QQ 测试消息 |
| POST | `/llm/test` | 用一段固定的示例差异试一次 LLM，回判读结果 |

`/mail/*`、`/qq/*`、`/llm/test` 都接受**表单当前值**（请求体，可选、不落库），
用于「还没保存就想先测一下」。

写操作（改配置 / 立即运行 / 测试）都写审计日志。

## 配置示例

```json
{
  "enabled": true,
  "interval_minutes": 360,
  "targets": [
    {"name": "plans-usage", "url": "https://www.workbuddy.cn/profile/plans-usage",
     "kind": "assets", "enabled": true},
    {"name": "codebuddy-home", "url": "https://www.codebuddy.cn/",
     "kind": "html", "enabled": false}
  ],
  "timeout_seconds": 20,
  "asset_limit": 4,
  "notify_on": "change",
  "scan_compensation": true,
  "auto_claim": false,
  "allow_private_targets": false,
  "llm": {
    "enabled": true,
    "base_url": "https://wb.kuruomi.cloud/v1",
    "api_key": "（保存后只回 ********）",
    "model": "deepseek-v4.1-flash",
    "timeout_seconds": 60
  },
  "mail": {
    "enabled": true,
    "host": "smtp.example.com",
    "port": 465,
    "security": "ssl",
    "username": "notify@example.com",
    "password": "（保存后只回 ********）",
    "from": "notify@example.com",
    "to": ["you@example.com"],
    "subject_prefix": "[WorkBuddy 监控]"
  },
  "qq": {
    "enabled": false,
    "base_url": "http://127.0.0.1:6185",
    "api_key": "（保存后只回 ********）",
    "umo": "qq_official:GroupMessage:xxxxxxxx",
    "timeout_seconds": 15
  }
}
```

`notify_on`：

- `change`：只要指纹变了就发（最灵敏，改个文案也会收到）；
- `claimable`：LLM 或补偿扫描认为**有可领**才发；
- `anomaly`：LLM 认为值得处理、或有可领时才发（推荐配合 LLM 使用）。

## 安全边界

- 目标 URL 只允许 http/https；默认**拒绝解析到内网 / 回环 / 链路本地 / 保留地址**
  的目标（SSRF），确需监控内网再显式打开 `allow_private_targets`；
- 单目标响应上限 4 MB，超限即中止并记错误；
- 快照写在 `DATA_DIR/watchdog/`，只存归一化后的行，不存原始 HTML；
- 邮件密码 / LLM Key / AstrBot 密钥读取时打码，保存时回传 `********` 表示
  「不改」；HTTP 异常文本会先做密钥脱敏再返回；
- LLM 输入只包含**差异片段与关键词**，不整页回传；输出要求严格 JSON，
  解析失败按规则判读处理（`llm_error` 会带在结果里）。

## 已知边界

- 目标页需要登录时，抓到的是登录页；它的结构变化同样会告警，但无法据此判断
  活动内容——真正「有没有得领」仍以补偿状态回执为准。
- 首轮运行只建立基线、不告警，避免刚开启就收到一封"整页都是差异"的邮件。
- 一轮每个通道最多发一条：多个目标同时变化时，正文里已经列全；如需逐个目标
  发信，改 `run_once` 的发送循环即可。
- QQ 群主动推送受 QQ 平台与 AstrBot 状态影响（`msg_id` 缓存、主动发言开关、
  频率限制）。**通知没到不等于检测没跑**：`/status` 里的 `last_result.qq`
  会明确写出 `ok` / `skipped` / `error`。
