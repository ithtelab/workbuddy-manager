# 提案：密钥创建后一键导入 cc-switch / ZCode（供上游作者评审）

日期：2026-09-24。状态：**路径 B（导出片段）与路径 A（本机写入）均已实现并合入**；
仅 §5「客户端侧提供导入接口」仍需 cc-switch / ZCode 作者支持。
提出者：本地部署用户（面板 v1.0.69 + 上游 workbuddy2api 47d0c39）。

## 1. 需求

在面板「密钥」页新建密钥成功后，允许把这把密钥**一键导入**到本机的客户端工具：

- **cc-switch**（Claude Code / Codex 多供应商切换器，SQLite 存储）
- **ZCode**（`~/.zcode/v2/provider_config.json`，JSON 存储）

目标体验：**真一键**——点一下按钮，客户端里立刻多出一个可直接用的供应商配置，用户无需手动复制密钥、填写 baseUrl、挑选模型。

## 2. 决定性约束（决定了功能只能落在"创建时"）

面板密钥**只存哈希**：

- `server/keysvc.py:211` — `token = TOKEN_PREFIX + secrets.token_urlsafe(32)`
- 同文件 `:230` — `out['key'] = token  # 仅此一次返回明文`
- 数据库 `api_keys` 表只有 `key_hash` 与 `prefix`（`server/db.py:76-99`）
- 前端 `web/app/(main)/keys/page.tsx:710` —「一次性展示新密钥」弹窗，之后无法再取回

**因此：密钥列表的操作列（按已有行的 `prefix`）无法生成有效配置**——前缀拼不出完整密钥。一键导入必须发生在**创建成功、明文仍可用的那一刻**。

> 这是安全设计的正确结果，本提案不建议为了该功能改动存储方式。若维护者希望支持存量密钥导入，可另开「重置并导入」（重新签发新密钥、旧密钥立即失效）作为独立特性，但那会改变用户手中已分发的密钥，需单独讨论。
>
> **2026-11-02 补充**：面板后来还是改了存储方式 —— 密钥页加了「查看密钥」，
> 明文与哈希一起入库（理由与代价写在 `server/db.py` 的 `api_keys.key_plain`
> 注释里）。于是**下面这段「只能用一次」的前提已不成立**：任意一把密钥都能
> 在列表里再取回明文，导出 / 导入本可以基于 `key_id` 而不是创建那一刻的明文。
> 本提案的接口形态（`/api/keys/export` 收明文）暂时保持原样，改动它属于另一件事。
> 唯一仍未覆盖的是**本列之前建的存量密钥**（库里只有哈希，取不回来）。

## 3. 落点（UI）

`web/app/(main)/keys/page.tsx` 的「一次性展示新密钥」弹窗（`:710` 起），在现有 `CopyButton`（`:722`）旁新增两个按钮：

```
┌─ 新密钥已创建（仅此一次展示）─────────────┐
│  wbk_xxxxxxxxxxxxxxxxxxxxxxxxxxxx        │  ← 现有：明文 + 复制
│  [复制]  [导入 cc-switch ▾]  [导入 ZCode ▾] │  ← 新增
└──────────────────────────────────────────┘
```

下拉/二次确认里可选**导入到哪个客户端身份**（Claude / Codex；ZCode 通常只有一个），避免误写。

## 4. 两种实现路径（建议同时提供）

因为"真一键"要求面板能写用户本机文件，故必须区分部署形态：

### 路径 A：本机可写（本地面板，真一键）

面板与客户端同机时，后端直接落盘/入库。

**A-1 cc-switch**（`~/.cc-switch/cc-switch.db`，SQLite）

`providers` 表字段（已实测）：

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | 与 `app_type` 组成复合主键（UUID 或固定名如 `claude-official`） |
| `app_type` | TEXT PK | `claude` / `codex` / `gemini` … |
| `name` | TEXT | 显示名 |
| `settings_config` | TEXT | **JSON 字符串**，格式见下 |
| `website_url` / `category` / `icon` / `icon_color` | TEXT | 显示元数据（`category` 可用 `custom`） |
| `created_at` / `sort_index` | INTEGER | 排序 |
| `is_current` | BOOLEAN | 置 1 表示立即启用（**需把同 app_type 的其它行置 0**） |
| `cost_multiplier` / `limit_daily_usd` / `limit_monthly_usd` / `provider_type` | TEXT | 可选 |

`settings_config` 实测样例：

```jsonc
// app_type = claude
{"env": {
  "ANTHROPIC_AUTH_TOKEN": "<面板密钥>",
  "ANTHROPIC_BASE_URL": "http://<面板地址>",     // 根地址：不带 /v1，见下方"要点"
  "ANTHROPIC_MODEL": "<默认模型>",
  "ANTHROPIC_DEFAULT_HAIKU_MODEL": "<模型>",
  "ANTHROPIC_DEFAULT_SONNET_MODEL": "<模型>",
  "ANTHROPIC_DEFAULT_OPUS_MODEL": "<模型>"
}}

// app_type = codex
{"auth": {"OPENAI_API_KEY": "<面板密钥>"},
 "config": "model_provider = \"custom\"\nmodel = \"<默认模型>\"\n[model_providers]\n[model_providers.custom]\nname = \"custom\"\nwire_api = \"responses\"\nbase_url = \"http://<面板地址>/v1\"\n"}
```

要点：
- **两个客户端的 base url 口径不同，不能共用一个值**：

  | 客户端 / app | 需要的值 | 原因 |
  |---|---|---|
  | cc-switch / claude | `http://<面板地址>`（根） | Claude Code 走 Anthropic SDK，SDK 自己拼 `/v1/messages` |
  | cc-switch / codex | `http://<面板地址>/v1` | Codex 在 base url 后拼 `/responses` |
  | ZCode（openai-responses） | `http://<面板地址>/v1` | 同上 |

  给 claude 填成 `…/v1` 是实测踩过的坑：实际请求变成 `…/v1/v1/messages`，
  而面板上**那个路径存在但不是 POST 路由**，于是返回 **405**（不是 404）——
  报错看起来像"方法不对"，很容易让人往错误方向排查。本机实测已确认。
  另：实测的 cc-switch 配置里，claude provider 用的就是**根地址**（不写成 `/v1`）。
- **写入前必须关闭/退出 cc-switch**，或在其运行时通过它的接口写入——直接改 SQLite 有被应用内存态覆盖的风险（与下文 ZCode 同类问题）。
- `provider_endpoints` 表另存 URL 列表（`provider_id` + `app_type` + `url`），建议同步写入，保持 UI 中「端点」一致。
- 建议 `id` 用稳定 UUID 存到面板侧（例如 `settings` 表记 `ccswitch_provider_id`），重复导入时**更新而非追加**，避免堆重复项。

**A-2 ZCode**（`~/.zcode/v2/provider_config.json`）

结构（已实测，本机已有同类配置）：

```jsonc
{
  "schemaVersion": 1,
  "config": {
    "providerOrder": ["<providerId>", ...],
    "providerConfigRules": {"providerRules": [{
      "providerId": "<uuid 或稳定串>",
      "providerName": "<供应商名>",
      "config": {
        "group": "standard-personal",
        "access": {"type": "api-key", "apiKey": "<面板密钥>"},
        "api": {"type": "openai-responses", "baseUrl": "http://<面板地址>/v1"},
        "personalModelIds": ["<模型 id>", ...],
        "modelOrder": ["<模型 id>", ...]
      }
    }]},
    "modelConfigRules": {"providerModelRules": [
      {"modelId": "<id>", "providerId": "<同上>", "config": {"properties": {"contextWindow": 1000000}}}
    ]}
  }
}
```

要点：
- **ZCode 运行时会重写该文件**（用户改配置即落盘），因此程序化写入存在竞态；理想做法是让 ZCode 提供导入入口（见 §5）。
- `personalModelIds` 与 `modelOrder` 应一致，并按密钥的**模型白名单**裁剪（见 §6）。
- `contextWindow` 取自网关 `/v1/models` 的 `context_length`（本机实测 44 个模型全部带该字段）。

### 路径 B：远程面板（降级，非真一键）

面板部署在服务器时无法写用户本机。退化为：

- 后端返回**配置片段**（`GET /api/keys/{id}/export?client=ccswitch|zcode`），前端提供「下载 .json / 复制」；
- 或生成 **cc-switch / ZCode 的导入深链**（若客户端支持）

用户体验为"一键生成配置 + 手动导入一次"，仍比手工填表好，但不满足"真一键"。

## 5. 上游/客户端侧的建议（供作者判断贡献方向）

要让"真一键"对所有用户都成立，最干净的是**客户端提供导入能力**，面板只负责生成标准配置：

1. **ZCode**：希望提供 `zcode://import?payload=<base64(json)>` 或 CLI（`zcode provider add --file -`）——面板按钮直接触发即可，避免抢写配置文件。
2. ~~**cc-switch**：希望提供 CLI/URL scheme（如 `ccswitch://import?...`）~~ ✅ **已有**：
   cc-switch 已经注册了 `ccswitch://v1/import`（V1 协议，见 §10）。本项目已改为
   **深链优先**，不需要再向作者提这条。
3. **面板侧**（本项目可自主实现，与作者无关）：新增导出端点 + 落点按钮。

> 若客户端暂无导入能力，建议**先合入路径 B（导出片段）+ 路径 A（本机写入，带"客户端未运行"检测）**，并在文档中标注适用条件。

## 6. 配置内容如何生成（两客户端通用）

| 字段 | 取值 |
|---|---|
| 密钥 | 创建时返回的明文（仅此一次） |
| baseUrl | 面板对外地址 + `/v1`；**必须来自面板自身配置**（`WB_PUBLIC_BASE_URL` 或请求 HOST），不能硬编码 `127.0.0.1` |
| 默认模型 | 用户在下拉里选（候选来自 `/v1/models`，按密钥 `models` 白名单过滤）；未选则用网关 default |
| 模型列表 | 密钥 `models` 白名单非空时取交集；为空（全部模型）时取网关全量（本机 44 个） |
| 版本（realm） | 密钥的 `realm`（`cn`/`global`）决定前缀（`cn:` / `global:`），与网关 `/v1/models` 的 id 一致 |
| 供应商名 | 默认「WorkBuddy <密钥名>」，便于多密钥区分 |

**重要**：本机实测网关 44 个模型的 id 带 `cn:` 前缀，而面板模型目录（`/api/model-catalog`）返回的是**裸名**——生成配置时必须用**网关口径的 id**（带前缀），否则客户端调用会 404。这是最容易踩的坑，建议在实现里加断言。

## 7. 风险与边界

| 风险 | 处理建议 |
|---|---|
| 进程探测失败（非 Windows/POSIX 环境、权限受限） | **视同"不能写"并拒绝**，并提示改用导出。两种误判的代价不对称：漏判会写坏用户配置，误判只是让用户多点一次 |
| 客户端运行时写文件/库 | **能走深链就走深链**（§10，客户端自己入库，不存在覆盖问题）；只能直写时：未获授权**一律拒绝**，获授权则先关客户端、写完再拉起来（§8.4） |
| 客户端运行时抢写（ZCode 明确存在） | 优先走客户端导入接口；ZCode 无公开接口，只能关掉→写入→拉起；关闭失败要如实拒绝，不要盲写 |
| 自动关闭客户端会丢用户未保存的东西 | 先**优雅关闭**（`taskkill` 不带 `/F`）给足 12 秒；超时才升级强杀。关闭与重启都写进响应的 `lifecycle` 并显示在界面上（"已自动重新打开"/"没能重新打开"），不静默 |
| 关闭后再也拉不起来（单实例互斥 / 启动耗时） | 写入与拉起解耦：`finally` 里必定尝试重启，并轮询确认进程真的起来了；失败时如实上报 `restarted=false`，界面提示手动打开——**用户的配置已经写好了**，只是窗口没自己回来 |
| 深链投递成功但客户端没弹确认框 | 无法从外部确认。响应里返回 `method='deeplink'` + `action='handed-off'`，界面文案表述为"已交给 <客户端> 处理，请在它的窗口里确认"，不谎称"已导入成功" |
| 明文密钥落盘 | 面板侧不保存明文；客户端侧本就是明文存储（cc-switch/ZCode 现状如此），属既有事实；导入动作需**记入审计日志**（`audit_logs` 表已存在） |
| 重复导入产生重复供应商 | 以「密钥 id / 前缀 + 客户端」为键做 upsert；cc-switch 的 provider id 由 uuid5(前缀+app) 推导（不额外存映射），ZCode 用 `workbuddy-<前缀>` |
| 重复导入产生重复**模型规则** | 我方规则**整体替换**为本次清单，而不是逐条 upsert——逐条 upsert 对已存在的 modelId 会写成两条相同规则（实测踩过，见 §8.1） |
| 面板在服务器（远程部署） | 走路径 B；UI 上明确区分「导入到本机」与「导出配置」两种动作。后端另以**来源必须是回环地址**兜底，避免"以为写到了本机" |
| 密钥含 IP 白名单 | 导入后从本机调用会受白名单限制；若白名单不含本机 IP，应在导入前校验并警告 |
| cc-switch 的 `endpointAutoSelect` / `commonConfigEnabled` 语义未公开 | 确认前**不写这两个开关**：猜错会让流量被静默改到别的地址（见 §8.1 末段） |

## 8. 本地验证结论（已完成，未改任何客户端数据）

- ✅ cc-switch 为 SQLite（`~/.cc-switch/cc-switch.db`，16 张表），`providers.settings_config` 结构已实测（claude 用 `env.ANTHROPIC_*`；codex 用 `auth.OPENAI_API_KEY` + `config` TOML 文本）
- ✅ ZCode 为 JSON（`~/.zcode/v2/provider_config.json`），结构与写入方式已实测（本机已成功写入 44 个模型）
- ✅ 网关 `/v1/models` 返回 44 个模型，字段含 `context_length` / `supports_reasoning` / `supports_images`，可直接用于生成配置
- ⚠️ 两客户端均为"运行时重写自身配置"型应用，程序化写入需处理竞态（§7）
- ⚠️ 本机 cc-switch 与 ZCode 均处于运行状态，本次**未执行任何写入**，仅读取结构

### 8.1 实现后按真实数据复测（2026-09-24，路径 A 合入后）

做法：把本机真实的 `cc-switch.db`（6 条 provider）与 `provider_config.json`
（3 个供应商 / 61 条模型规则）**拷贝到临时目录**后写入，原文件全程只读并以 sha256
校验未被改动。结论：

- ✅ 首次写入 created、重复写入 updated，`providers` 行数与 key 前缀一一对应（无重复项）
- ✅ `set_current` 只在**同 app_type** 内翻转，gemini 等其它 app_type 不受影响
- ✅ 既有端点的行未被破坏，新端点行按 (provider_id, app_type) 覆盖写
- ✅ ZCode 合并后：其它供应商与其 61 条模型规则原样保留、`providerOrder` 未重复追加；
  44 个模型缩到 3 个后我方旧规则被正确清除
- ✅ 用 `*.wbimport-*.bak` 回滚后，副本与原始**逐字节一致**

复测中修掉两个真实缺陷（都已加回归测试）：

1. **claude 的 base url 带 `/v1`**（原导出实现照抄了 OpenAI 口径）→ 实际请求打到
   `/v1/v1/messages`，返回 **405**（见 §4 A-1 要点）。
2. **重复导入时模型规则重复**：重叠的 `modelId` 被"保留旧的 + 又追加新的"写成两条
   相同规则（44 → 3 的复测里总数是 65 而非 64）。现改为我方规则**整体替换**。

另有一个**未验证因而刻意不使用**的机制，供 cc-switch 作者确认（见 §7 末条）：
`providers.meta.endpointAutoSelect` 打开后，`provider_endpoints` 里的 URL 是否会
替换 `settings_config` 中的 base url。实测本机 `provider_health` 为空、日志无端点
探测记录，无法判定；猜错的后果是用户流量被静默改到另一个地址上，因此本实现
**不写这个开关**（端点行照写，仅供界面展示）。

### 8.2 全量回归结果与两个环境性假失败（2026-09-24）

`python -m unittest discover -s server/tests` 共 **1609 个用例**。结论：

- **本次改动相关的用例全绿**：`test_key_import` 32 / `test_key_export` 26 /
  i18n 一致性 30；前端 `tsc --noEmit` 0 错误、`eslint` 0 error（仅 1 条既有告警）、
  `next build`（导出模式）成功。
- **2 个失败**（`test_ssrf_guard` 的 `test_allowed_hosts` 与
  `test_public_target_is_actually_probed`）**与本改动无关**，是本机网络环境所致：
  代理软件以 TUN/fake-IP 模式把**全部**域名的 DNS 劫持到保留网段
  （`198.18.0.0/15`，基准测试保留段），于是 SSRF 守卫按"解析到内部地址"
  拒绝探测。
  退出代理或改直连后应恢复。
- **4 个 error** 由 WorkBuddy 运行时的 safe-delete shim 拦截测试清理阶段的批量删除
  引起（`sitecustomize.py` 的 `_exit_bulk_guard_control` 抛 `SystemExit: 1`）。
  用 `CODEBUDDY_SAFE_DELETE_ENABLED=0` 重跑后 **4 个 error 全部消失**
  （`FAILED (failures=2)`，仅剩上述环境性失败）。属环境干扰，非代码缺陷。

> 复现命令：
> `CODEBUDDY_SAFE_DELETE_ENABLED=0 .venv/Scripts/python.exe -m unittest discover -s server/tests -t . -p "test_*.py"`

### 8.3 修复：冷缓存导致导出/导入必失败「模型清单为空」（2026-09-24）

**现象**（本机实测，非推测）：面板重启后，在密钥弹窗点「导出」/「一键导入」连续报
**400 `模型清单为空且未指定默认模型`**；把密钥删掉重建仍然报，同一把密钥反复点
9 次全部 400。

**根因**：模型清单缓存是**进程内内存**（`modelcatalog._cache`），**面板一重启就清空**，
而只有打开「模型中心」页才会触发一次拉取。而导出路径 `_export_models` 当时
**刻意只读缓存、从不联网**（注释写的是"不该被上游慢响应拖住，缓存空时宁可少写
几个模型"）——但清单为空时**一个模型都写不出来**，只能硬失败。于是
「重启 → 建密钥 → 点导出」这条最普通的路径必然失败，且报出的原因与密钥无关，
用户无法自查。日志侧证据：该会话只走过 dashboard→accounts→tasks→keys，
**从未访问 `/models`**；两把密钥都是 `realm='cn'`、`models='[]'`。

**修法**（导出是一次性交互，多等一两秒远好于直接报错）：

1. 缓存有值 → 照旧只读缓存，**不发网络**（这条路径必须保持"不联网"）；
2. 缓存为空 → **实时拉一次**；拉到就用；
3. 仍为空 → **409** + 按成因给出可照做的说明，区分两种情形：
   - 该版本没有可用账号 → 明确指向「账号」页（光填白名单是绕不过去的）；
   - 拉取失败 → 提示稍后重试或手填白名单。
4. `keyexport._pick_models` 的最后一道兜底文案也一并改成可照做的
   （不再只丢一句"未指定默认模型"）。

**实现细节**：路由是同步 `def`（跑在线程池，刻意让 sqlite / `tasklist` / 写盘
不占用事件循环），而 `catalog()` 是 async，故新增
`modelcatalog.fetch_ids_blocking()` 作为同步桥：在线程内 `asyncio.run` 一个临时
事件循环。之所以安全：上游 HTTP 客户端全部是「每次调用新建 + `async with`」
（`config.http_client`），不绑定任何事件循环；工作线程内没有运行中的循环；
`asyncio.run` 的信号处理只在主线程生效。若当前线程**已有**运行中的循环，则
拒绝自建循环、按"取不到"处理，宁可不拉也不把请求卡死。
（**不要**把这个"取不到就拉一次"挪到「保存密钥时校验模型名」那条路：它挂在
输入框失焦上，一次上游慢响应就会把交互拖住。）

**验证**：冷缓存下 `POST /api/keys/export`（白名单为空）→ **200，17 个模型**
（来源=腾讯接口）；生成的配置 `ANTHROPIC_BASE_URL` 为根地址、`ANTHROPIC_MODEL`
为 `cn:auto`。新增 9 个回归用例（缓存命中不联网 / 冷缓存拉一次成功 / 无账号指向
「账号」页 / 拉取失败提示重试 / 版本措辞 / 显式白名单不碰缓存与网络 / 导入共用
同一路径 / 同步桥能跑通 / 同步桥在已有事件循环时拒绝）。

### 8.4 自动检测 + 自动关闭/拉起 + 深链：真机复测（2026-09-24）

做法：在本机跑 `C:/tmp/wb_live_verify.py`，**写入目标仍是真实库的临时副本**，
真实 `cc-switch.db` 全程只读并逐次校验 sha256。

**1) 自动检测**（不填任何环境变量）

| 客户端 | 命中来源 | 结果 |
|---|---|---|
| cc-switch | `process`（正在运行） | `E:\cc swich\cc-switch.exe` ← **路径带空格** |
| ZCode | `process`（正在运行） | `E:\zcode\ZCode.exe` |

两个都命中了，说明"运行中取进程映像路径"这条最省事也最准（`QueryFullProcessImageName`
拿到的就是绝对路径本身，不需要任何猜测），且带空格的路径不影响后续 `Popen`。

**2) 关闭 → 写入 → 重新拉起**（`mode='direct'`，授权 `close_running`）

- `stop('ccswitch')` → `{'stopped': True, 'forced': True}`，**耗时 16.3 秒**
  → 优雅关闭 12 秒没成功，走了升级强杀（`forced=True`）。进程 16400 → 消失 ✔
  - 复测提醒：**16 秒的等待对界面是可见的**，所以前端在按钮上要有"正在处理"态
    （本次已加），不能让它看起来像卡死。
- 写入副本 → `action='created'`，副本 `providers` **6 → 7 行**，新行为
  `('WorkBuddy 真机自检', 0)`；同目录生成 `.wbimport-20260924-232954.bak`
- `reopen()` → `True`，1.0 秒，新 pid **17820**，窗口枚举回到 `('CC Switch', True)`
- **真实库 sha256 前后都是 `05e31d5a750a56df` → 未被触碰 ✔**

**3) 官方深链**

- 构造出的 URL 长度 622 字符，逐字段核对无误（`resource=provider` / `app=claude` /
  `name` / `config`（379B base64 JSON）/ `configFormat=json` / `endpoint` /
  `apiKey` / `model`，base64 的 `=` 正确编码为 `%3D`，无 `enabled`）
- `open_url()` → `True`（`os.startfile` 成功转交 shell），3 秒后前台窗口仍是
  cc-switch 的 `CC Switch`（同一个 pid，没有起第二个实例）
- **从外部无法确认客户端有没有弹确认框**（窗口标题不变、真实库里没有新行）。
  于是改查客户端自己的日志 `~/.cc-switch/logs/cc-switch.log`，拿到了确定结论：

  ```
  [2026-09-24][23:29:59][INFO][cc_switch_lib] === Single Instance Callback Triggered ===
  [2026-09-24][23:29:59][INFO][cc_switch_lib] ✓ Deep link URL detected from single_instance args:
      ccswitch://v1/import?[keys:apiKey,app,config,configFormat,endpoint,model,name,resource]
  [2026-09-24][23:29:59][INFO][cc_switch_lib] ✓ Successfully parsed deep link:
      resource=provider, app=Some("claude"), name=Some("WorkBuddy 自检（可删）")
  [2026-09-24][23:29:59][INFO][cc_switch_lib] ✓ Emitted deeplink-import event to frontend
  ```

  三点都确认了：**单实例回调被触发**（URL 被转发给了已在运行的实例，没有起第二个
  进程）；**参数全部被识别**（日志打印的 key 集合与面板发送的一字不差）；
  **解析成功并已把 `deeplink-import` 事件推给前端**——也就是确认框那一步。
  这一次用户没有点确认，所以库里始终是 6 行、sha256 未变，这恰好也验证了
  "人不确认就不会写入"这条保护。
- 排查提示：**`~/.cc-switch/logs/cc-switch.log` 是验证深链的最佳取证点**，
  比看窗口可靠得多。为拿到它，cc-switch 需要在投递 URL *之前*就已启动
  （日志里的 `Registering deep-link URL handler` 出现在启动阶段）。
- 接口因此返回 `method='deeplink'` / `action='handed-off'`，界面文案只说
  "已交给 <客户端> 处理，请在它的窗口里确认"，不说"已导入成功"。

**附带结论**：`_locate()` 原来每次 `status()` 都要翻注册表（约 2 秒），现已加 `cheap` 档
（只走 env → 协议注册表 → 缓存 → 常见安装位，跳过进程枚举与卸载注册表），
`status()` 用 `cheap`、真正导入时才走全链。

## 9. 建议的实现顺序（供作者取舍）


1. ~~**后端**：导出配置片段（只读、无副作用）~~ ✅ 已合入：`POST /api/keys/export`
2. ~~**前端**：一次性展示弹窗加按钮 → 接导出（复制）~~ ✅ 已合入
3. ~~**本机写入（可选特性，默认关闭）**~~ ✅ 已合入：
   - `GET /api/keys/import-local/status`（只读探测，供界面提前提示）
   - `POST /api/keys/import-local`（写入；**三重门**：`WB_LOCAL_IMPORT=1`
     + 请求来源必须是回环地址 + 客户端已安装且未在运行）
   - 写前同目录 `*.wbimport-*.bak` 备份；失败回滚；写审计且审计不含密钥
   - 实现要点见 `server/services/keyimport.py` 的模块说明
4. ~~**上游客户端配合**：向 cc-switch / ZCode 提议导入 scheme 或 CLI（§5）~~
   ✅ cc-switch **已自带** `ccswitch://v1/import`，本项目已接入（§10）；
   ZCode 仍待作者支持 `zcode://import`（它注册了 `zcode://`，但格式未公开，**刻意不猜**）。
5. **新增待确认项**：`meta.endpointAutoSelect` 的语义（§8.1 末段）。确认后
   本实现的端点行可以真正参与端点选择，而不是仅作展示。
6. ~~**缺陷修复**：冷缓存导致导出/导入必失败~~ ✅ 已合入（见 §8.3）——
   缓存为空时实时拉一次，仍取不到则 409 并说清该去做什么。
7. ~~**客户端定位**：路径因机器而异~~ ✅ 已合入：五级定位链 + 协议注册表 +
   受限扫描 + 结果缓存，并新增 `GET /api/keys/import-local/detect`（见 §10.3）
8. ~~**客户端生命周期**：不要求用户自己先退出再回来~~ ✅ 已合入：深链优先；
   只能直写时由面板关闭客户端、写完再拉起来（见 §8.4）

## 10. 优先走客户端官方深链（`ccswitch://v1/import`）

### 10.1 为什么这比直写数据库好

cc-switch 已注册 `ccswitch://` 协议，官方文档 §5.3 公布了完整的导入深链格式：

```
ccswitch://v1/import?resource=provider&app=claude|codex&name=<显示名>
    &endpoint=<base url>&apiKey=<密钥>&model=<默认模型>
    &config=<base64 配置>&configFormat=json|toml&enabled=true
```

| 维度 | 直写 SQLite | 官方深链 |
|---|---|---|
| 表结构变更 | 客户端升级换 schema 就可能写坏 | 由客户端自己解析，面板不必知道 schema |
| `meta.endpointAutoSelect` 等未公开语义 | §7 末条：猜错会把流量静默改到别的地址 | 不用猜，客户端自己决定 |
| 客户端正在运行 | 必须先关掉（否则被它的保存整体覆盖） | **不用关**，由它自己入库 |
| 弹确认框 | 无（面板单方面写入） | **有**——用户能看见并否决，多一道人审 |
| 失败可诊断 | 靠异常 | 客户端会自己提示为什么不合规 |

结论：**能走深链就走深链**。直写降级为"客户端没注册协议 / 深链打不开"时的后备，
而不是主线——这与 §5 原来的设想（"希望客户端提供 scheme"）正好被官方实现满足了。

### 10.2 实现要点

- `keyimport.build_ccswitch_deeplink(...)` 拼 URL：
  - `config` 是对该 app 的 **base64** 配置：claude 传 `settings_config` 的 JSON
    （`configFormat=json`）、codex 传其中的 TOML 文本（`configFormat=toml`）；
  - `endpoint` / `apiKey` / `model` 是冗余但又必须给（客户端优先取 URL 参数）；
  - `enabled=true` **只在用户勾了"设为当前供应商"时才给**——默认不给，免得
    导入一个还没想好的供应商就把正在用的顶掉。
- 深链**不落盘**：`action='handed-off'`，`target`/`backup` 为空，不产生 `.bak`，
  也不改任何文件（回归测试里用"库字节不变 + 无 `.bak` + 没调用 stop"三重断言锁住）。
- `mode` 参数：`auto`（默认，cc-switch 能深链就深链）/ `deeplink`（强制，客户端没注册
  协议就 409）/ `direct`（强制走文件写入）。
- 响应新增 `method`（`deeplink`/`direct`）与 `lifecycle`（`stopped`/`forced`/
  `restarted`/`exe`），界面据此显示不同文案与提示。
- **ZCode 不猜**：`zcode://` 虽然注册在册，但格式未公开，本实现只把它用于
  **定位 exe**，绝不据此拼导入链接。

### 10.3 客户端定位：五级链 + 协议注册表

原提案只要求"检测客户端是否安装"，但真实机器的安装路径千差万别
（本机 cc-switch 在 `E:\cc swich\`，还带空格）。现实现为按成本递增的五级链，
**命中即停并写缓存**：

1. `WB_CCSWITCH_EXE` / `WB_ZCODE_EXE`（用户显式指定，最强）
2. **运行中的进程**（Win32 `QueryFullProcessImageName`）——客户端开着时最准
3. **协议注册表** `HKCU\Software\Classes\<协议>\shell\open\command`
   —— 实测 `ccswitch → "E:\cc swich\cc-switch.exe" "%1"`、`zcode → "E:\zcode\ZCode.exe" "%1"`。
   **客户端没开也准**，且安装位置一变就自动跟着变（客户端自己写的注册表）。
   取值时要挡掉 `InstallLocation` 指向卸载器的情况（实测 ZCode 的 `DisplayIcon`
   是 `Uninstall ZCode.exe`），故有 `unins/installer/setup/update/crashpad` 黑名单。
4. 上次检测结果的缓存（`WB_CLIENT_PATHS_FILE`，默认 `data/` 下）
5. 常见安装位 → （最后）**受限扫描**：只扫固定盘（`GetDriveType==3`）、BFS、
   双预算（4000 目录 / 6 秒）、跳过 `node_modules` 等噪声目录

定位链同时服务于两件事：**深链投递目标** 和 **要关掉/拉起的那个进程是谁**。
新增 `GET /api/keys/import-local/detect`（同样受回环来源+开关限制），
供界面上「自动检测」按钮使用；`status` 里也带出 `exe` / `exe_source` /
`needs_close` / `deeplink` 四个字段，让按钮在打开弹窗时就处于正确状态。
