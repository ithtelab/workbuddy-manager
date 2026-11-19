# WorkBuddy Manager (WebUI) 设计与实施方案

> **项目目标**：为 `workbuddy2api`（腾讯 CodeBuddy 转 OpenAI 代理）开发一套现代化的 Web 管理控制台，实现可视化多账号查看、一键扫码添加、全自动签到与状态监控。
>
> **UI 风格**：对标 **LDC（`linux-do/cdk`）** —— shadcn/ui + Tailwind 中性 zinc 灰阶，亮色为主/暗色可切，扁平克制、类 GitHub 的社区产品质感。
>
> **技术底座**：Python FastAPI 后端 + Vue3 CDN 单文件前端（免构建），设计令牌完全复刻 LDC。

---

## 一、 当前服务器核心环境与服务清单

在开发与联调 WebUI 时，需直接对接以下现有的服务器与容器服务：

### 1. 基础连接信息
- **公网 IP**：`203.0.113.10`
- **SSH 端口**：`22`
- **SSH 用户名**：`root`
- **SSH 密码**：`请在此填写你的SSH密码`
- **系统架构**：Ubuntu 24.04 LTS (x86_64)，已挂载 4GB 虚拟内存 Swap

### 2. 现存端口与服务分布
| 服务名称 | 监听端口 | 宿主机路径 / 容器名 | 作用说明 |
|---|---|---|---|
| **WorkBuddy2API** | **`7863`** | `/opt/workbuddy2api` (`workbuddy2api`) | 腾讯 CodeBuddy 转 OpenAI 代理核心后端 |
| **WebUI 规划端口** | **`7864`** | `/opt/workbuddy-web` (待部署) | **本次计划开发的 Web 管理端端口** |
| Antigravity Tools | `8320` | `/opt/agm` (`antigravity-manager`) | Gemini 号池网关（UI 参照标杆） |
| sub2api 网关 | `8080` | `/opt/1panel/apps/...` (`1Panel-sub2api-Mkkl`) | 上层分销、计费与模型路由服务 |
| 1Panel OpenResty | `80` / `443` | `/opt/1panel/www/` (`1Panel-openresty-nx9i`) | 反向代理与 SSL 证书管理 |
| 青龙面板 | `5700` | `1Panel-qinglong-tryj` | 自动化脚本面板 |
| 1Panel 管理端 | `10198` | 宿主机进程 (安全入口 `/your-entry-path`) | 服务器运维总控面板 |

### 3. WorkBuddy2API 当前关键凭据
- **全局 API Key**：`请在此填写你的上游APIKey`
- **容器内部通信地址**：`http://172.17.0.1:7863`
- **账号授权存放目录**：`/opt/workbuddy2api/auths/`（每个账号存为一个 `workbuddy-<uid>.json`）
- **状态存储文件**：`/opt/workbuddy2api/data/state.json`
- **配置文件**：`/opt/workbuddy2api/config.json`

---

## 二、 腾讯 CodeBuddy 认证协议逆向核心（后端通信依据）

WebUI 后端需实现与腾讯接口的交互。底层请求与 `workbuddy2api` 的 `cmd/login/main.go` 保持完全一致：

```
                    ┌─────────────────────────┐
                    │ 腾讯官方 Copilot 接口    │
                    │ (copilot.tencent.com)   │
                    └────────────┬────────────┘
                                 ▲
          1. 请求授权链接          │ 3. 轮询扫码结果 / 换取 Token
  POST /v2/plugin/auth/state     │ GET /v2/plugin/auth/token?state=...
                                 │ 4. 拿用户信息
                                 │ GET /v2/plugin/login/account?state=...
                                 ▼
                    ┌─────────────────────────┐
                    │ WorkBuddy WebUI 后端    │
                    └────────────┬────────────┘
                                 ▲
          前端点击【添加账号】     │ 前端每 2 秒轮询一次
          返回 二维码+链接        │ 扫码完成 -> 自动签到 -> 写入 auths/
                                 ▼
                    ┌─────────────────────────┐
                    │ WorkBuddy Web 前端页面   │
                    │ (仿 AGM 暗黑科技风)      │
                    └─────────────────────────┘
```

### 核心接口规范（已对照 `cmd/login/main.go` 源码核实）

> **重要**：所有腾讯接口返回统一信封 `{code, msg, data}`，`code != 0` 即为业务失败（含仍在等待登录）。

1. **通用请求头**：
   ```
   Content-Type: application/json
   Accept: application/json, text/plain, */*
   X-Requested-With: XMLHttpRequest
   User-Agent: CLI/2.63.2 CodeBuddy/2.63.2
   Origin: https://www.codebuddy.cn
   Referer: https://www.codebuddy.cn/
   ```

2. **第一步：获取登录 State 和授权 URL**
   - 请求：`POST https://copilot.tencent.com/v2/plugin/auth/state?platform=CLI`
   - 数据体：`{}`
   - 成功响应 `data`：`{ "state": "...", "authUrl": "https://..." }`

3. **第二步：轮询授权结果（前端每 2 秒调一次）**
   - 请求：`GET https://copilot.tencent.com/v2/plugin/auth/token?state={state}`
   - **未扫码**：`code != 0`（返回等待状态，应继续轮询）
   - **扫码成功**：`code == 0`，`data` 为 `{ "accessToken": "...", "refreshToken": "...", "expiresIn": 3600, "domain": "..." }`
   - ⚠️ 字段是**驼峰**：`accessToken` / `refreshToken` / `expiresIn`

4. **第三步：获取账号 UID 与昵称**
   - 请求：`GET https://copilot.tencent.com/v2/plugin/login/account?state={state}`
   - **必须带**：`Authorization: Bearer {accessToken}`
   - 成功响应 `data`：`{ "uid": "...", "enterpriseId": "...", "nickname": "..." }`
   - ⚠️ 字段是驼峰：`enterpriseId`

5. **第四步：自动每日签到（领额度）**
   - 请求：`POST https://www.codebuddy.cn/v2/billing/meter/daily-checkin`
   - 请求头增加：`Authorization: Bearer {accessToken}`
   - 数据体：`{}`
   - ⚠️ 签到失败会以 **HTTP 4xx** 返回（如 `code=10001` 表示"今天已签到"），**属正常幂等，不应视为错误中断流程**

6. **第五步：落盘授权文件（⚠️ 嵌套结构，非扁平）**
   - 路径：`/opt/workbuddy2api/auths/workbuddy-{uid}.json`
   - `expiresAt = 当前Unix时间戳 + expiresIn`
   - JSON 结构（**必须严格遵循，否则 internal/auth 读取失败**）：
     ```json
     {
       "account": {
         "uid": "12345678",
         "enterpriseId": "",
         "nickname": "微信昵称"
       },
       "auth": {
         "accessToken": "eyJ...",
         "refreshToken": "eyJ...",
         "expiresAt": 1788200000,
         "domain": ""
       }
     }
     ```

7. **第六步：重启容器加载新账号**
   - `docker restart workbuddy2api`
   - 校验：`GET http://127.0.0.1:7863/status` + `Authorization: Bearer {config.json 里的 api_key}`，返回体含 `accounts` 数组。

---

## 二·五、 workbuddy2api 自身接口清单（WebUI 数据来源，已实测）

WebUI 的后端需要向 `workbuddy2api`（`127.0.0.1:7863`）拉取运行时状态，接口如下：

| 方法 | 路径 | 鉴权 | 用途 |
|---|---|---|---|
| GET | `/healthz` | 无 | 存活探测，返回 `{"healthy":N,"total":N}` |
| GET | `/status` | ✅ Bearer | **核心状态**：账号池运行概览 |
| GET | `/v1/models` | ✅ Bearer | 可用模型列表（OpenAI 格式） |
| POST | `/v1/chat/completions` | ✅ Bearer | 对话代理（OpenAI 兼容） |
| POST | `/v2/chat/completions` | ✅ Bearer | 同上（v2 路径） |
| POST | `/token/refresh` | ✅ Bearer | 手动触发令牌刷新 |

### `/status` 实测返回结构（WebUI 看板数据源）
```json
{
  "accounts": [],        // 账号明细数组
  "cooling": 0,          // 冷却中的账号数
  "disabled": 0,         // 已禁用账号数
  "healthy": 0,          // 健康账号数
  "in_flight_full": 0,   // 并发已满标志
  "redis_mode": "noop",  // Redis 模式（noop=未启用）
  "sticky_sessions": 0,  // 粘性会话数
  "total": 0             // 账号总数
}
```

### 可用模型清单（实测 `/v1/models` 返回）
腾讯 CodeBuddy 当前暴露的模型（**这些才是你能拿去接入 sub2api 卖的真实模型**）：

| 模型 ID | 上下文 | 说明 |
|---|---|---|
| `glm-5.2` | 131072 | 智谱 GLM-5.2 |
| `glm-5.1` | 131072 | 智谱 GLM-5.1 |
| `glm-5v-turbo` | 131072 | GLM 视觉模型 |
| `kimi-k2.7` | 131072 | Kimi K2.7 |
| `minimax-m3` | 131072 | MiniMax M3 |
| `hy3` / `hy3-preview` / `hy3-preview-age…` | 131072 | 腾讯混元 3 系列 |

> ⚠️ 实际调用走 `owned_by: "workbuddy"`，模型名直接用上表 `id` 即可。

### WebUI 前端可调用的自建后端接口（本方案规划）
| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/accounts` | 读取 auths/ 目录，列出本地账号 |
| POST | `/api/auth/start` | 生成腾讯授权链接（供生成二维码） |
| GET | `/api/auth/poll?state=` | 轮询扫码结果，成功即自动签到+落盘 |
| DELETE | `/api/accounts/{filename}` | 删除指定账号 |
| POST | `/api/accounts/{filename}/checkin` | 单账号手动签到 |
| GET | `/api/upstream/status` | 代理透传 workbuddy2api 的 `/status`（可选） |

---

## 三、 UI 视觉规范 —— 对标 LDC (linux-do/cdk) 风格

> 设计来源：`https://github.com/linux-do/cdk`（LINUX DO CDK，714★，MIT）
> 整体风格：**shadcn/ui (new-york) + Tailwind v4 + 中性 zinc 灰阶**，干净、克制、类 GitHub 的社区产品质感。**亮色为主、暗色可切**，不使用花哨霓虹/玻璃拟态。

### 1. 技术底座（严格对齐 LDC 原项目）

| 项目 | LDC 的选择 | 说明 |
|---|---|---|
| 框架 | **Next.js 15**（App Router）+ React 19 + TypeScript | LDC 原生栈 |
| UI 组件 | **shadcn/ui**，`style: “new-york”`，`baseColor: “zinc”` | 圆角小、边框淡、密度高 |
| 样式 | **Tailwind CSS v4**（`@import “tailwindcss”`）+ CSS 变量 | 用 `@theme inline` 映射色板 |
| 主题 | **next-themes**，`defaultTheme=”system”`，`attribute=”class”` | 即 `.dark` 类切换 |
| 图标 | `lucide-react` + `@tabler/icons-react` | LDC 两者混用 |
| 提示 | **Sonner**（`<Toaster />`）+ 自定义图标着色 | 成功绿 / 错误红 / 警告黄 / 信息蓝 |
| 动效 | `motion`（Framer Motion 新版）+ `animate-ui` 的 `CountingNumber` | 数字滚动动画 |
| 字体 | **Inter**（拉丁）+ **Noto Sans SC**（中文），`font-sans antialiased` | 通过 `next/font/google` 注入 CSS 变量 |

> **如果不想上 Next.js**：第七节骨架仍用「单文件 HTML + CDN」实现，但**视觉令牌（色板/圆角/间距）全部照搬 LDC**，观感一致。

### 2. 设计令牌（直接取自 LDC 的 `globals.css`）

**圆角**
```css
--radius: 0.625rem;              /* 基础 10px */
--radius-sm: calc(var(--radius) - 4px);   /* 6px  */
--radius-md: calc(var(--radius) - 2px);   /* 8px  */
--radius-lg: var(--radius);               /* 10px */
--radius-xl: calc(var(--radius) + 4px);   /* 14px */
```
> 注意：LDC 的**统计卡片用的是 `rounded-[20px]`**，比基础圆角更圆，这是它的视觉特征之一。

**色板（oklch 中性灰，亮/暗双套）**

| 令牌 | 亮色 (Light) | 暗色 (Dark) | 用途 |
|---|---|---|---|
| `--background` | `oklch(1 0 0)` 纯白 | `oklch(0.141 0.005 285.823)` 近黑 | 页面底色 |
| `--foreground` | `oklch(0.141 0.005 285.823)` | `oklch(0.985 0 0)` | 主文字 |
| `--card` | `oklch(1 0 0)` | `oklch(0.21 0.006 285.885)` | 卡片底 |
| `--muted` | `oklch(0.967 0.001 286.375)` | `oklch(0.274 0.006 286.033)` | 次级底色（统计卡就用它） |
| `--muted-foreground` | `oklch(0.552 0.016 285.938)` | `oklch(0.705 0.015 286.067)` | 次要文字 |
| `--border` | `oklch(0.92 0.004 286.32)` | `oklch(1 0 0 / 10%)` | 描边（很淡） |
| `--destructive` | `oklch(0.577 0.245 27.325)` | `oklch(0.704 0.191 22.216)` | 删除/危险 |
| `--sidebar` | `rgb(249,250,251)` gray-50 | `rgb(31,41,55)` gray-800 | 侧边栏底（与背景同色） |
| `--sidebar-border` | `transparent` | `transparent` | **LDC 特意去掉侧栏边框** |

**语义色（Sonner Toast 用，与 LDC 一致）**
```css
成功 #10b981 / 深色 #34d399
错误 #ef4444 / 深色 #f87171
警告 #f59e0b / 深色 #fbbf24
信息 #3b82f6 / 深色 #60a5fa
```

**间距与容器（LDC 主布局实测）**
```
外层容器: mx-auto w-full max-w-7xl flex-1 flex-col gap-4 md:gap-6
内边距:   px-4 pt-12 py-8 sm:px-6 md:px-8 lg:px-12
顶栏:     ManagementBar（顶部横向管理栏，非侧边栏）
```
> **重要差异**：LDC 用的是**顶部 ManagementBar 横向导航栏**，不是左侧竖排侧边栏。

### 3. LDC 的核心组件范式（照抄即可）

**① 统计卡片 StatCard**（仪表盘四宫格就用它）
```
容器:   min-h-[88px] sm:min-h-[96px] rounded-[20px] bg-muted px-3.5 py-3 sm:px-4
标题:   text-[11px] font-medium text-gray-500 dark:text-gray-400（truncate）
图标:   h-6 w-6 rounded-full bg-white/70 dark:bg-white/[0.05] text-gray-500
数值:   text-xl sm:text-2xl font-semibold tracking-[-0.03em]
        text-gray-900 dark:text-gray-100（数字用 CountingNumber 滚动动画）
描述:   text-[11px] text-gray-500 dark:text-gray-400
```

**② 顶部管理栏 ManagementBar**
- 高度紧凑，`border-b` 一条极淡分隔线
- 左侧：Logo + 站名（`text-sm font-semibold`）
- 中间：导航项（`text-sm text-muted-foreground`，激活态变 `text-foreground` + 底部细线）
- 右侧：主题切换按钮（☀️/🌙）、用户头像下拉

**③ 列表项 CardList**（用于账号/用量排行）
- 序号：`w-4 text-[11px] font-medium tabular-nums text-gray-400`（`01`、`02` 补零）
- 头像：`Avatar` + `AvatarFallback`
- 右侧数值：`tabular-nums`，次要标签 `text-[11px] text-gray-500`

**④ 空状态 EmptyState / DashboardEmptyState**
- 居中图标（lucide，`text-muted-foreground`，尺寸偏大）
- 主文案 `text-sm font-medium`，副文案 `text-xs text-muted-foreground`
- 下方一个 `variant=”outline”` 的行动按钮

**⑤ 全局提示 Sonner**
- 位置默认右下，圆角 `rounded-lg`，`bg-popover`，描边 `border`
- 图标按类型着色（见上面语义色）

### 4. 页面结构设计（WorkBuddy Manager）

#### 模块 ①：顶部管理栏 (ManagementBar)
```
┌────────────────────────────────────────────────────────────────┐
│  🤖 WorkBuddy          [账号] [用量] [设置]        🌙  ⚙️  👤  │
└────────────────────────────────────────────────────────────────┘
```
- **左**：Logo 方块（`h-7 w-7 rounded-md bg-primary text-primary-foreground grid place-items-center`）+ 标题 `WorkBuddy Manager`（`text-sm font-semibold`）+ 版本徽标 `v1.0.0`（`text-[10px] text-muted-foreground`）
- **中**：导航三项 —— `账号` / `用量` / `设置`（激活项 `text-foreground`，其余 `text-muted-foreground`）
- **右**：主题切换 `Button variant=”ghost” size=”icon”`、`+ 添加账号`（`Button size=”sm”`，主色实心）

#### 模块 ②：统计卡片四宫格 (StatCard × 4)
```
┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│ 账号总数      │ │ 有效期内      │ │ 即将过期      │ │ 今日签到      │
│     12     ⭐│ │     11     ✅│ │      1     ⚠️│ │     12     🎁│
│ 已纳管账号    │ │ Token 正常    │ │ <1h 需刷新    │ │ 全员已领取    │
└──────────────┘ └──────────────┘ └──────────────┘ └──────────────┘
```
- 统一 `rounded-[20px] bg-muted`，无边框，靠底色区分
- 数值用 `CountingNumber` 滚动动效（或纯 CSS 数字过渡）
- 图标用 lucide（`Users` / `CircleCheck` / `TriangleAlert` / `Gift`）

#### 模块 ③：账号列表（shadcn `Table` 或 `Card` 网格）
**LDC 更偏表格**，建议主视图用 `Table`，可选切卡片视图：
```
┌────────────────────────────────────────────────────────────────────┐
│ 昵称             UID        状态      Token 有效期      操作        │
├────────────────────────────────────────────────────────────────────┤
│ ● 黑天鹅         89374***   🟢 在线   ▓▓▓▓▓▓░░ 47.2h   🎁 ⚡ 🗑️    │
│ ● 张三           91203***   🟢 在线   ▓▓▓▓▓░░░ 31.8h   🎁 ⚡ 🗑️    │
│ ● 测试号         88111***   🔴 过期   ░░░░░░░░ 已过期   🔑 🗑️      │
└────────────────────────────────────────────────────────────────────┘
```
- 行高紧凑，`hover:bg-muted/50`
- 状态用 `Badge`：在线 `variant=”secondary”`，过期 `variant=”destructive”`
- 有效期进度条用 shadcn `Progress`（`h-1.5`，配色 `bg-primary`）
- 操作列用 `Button variant=”ghost” size=”icon”` 的图标按钮组

#### 模块 ④：添加账号弹窗（shadcn `Dialog`）
```
        ┌───────────────────────────────┐
        │  添加腾讯账号              ✕  │
        │  使用微信 / QQ 扫码完成授权    │
        │                               │
        │        ┌─────────────┐        │
        │        │   QR CODE   │        │
        │        │   (200px)   │        │
        │        └─────────────┘        │
        │                               │
        │  ⏳ 等待手机扫码确认...        │
        │                               │
        │         [ 取消 ]              │
        └───────────────────────────────┘
```
- 用 shadcn `Dialog` + `DialogHeader` / `DialogTitle` / `DialogDescription` / `DialogFooter`
- 二维码区域：白色 `rounded-lg p-4`，居中
- 轮询状态文案用 `text-xs text-muted-foreground`；成功变 `text-emerald-500`
- 成功后 Sonner 弹 `toast.success('账号 [昵称] 授权成功')` 并 1.5s 后自动关闭

### 5. 与 AGM 风格的取舍（为什么改）

| 维度 | AGM 原风格 | **LDC 风格（改用）** |
|---|---|---|
| 底色 | 深蓝黑 `#0b0f19` 强制暗色 | **亮色为主 + 可切暗色**，zinc 中性灰 |
| 质感 | 玻璃拟态、霓虹发光 | **扁平、无阴影、极淡描边** |
| 圆角 | 12~16px 混用 | 统一 `--radius` 10px（卡片 20px） |
| 信息密度 | 大卡片、空隙大 | **紧凑、表格优先**，类 GitHub |
| 典型观感 | 游戏化后台 | **社区产品 / 开发者工具** |

---

## 四、 推荐技术栈与开发路径

只提供**一条推荐路线**（与 LDC 风格一致性最高、且最快出成果）：

### 推荐路线：Python 后端 + Vue3 CDN 前端（LDC 设计令牌，零构建）

* **架构**：
  - 后端：`server.py`（FastAPI），提供 `/api/*` 接口，同时托管静态页面。
  - 前端：单文件 `index.html`，**CDN 引入 Tailwind + Vue3 + QRCode.js**。
* **为什么这么选**：
  - **零 npm 构建**：改完 `index.html` 刷新浏览器即生效，不需要 `npm run build`，迭代极快。
  - **视觉照搬 LDC**：用 Tailwind CDN + 自定义 CSS 变量复刻 LDC 的 zinc 灰阶、`rounded-[20px]` 卡片、亮/暗双主题。
  - **部署简单**：`systemd` 常驻，`7864` 端口，内存占用 < 30MB。
* **取舍说明**：LDC 原项目用 Next.js 15 + shadcn/ui（React）。若你后续想**完全 1:1 复刻 LDC 组件**（Radix 交互、Sonner 提示、CountingNumber 动效），可再升级为 Next.js + shadcn 栈；但**第一阶段用 CDN 方案性价比最高**。

---

## 五、 后端核心处理伪代码原型 (Python / FastAPI)

你可以直接新建一个文件 `server.py`，其核心逻辑如下：

```python
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import urllib.request, urllib.error, json, os, time

app = FastAPI(title="WorkBuddy Manager")

AUTH_DIR = "/opt/workbuddy2api/auths"
UPSTREAM = "https://copilot.tencent.com"
CHECKIN_URL = "https://www.codebuddy.cn/v2/billing/meter/daily-checkin"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "CLI/2.63.2 CodeBuddy/2.63.2",
    "Origin": "https://www.codebuddy.cn",
    "Referer": "https://www.codebuddy.cn/",
}

# state 临时缓存：{state: 创建时间}，用于 /api/auth/start 与 /api/auth/poll 配对
_pending_states = {}

def get_json(url, method="GET", body=None, extra_headers=None):
    """调用腾讯接口，返回 (信封code, 信封data)。"""
    headers = dict(HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            env = json.loads(r.read().decode())
            return env.get("code", -1), env.get("data")
    except urllib.error.HTTPError as e:
        # 4xx 也可能是业务信封（如签到"今天已签到"）
        try:
            env = json.loads(e.read().decode() or "{}")
            return env.get("code", e.code), env.get("data")
        except Exception:
            return e.code, None


# ── 1. 账号列表（读取 auths 目录下的嵌套 JSON）─────────────────────
@app.get("/api/accounts")
def list_accounts():
    accounts = []
    if os.path.isdir(AUTH_DIR):
        for fn in sorted(os.listdir(AUTH_DIR)):
            if not fn.endswith(".json"):
                continue
            try:
                raw = json.load(open(os.path.join(AUTH_DIR, fn), encoding="utf-8"))
                acct = raw.get("account", {})
                auth = raw.get("auth", {})
                exp = auth.get("expiresAt", 0)
                accounts.append({
                    "file": fn,
                    "uid": acct.get("uid"),
                    "nickname": acct.get("nickname") or "未命名",
                    "enterprise_id": acct.get("enterpriseId", ""),
                    "expires_at": exp,
                    "is_expired": time.time() >= exp,
                    "remain_seconds": max(0, int(exp - time.time())),
                })
            except Exception:
                pass
    return {"total": len(accounts), "accounts": accounts}


# ── 2. 生成授权链接（返回给前端生成二维码）────────────────────────
@app.post("/api/auth/start")
def auth_start():
    code, data = get_json(
        f"{UPSTREAM}/v2/plugin/auth/state?platform=CLI", "POST", {}
    )
    if code != 0 or not data:
        return JSONResponse({"error": f"获取授权链接失败 code={code}"}, status_code=502)
    state = data.get("state")
    _pending_states[state] = time.time()
    return {"state": state, "authUrl": data.get("authUrl")}


# ── 3. 轮询扫码状态，成功后自动签到 + 落盘 + 重启 ─────────────────
@app.get("/api/auth/poll")
def auth_poll(state: str):
    if state not in _pending_states:
        return {"status": "invalid"}

    # 3.1 换 token（未完成时 code != 0）
    code, data = get_json(f"{UPSTREAM}/v2/plugin/auth/token?state={state}")
    if code != 0 or not data or not data.get("accessToken"):
        # 超时清理：5 分钟未完成自动失效
        if time.time() - _pending_states.get(state, 0) > 300:
            _pending_states.pop(state, None)
            return {"status": "expired"}
        return {"status": "waiting"}

    access_token = data["accessToken"]
    refresh_token = data.get("refreshToken", "")
    expires_in = data.get("expiresIn", 3600)
    domain = data.get("domain", "")

    # 3.2 拿 uid / nickname（必须带 Bearer）
    _, acct = get_json(
        f"{UPSTREAM}/v2/plugin/login/account?state={state}",
        extra_headers={"Authorization": f"Bearer {access_token}"},
    )
    acct = acct or {}
    uid = acct.get("uid")
    if not uid:
        return {"status": "waiting"}

    # 3.3 自动每日签到（幂等：失败/已签到都不阻断）
    get_json(CHECKIN_URL, "POST", {},
             extra_headers={"Authorization": f"Bearer {access_token}"})

    # 3.4 落盘（严格嵌套结构）
    auth_obj = {
        "account": {
            "uid": uid,
            "enterpriseId": acct.get("enterpriseId", ""),
            "nickname": acct.get("nickname", ""),
        },
        "auth": {
            "accessToken": access_token,
            "refreshToken": refresh_token,
            "expiresAt": int(time.time()) + int(expires_in),
            "domain": domain,
        },
    }
    os.makedirs(AUTH_DIR, exist_ok=True)
    target = os.path.join(AUTH_DIR, f"workbuddy-{uid}.json")
    existed = os.path.exists(target)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(auth_obj, f, ensure_ascii=False, indent=1)

    _pending_states.pop(state, None)

    # 3.5 异步重启容器加载新号
    os.system("docker restart workbuddy2api >/dev/null 2>&1 &")

    return {
        "status": "success",
        "uid": uid,
        "nickname": acct.get("nickname", ""),
        "updated": existed,   # true=覆盖更新, false=新增
    }


# ── 4. 删除指定账号 ────────────────────────────────────────────
@app.delete("/api/accounts/{filename}")
def delete_account(filename: str):
    # 防目录穿越
    if "/" in filename or ".." in filename:
        return JSONResponse({"error": "invalid filename"}, status_code=400)
    target = os.path.join(AUTH_DIR, filename)
    if os.path.exists(target):
        os.remove(target)
        os.system("docker restart workbuddy2api >/dev/null 2>&1 &")
        return {"success": True}
    return JSONResponse({"error": "not found"}, status_code=404)


# ── 5. 单个账号手动签到 ────────────────────────────────────────
@app.post("/api/accounts/{filename}/checkin")
def manual_checkin(filename: str):
    if "/" in filename or ".." in filename:
        return JSONResponse({"error": "invalid filename"}, status_code=400)
    target = os.path.join(AUTH_DIR, filename)
    if not os.path.exists(target):
        return JSONResponse({"error": "not found"}, status_code=404)
    raw = json.load(open(target, encoding="utf-8"))
    token = raw.get("auth", {}).get("accessToken", "")
    code, data = get_json(CHECKIN_URL, "POST", {},
                          extra_headers={"Authorization": f"Bearer {token}"})
    return {"code": code, "message": "签到成功或今日已签到" if code in (0, 10001) else "签到失败"}


# ── 6. 单账号连通性测试（前端 ⚡ 按钮调用）──────────────────────
@app.post("/api/accounts/{filename}/test")
def test_account(filename: str):
    if "/" in filename or ".." in filename:
        return JSONResponse({"error": "invalid filename"}, status_code=400)
    target = os.path.join(AUTH_DIR, filename)
    if not os.path.exists(target):
        return JSONResponse({"error": "not found"}, status_code=404)

    raw = json.load(open(target, encoding="utf-8"))
    token = raw.get("auth", {}).get("accessToken", "")
    if not token:
        return {"ok": False, "message": "该账号无有效 accessToken"}

    # 用最小请求探测上游连通性（模型名用 workbuddy2api 暴露的任一模型）
    payload = {
        "model": "glm-5.2",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }
    url = f"{UPSTREAM}/v2/chat/completions"
    try:
        code, data = get_json(url, "POST", payload,
                              extra_headers={"Authorization": f"Bearer {token}"})
        if code == 0:
            return {"ok": True, "message": "连通正常 ✅"}
        return {"ok": False, "message": f"上游返回 code={code}"}
    except Exception as e:
        return {"ok": False, "message": f"请求异常: {e}"}
```

---

## 五·五、 鉴权与对外访问（像 AGM 一样，多人可用）

> **需求**：AGM 的 WebUI 是「设一个管理密码 → 任何人凭密码从公网访问」。本方案对齐这一形态，并额外支持**多用户 + 角色分级**，方便团队共用。

### 1. 鉴权设计要点

| 设计项 | 方案 | 对齐 AGM |
|---|---|---|
| 登录方式 | **用户名 + 密码**（AGM 只有单一密码，这里做增强） | ✅ |
| 会话保持 | **HttpOnly + SameSite=Lax 签名 Cookie**，有效期 7 天 | ✅ |
| 密码存储 | **PBKDF2-SHA256 加盐哈希**（标准库实现，无额外依赖） | ✅ |
| 角色分级 | `admin`（可增删账号）/ `viewer`（只读，可看不可改） | ➕ 增强 |
| 登录防爆破 | 同 IP 连续失败 5 次锁定 10 分钟 | ➕ 增强 |
| 机机调用 | 可选 **API Key**（`X-API-Key`），供脚本/自动化使用 | ➕ 增强 |
| 传输安全 | 由 1Panel 反代提供 HTTPS；Cookie 自动带 `Secure` | ✅ |

> **多用户的意义**：老板用 `admin`，客服/同事用 `viewer` 只读查看账号状态，避免误删号。

### 2. 用户配置：`users.json`

```json
{
  "secret": "请改成随机字符串(用于签名Cookie)",
  "users": [
    {
      "username": "admin",
      "role": "admin",
      "pwd_hash": "pbkdf2_sha256$260000$<salt_hex>$<hash_hex>"
    },
    {
      "username": "guest",
      "role": "viewer",
      "pwd_hash": "pbkdf2_sha256$260000$<salt_hex>$<hash_hex>"
    }
  ],
  "api_keys": ["wbk_xxxxxxxxxxxxxxxx"]
}
```

**生成密码哈希的小工具**（部署时跑一次即可）：
```python
import hashlib, secrets
def make_hash(pwd: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pwd.encode(), bytes.fromhex(salt), 260000)
    return f"pbkdf2_sha256$260000${salt}${h.hex()}"
print(make_hash("你的密码"))
```

### 3. 后端鉴权代码（追加到 `server.py`）

```python
import hmac, hashlib, secrets, base64, time
from fastapi import Request, Depends, HTTPException
from fastapi.responses import JSONResponse

USERS_FILE = "/opt/workbuddy-manager/users.json"
SESSION_DAYS = 7
_fail_counter = {}   # {ip: [失败次数, 首次失败时间]}

def _load_users():
    return json.load(open(USERS_FILE, encoding="utf-8"))

def _verify_pwd(pwd: str, stored: str) -> bool:
    try:
        algo, iters, salt, expect = stored.split("$")
        h = hashlib.pbkdf2_hmac("sha256", pwd.encode(), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(h.hex(), expect)
    except Exception:
        return False

def _sign(payload: str, secret: str) -> str:
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()

def _unsign(token: str, secret: str):
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        payload, sig = raw.rsplit("|", 1)
        if not hmac.compare_digest(
            hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest(), sig
        ):
            return None
        obj = json.loads(payload)
        if obj.get("exp", 0) < time.time():
            return None
        return obj
    except Exception:
        return None

@app.post("/api/login")
async def login(req: Request):
    ip = req.client.host
    cnt, first = _fail_counter.get(ip, [0, time.time()])
    if cnt >= 5 and time.time() - first < 600:
        return JSONResponse({"error": "失败次数过多，请 10 分钟后再试"}, status_code=429)

    body = await req.json()
    cfg = _load_users()
    user = next((u for u in cfg["users"] if u["username"] == body.get("username")), None)
    if not user or not _verify_pwd(body.get("password", ""), user["pwd_hash"]):
        _fail_counter[ip] = [cnt + 1, first]
        return JSONResponse({"error": "用户名或密码错误"}, status_code=401)

    _fail_counter.pop(ip, None)
    payload = json.dumps({
        "u": user["username"], "r": user["role"],
        "exp": int(time.time()) + SESSION_DAYS * 86400
    })
    resp = JSONResponse({"ok": True, "username": user["username"], "role": user["role"]})
    resp.set_cookie(
        "wb_session", _sign(payload, cfg["secret"]),
        max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", path="/"
        # 走 HTTPS 时再加 secure=True（或让反代注入）
    )
    return resp

@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("wb_session", path="/")
    return resp

# ── 鉴权依赖：所有业务接口都挂上它 ──────────────────────────────
def current_user(req: Request):
    cfg = _load_users()
    # 1) 优先 Cookie 会话
    tok = req.cookies.get("wb_session")
    if tok:
        obj = _unsign(tok, cfg["secret"])
        if obj:
            return obj
    # 2) 备用：API Key（机机调用）
    key = req.headers.get("X-API-Key")
    if key and key in cfg.get("api_keys", []):
        return {"u": "api", "r": "admin"}
    raise HTTPException(status_code=401, detail="未登录")

def require_admin(user=Depends(current_user)):
    if user.get("r") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user

@app.get("/api/me")
def me(user=Depends(current_user)):
    return {"username": user["u"], "role": user["r"]}
```

**给现有接口挂上鉴权**（把 `Depends(current_user)` 加进去）：

```python
@app.get("/api/accounts")
def list_accounts(user=Depends(current_user)):     # 只读，viewer 也可
    ...

@app.delete("/api/accounts/{filename}")
def delete_account(filename: str, user=Depends(require_admin)):   # 仅 admin
    ...

@app.post("/api/accounts/{filename}/checkin")
def manual_checkin(filename: str, user=Depends(require_admin)):   # 仅 admin
    ...

@app.post("/api/auth/start")
def auth_start(user=Depends(require_admin)):        # 添加账号仅 admin
    ...

@app.get("/api/auth/poll")
def auth_poll(state: str, user=Depends(require_admin)):
    ...
```

> **务必**：`/api/login`、`/api/logout`、`/api/healthz` 这三个**不要**挂鉴权依赖。

### 4. 前端登录门禁（改造 `index.html`）

> 第七节骨架的根节点是 `<div id="app">`，内部依次是 `header` / `main` / 两个弹窗。
> **改造方法**：把「`header` + `main` + 两个弹窗」整体包进 `<template v-if="me">`，并在其**前面**加一个 `<template v-else>` 的登录卡片。

```html
<div id="app">
  <!-- ① 未登录：登录卡片 -->
  <template v-else>   <!-- 注意：与下面的 v-if 配对，实际写在 v-if 之前 -->
    <div class="min-h-screen grid place-items-center">
      <div class="w-[360px] rounded-ldc border border-ldc p-6" :style="{ background: 'var(--card)' }">
        <h1 class="text-base font-semibold mb-1">WorkBuddy Manager</h1>
        <p class="text-xs mb-5" :style="{ color: 'var(--muted-foreground)' }">请登录以继续</p>
        <input v-model="loginForm.username" placeholder="用户名"
               class="w-full h-9 px-3 rounded-md border border-ldc text-sm mb-2"
               :style="{ background: 'var(--background)' }" />
        <input v-model="loginForm.password" type="password" placeholder="密码" @keyup.enter="doLogin"
               class="w-full h-9 px-3 rounded-md border border-ldc text-sm mb-3"
               :style="{ background: 'var(--background)' }" />
        <p v-if="loginErr" class="text-xs text-red-500 mb-2">{{ loginErr }}</p>
        <button @click="doLogin" class="w-full h-9 rounded-md text-sm font-medium"
                :style="{ background: 'var(--primary)', color: 'var(--primary-foreground)' }">登录</button>
      </div>
    </div>
  </template>

  <!-- ② 已登录：包住原有的 header / main / 弹窗 -->
  <template v-if="me">
    <!-- 原来的 <header> 管理栏 ... -->
    <!-- 原来的 <main> 统计卡 + 账号表格 ... -->
    <!-- 原来的 添加账号弹窗 / Toast ... -->
  </template>
</div>
```

> 正确写法是把 `<template v-if="me">` 放在前面、`<template v-else>` 放在后面（上面为便于阅读把说明写在前面）。 Vue 要求 `v-if`/`v-else` 相邻。

```js
// setup() 内新增
const me = ref(null);
const loginForm = ref({ username: '', password: '' });
const loginErr = ref('');

async function checkMe() {
  try {
    const r = await fetch('/api/me');
    if (r.ok) me.value = await r.json();
  } catch (e) {}
}

async function doLogin() {
  loginErr.value = '';
  const r = await fetch('/api/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(loginForm.value)
  });
  const d = await r.json();
  if (!r.ok) { loginErr.value = d.error || '登录失败'; return; }
  me.value = { username: d.username, role: d.role };
  loadAccounts();
}

async function doLogout() {
  await fetch('/api/logout', { method: 'POST' });
  me.value = null;
}

// 替换原来的 onMounted(loadAccounts)，改为先校验登录态
onMounted(() => {
  const saved = localStorage.getItem('wb-theme');
  if (saved === 'dark' || (!saved && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
    isDark.value = true;
    document.documentElement.classList.add('dark');
  }
  checkMe().then(() => { if (me.value) loadAccounts(); });
});
// 返回里记得加上 me, loginForm, loginErr, doLogin, doLogout
```

**界面上的角色差异**（`viewer` 隐藏危险按钮）：
```html
<button v-if="me?.role === 'admin'" @click="del(a)" ...>🗑️</button>
<button v-if="me?.role === 'admin'" @click="openAdd" ...>+ 添加账号</button>
```
**右上角加退出按钮**（`admin`/`viewer` 都显示）：
```html
<button @click="doLogout" class="h-8 px-3 rounded-md border border-ldc text-xs hover:opacity-70">退出</button>
```

### 5. 对外开放（步骤）

1. 启动服务（监听 `0.0.0.0:7864`）。
2. **1Panel → 网站 → 创建反向代理**：
   - 域名：`wb.example.com`（换成你自己的域名）
   - 目标：`http://127.0.0.1:7864`
   - **务必加** `client_max_body_size 2m;`（虽然本面板无大请求体，但统一习惯）
3. **申请 Let's Encrypt 证书**并开启强制 HTTPS → Cookie 自动受 TLS 保护。
4. 登录后按需在 `users.json` 里增删用户（改完重启服务生效）。
5. **可选加固**：
   - 在 1Panel 里对 `wb.example.com` 配 **IP 白名单**（只允许公司/家庭 IP 访问）。
   - 或叠加 Cloudflare 的 Access / WAF。

### 6. 安全红线（务必遵守）

- ⚠️ **绝对不要**把 `users.json` 提交到 Git 或公开（含密码哈希与签名密钥）。
- ⚠️ 签名密钥 `secret` 必须随机、且足够长（≥32 字符），泄露等于会话可被伪造。
- ⚠️ 公网暴露**必须**走 HTTPS，否则 Cookie 与密码可被中间人窃取。
- ⚠️ 登录接口的失败计数是**内存态**，重启即清零；对更强防护可接入 Fail2ban 读日志封 IP。

---

## 六、 部署至服务器实施备忘

1. **新建项目文件**：在本地完成开发调试，或在服务器 `/opt/workbuddy-manager` 放置代码。
2. **开放反代或端口**：
   - 可以在 1Panel 创建反代站点 `wb.example.com` 指向 `127.0.0.1:7864`。
   - 开启 HTTPS，直接通过浏览器进行手机扫码与号池管理。
3. **接入分销 (sub2api)**：
   - 后台通过 WebUI 纳管所有腾讯账号后，底层由 `workbuddy2api` 负责轮询并发，上层由 sub2api 负责给下游发放 Key 和计算 Token 计费。

---

## 七、 前端页面骨架（单文件，可直接新建 `index.html`）

> 使用 CDN 引入 Vue 3 + Tailwind + QRCode，无需 npm 构建，双击即可在浏览器预览。后端只需把此文件作为静态页面返回即可。

```html
<!DOCTYPE html>
<html lang="zh-CN" class="light">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>WorkBuddy Manager</title>

  <!-- LDC 风格：Inter + Noto Sans SC -->
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Noto+Sans+SC:wght@300;400;500;600;700&display=swap" rel="stylesheet">

  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/qrcodejs@1.0.0/qrcode.min.js"></script>

  <style>
    /* ── LDC 设计令牌（取自 linux-do/cdk globals.css）── */
    :root {
      --radius: 0.625rem;
      --background: #ffffff;
      --foreground: #18181b;          /* zinc-900 */
      --card: #ffffff;
      --muted: #f4f4f5;               /* zinc-100 */
      --muted-foreground: #71717a;    /* zinc-500 */
      --border: #e4e4e7;              /* zinc-200 */
      --primary: #18181b;
      --primary-foreground: #fafafa;
      --destructive: #dc2626;
      --sidebar: #f9fafb;             /* gray-50 */
    }
    .dark {
      --background: #18181b;
      --foreground: #fafafa;
      --card: #27272a;
      --muted: #3f3f46;               /* zinc-700 */
      --muted-foreground: #a1a1aa;
      --border: rgba(255,255,255,0.10);
      --primary: #e4e4e7;
      --primary-foreground: #18181b;
      --destructive: #ef4444;
      --sidebar: #1f2937;             /* gray-800 */
    }
    * { font-family: 'Inter', 'Noto Sans SC', system-ui, sans-serif; }
    body {
      background: var(--background);
      color: var(--foreground);
      -webkit-font-smoothing: antialiased;
    }
    .rounded-ldc { border-radius: 20px; }
    .card-ldc { background: var(--muted); border-radius: 20px; padding: 12px 14px; }
    .border-ldc { border: 1px solid var(--border); }
    .tabular { font-variant-numeric: tabular-nums; }
    * { transition: background-color .15s ease, border-color .15s ease; }
  </style>
</head>

<body>
  <div id="app">

    <!-- ═══ 顶部管理栏 ManagementBar（LDC 顶部横向导航）═══ -->
    <header class="border-b border-ldc sticky top-0 z-40 backdrop-blur"
            :style="{ background: 'color-mix(in srgb, var(--background) 88%, transparent)' }">
      <div class="mx-auto max-w-7xl px-4 sm:px-6 md:px-8 lg:px-12">
        <div class="flex h-14 items-center justify-between">

          <div class="flex items-center gap-2.5">
            <div class="h-7 w-7 rounded-md grid place-items-center text-sm"
                 :style="{ background: 'var(--primary)', color: 'var(--primary-foreground)' }">🤖</div>
            <span class="text-sm font-semibold">WorkBuddy Manager</span>
            <span class="text-[10px] px-1.5 py-0.5 rounded border border-ldc"
                  :style="{ color: 'var(--muted-foreground)' }">v1.0.0</span>
          </div>

          <nav class="hidden md:flex items-center gap-6 text-sm">
            <a href="#" class="font-medium" :style="{ color: 'var(--foreground)' }">账号</a>
            <a href="#" class="hover:opacity-80" :style="{ color: 'var(--muted-foreground)' }">用量</a>
            <a href="#" class="hover:opacity-80" :style="{ color: 'var(--muted-foreground)' }">设置</a>
          </nav>

          <div class="flex items-center gap-2">
            <button @click="toggleTheme"
                    class="h-8 w-8 grid place-items-center rounded-md border border-ldc hover:opacity-70"
                    :title="isDark ? '切换到亮色' : '切换到暗色'">
              {{ isDark ? '☀️' : '🌙' }}
            </button>
            <button @click="loadAccounts"
                    class="h-8 px-3 rounded-md border border-ldc text-xs hover:opacity-70">刷新</button>
            <button @click="openAdd"
                    class="h-8 px-3 rounded-md text-xs font-medium"
                    :style="{ background: 'var(--primary)', color: 'var(--primary-foreground)' }">
              + 添加账号
            </button>
          </div>
        </div>
      </div>
    </header>

    <!-- ═══ 主内容区（LDC 布局：max-w-7xl + 大边距）═══ -->
    <main class="mx-auto max-w-7xl px-4 sm:px-6 md:px-8 lg:px-12 py-8">

      <!-- ── 统计卡片四宫格 (LDC StatCard 范式) ── -->
      <section class="grid grid-cols-2 lg:grid-cols-4 gap-3 md:gap-4 mb-8">
        <div class="card-ldc min-h-[88px]">
          <div class="flex items-start justify-between">
            <div class="text-[11px] font-medium" :style="{ color: 'var(--muted-foreground)' }">账号总数</div>
            <div class="h-6 w-6 rounded-full grid place-items-center text-xs"
                 :style="{ background: 'var(--background)' }">👥</div>
          </div>
          <div class="mt-3 text-2xl font-semibold tracking-[-0.03em] tabular">{{ accounts.length }}</div>
          <div class="mt-2 text-[11px]" :style="{ color: 'var(--muted-foreground)' }">已纳管账号</div>
        </div>

        <div class="card-ldc min-h-[88px]">
          <div class="flex items-start justify-between">
            <div class="text-[11px] font-medium" :style="{ color: 'var(--muted-foreground)' }">有效期内</div>
            <div class="h-6 w-6 rounded-full grid place-items-center text-xs"
                 :style="{ background: 'var(--background)' }">✅</div>
          </div>
          <div class="mt-3 text-2xl font-semibold tracking-[-0.03em] tabular text-emerald-500">{{ validCount }}</div>
          <div class="mt-2 text-[11px]" :style="{ color: 'var(--muted-foreground)' }">Token 正常</div>
        </div>

        <div class="card-ldc min-h-[88px]">
          <div class="flex items-start justify-between">
            <div class="text-[11px] font-medium" :style="{ color: 'var(--muted-foreground)' }">即将过期</div>
            <div class="h-6 w-6 rounded-full grid place-items-center text-xs"
                 :style="{ background: 'var(--background)' }">⚠️</div>
          </div>
          <div class="mt-3 text-2xl font-semibold tracking-[-0.03em] tabular text-amber-500">{{ expiringCount }}</div>
          <div class="mt-2 text-[11px]" :style="{ color: 'var(--muted-foreground)' }">&lt;1h 需刷新</div>
        </div>

        <div class="card-ldc min-h-[88px]">
          <div class="flex items-start justify-between">
            <div class="text-[11px] font-medium" :style="{ color: 'var(--muted-foreground)' }">反代端点</div>
            <div class="h-6 w-6 rounded-full grid place-items-center text-xs"
                 :style="{ background: 'var(--background)' }">🔌</div>
          </div>
          <div class="mt-3 text-lg font-semibold tabular">:7863</div>
          <div class="mt-2 text-[11px]" :style="{ color: 'var(--muted-foreground)' }">OpenAI 兼容</div>
        </div>
      </section>

      <!-- ── 账号表格（LDC 偏表格，紧凑高信息密度）── -->
      <section class="rounded-ldc border border-ldc overflow-hidden">
        <div class="grid grid-cols-12 px-4 py-2.5 text-[11px] font-medium border-b border-ldc"
             :style="{ color: 'var(--muted-foreground)', background: 'var(--muted)' }">
          <div class="col-span-4">昵称</div>
          <div class="col-span-2">UID</div>
          <div class="col-span-2">状态</div>
          <div class="col-span-2">Token 有效期</div>
          <div class="col-span-2 text-right">操作</div>
        </div>

        <div v-for="a in accounts" :key="a.file"
             class="grid grid-cols-12 items-center px-4 py-3 border-b border-ldc last:border-b-0 hover:opacity-90">
          <div class="col-span-4 flex items-center gap-2.5 min-w-0">
            <div class="h-7 w-7 rounded-full grid place-items-center text-xs font-semibold flex-shrink-0"
                 :style="{ background: 'var(--primary)', color: 'var(--primary-foreground)' }">
              {{ (a.nickname || '?').charAt(0) }}
            </div>
            <span class="text-sm font-medium truncate">{{ a.nickname || '未命名' }}</span>
          </div>
          <div class="col-span-2 text-xs tabular" :style="{ color: 'var(--muted-foreground)' }">{{ a.uid }}</div>
          <div class="col-span-2">
            <span class="text-[11px] px-2 py-0.5 rounded-full border"
                  :class="a.is_expired ? 'text-red-500 border-red-500/30 bg-red-500/10'
                                       : 'text-emerald-600 border-emerald-500/30 bg-emerald-500/10'">
              {{ a.is_expired ? '● 已过期' : '● 在线' }}
            </span>
          </div>
          <div class="col-span-2 pr-4">
            <div class="text-[11px] mb-1 tabular" :style="{ color: 'var(--muted-foreground)' }">
              {{ fmtRemain(a.remain_seconds) }}
            </div>
            <div class="h-1.5 rounded-full overflow-hidden" :style="{ background: 'var(--border)' }">
              <div class="h-full rounded-full transition-all"
                   :style="{ width: Math.min(100, a.remain_seconds / 72) + '%',
                             background: a.is_expired ? 'var(--destructive)' : '#10b981' }"></div>
            </div>
          </div>
          <div class="col-span-2 flex justify-end gap-1">
            <button @click="checkin(a)" title="签到"
                    class="h-7 w-7 grid place-items-center rounded-md border border-ldc text-xs hover:opacity-70">🎁</button>
            <button @click="testCall(a)" title="测试调用"
                    class="h-7 w-7 grid place-items-center rounded-md border border-ldc text-xs hover:opacity-70">⚡</button>
            <button @click="del(a)" title="删除"
                    class="h-7 w-7 grid place-items-center rounded-md border border-ldc text-xs text-red-500 hover:opacity-70">🗑️</button>
          </div>
        </div>

        <!-- 空状态（LDC EmptyState 范式） -->
        <div v-if="!accounts.length" class="px-4 py-20 text-center">
          <div class="text-4xl mb-3 opacity-40">📭</div>
          <p class="text-sm font-medium">暂无账号</p>
          <p class="text-xs mt-1" :style="{ color: 'var(--muted-foreground)' }">
            点击右上角「+ 添加账号」扫码授权
          </p>
          <button @click="openAdd"
                  class="mt-5 h-9 px-4 rounded-md text-xs font-medium"
                  :style="{ background: 'var(--primary)', color: 'var(--primary-foreground)' }">
            + 添加账号
          </button>
        </div>
      </section>
    </main>

    <!-- ═══ 添加账号弹窗（LDC / shadcn Dialog 范式）═══ -->
    <div v-if="showAdd" class="fixed inset-0 z-50 grid place-items-center"
         :style="{ background: 'rgba(0,0,0,0.55)', backdropFilter: 'blur(3px)' }">
      <div class="w-[400px] rounded-ldc border border-ldc p-6 text-center"
           :style="{ background: 'var(--card)' }">

        <div class="flex items-start justify-between mb-1">
          <h3 class="text-base font-semibold">添加腾讯账号</h3>
          <button @click="closeAdd" class="text-sm opacity-50 hover:opacity-100">✕</button>
        </div>
        <p class="text-xs mb-5" :style="{ color: 'var(--muted-foreground)' }">
          使用微信 / QQ 扫码完成授权
        </p>

        <div v-if="qrState === 'loading'" class="py-16 text-xs"
             :style="{ color: 'var(--muted-foreground)' }">正在生成二维码...</div>
        <div v-else class="inline-block rounded-lg p-4 bg-white">
          <div id="qrcode"></div>
        </div>

        <p class="text-[11px] mt-4 break-all cursor-pointer text-blue-500 hover:underline"
           @click="openUrl">{{ authUrl }}</p>

        <p class="text-xs mt-3"
           :class="pollMsg.type === 'success' ? 'text-emerald-500'
                 : pollMsg.type === 'error' ? 'text-red-500' : ''"
           :style="pollMsg.type === 'wait' ? { color: 'var(--muted-foreground)' } : {}">
          {{ pollMsg.text }}
        </p>

        <button @click="closeAdd"
                class="mt-5 w-full h-9 rounded-md border border-ldc text-xs hover:opacity-70">
          取消
        </button>
      </div>
    </div>

    <!-- 轻量 Toast（LDC 用 Sonner，这里做等价简化） -->
    <div v-if="toast.show" class="fixed bottom-6 right-6 z-[60] px-4 py-3 rounded-lg border border-ldc shadow-lg text-sm"
         :style="{ background: 'var(--card)' }">
      <span :class="toast.ok ? 'text-emerald-500' : 'text-red-500'">{{ toast.ok ? '✓' : '✕' }}</span>
      <span class="ml-2">{{ toast.text }}</span>
    </div>

  </div>

  <script>
    const { createApp, ref, computed, onMounted, nextTick } = Vue;
    const API = '/api';

    createApp({
      setup() {
        const accounts = ref([]);
        const showAdd = ref(false);
        const authUrl = ref('');
        const qrState = ref('idle');
        const pollMsg = ref({ type: 'wait', text: '' });
        const isDark = ref(false);
        const toast = ref({ show: false, ok: true, text: '' });
        let pollTimer = null, currentState = '';

        const validCount = computed(() => accounts.value.filter(a => !a.is_expired).length);
        const expiringCount = computed(() => accounts.value.filter(a => a.remain_seconds < 3600 && a.remain_seconds > 0).length);

        function showToast(text, ok = true) {
          toast.value = { show: true, ok, text };
          setTimeout(() => { toast.value.show = false; }, 3000);
        }

        function fmtRemain(s) {
          if (s <= 0) return '已过期';
          if (s < 3600) return Math.floor(s / 60) + ' 分钟';
          if (s < 86400) return (s / 3600).toFixed(1) + ' 小时';
          return (s / 86400).toFixed(1) + ' 天';
        }

        function toggleTheme() {
          isDark.value = !isDark.value;
          document.documentElement.classList.toggle('dark', isDark.value);
          localStorage.setItem('wb-theme', isDark.value ? 'dark' : 'light');
        }

        async function loadAccounts() {
          try {
            const r = await fetch(API + '/accounts');
            const d = await r.json();
            accounts.value = d.accounts || [];
          } catch (e) { showToast('加载失败：' + e.message, false); }
        }

        function openUrl() { window.open(authUrl.value, '_blank'); }

        async function openAdd() {
          showAdd.value = true;
          qrState.value = 'loading';
          pollMsg.value = { type: 'wait', text: '' };
          try {
            const r = await fetch(API + '/auth/start', { method: 'POST' });
            const d = await r.json();
            if (d.error) { pollMsg.value = { type: 'error', text: d.error }; return; }
            authUrl.value = d.authUrl;
            currentState = d.state;
            qrState.value = 'ready';
            await nextTick();
            const el = document.getElementById('qrcode');
            el.innerHTML = '';
            new QRCode(el, { text: d.authUrl, width: 200, height: 200 });
            pollMsg.value = { type: 'wait', text: '⏳ 等待手机扫码确认...' };
            startPoll();
          } catch (e) { pollMsg.value = { type: 'error', text: '生成失败：' + e.message }; }
        }

        function startPoll() {
          clearInterval(pollTimer);
          pollTimer = setInterval(async () => {
            try {
              const r = await fetch(API + '/auth/poll?state=' + encodeURIComponent(currentState));
              const d = await r.json();
              if (d.status === 'success') {
                clearInterval(pollTimer);
                pollMsg.value = { type: 'success', text: '🎉 账号 [' + d.nickname + '] 授权成功！' };
                showToast('账号 [' + d.nickname + '] 授权成功', true);
                setTimeout(() => { closeAdd(); loadAccounts(); }, 1500);
              } else if (d.status === 'expired' || d.status === 'invalid') {
                clearInterval(pollTimer);
                pollMsg.value = { type: 'error', text: '二维码已失效，请关闭重试' };
              }
            } catch (e) { /* 忽略单次轮询错误 */ }
          }, 2000);
        }

        function closeAdd() {
          clearInterval(pollTimer);
          showAdd.value = false;
          qrState.value = 'idle';
        }

        async function checkin(a) {
          const r = await fetch(API + '/accounts/' + a.file + '/checkin', { method: 'POST' });
          const d = await r.json();
          showToast(d.message || '操作完成', d.code === 0 || d.code === 10001);
        }

        async function testCall(a) {
          showToast('测试调用中...', true);
          const r = await fetch(API + '/accounts/' + a.file + '/test', { method: 'POST' });
          const d = await r.json();
          showToast(d.message || '测试完成', !!d.ok);
        }

        async function del(a) {
          if (!confirm('确认删除账号 ' + (a.nickname || a.uid) + ' ？')) return;
          await fetch(API + '/accounts/' + a.file, { method: 'DELETE' });
          showToast('已删除', true);
          loadAccounts();
        }

        onMounted(() => {
          const saved = localStorage.getItem('wb-theme');
          if (saved === 'dark' || (!saved && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
            isDark.value = true;
            document.documentElement.classList.add('dark');
          }
          loadAccounts();
        });

        return { accounts, showAdd, authUrl, qrState, pollMsg, isDark, toast,
                 validCount, expiringCount, fmtRemain, toggleTheme, loadAccounts,
                 openAdd, closeAdd, checkin, testCall, del, openUrl };
      }
    }).mount('#app');
  </script>
</body>
</html>
```

---

## 八、 启动与运维命令速查

### 1. 本地/服务器启动 WebUI 后端
```bash
cd /opt/workbuddy-manager
pip3 install fastapi uvicorn --break-system-packages
# 前台调试运行（7864 端口）
uvicorn server:app --host 0.0.0.0 --port 7864
```

### 2. 注册为 systemd 常驻服务（开机自启）
新建 `/etc/systemd/system/workbuddy-web.service`：
```ini
[Unit]
Description=WorkBuddy Manager WebUI
After=network.target docker.service

[Service]
Type=simple
WorkingDirectory=/opt/workbuddy-manager
ExecStart=/usr/bin/python3 -m uvicorn server:app --host 0.0.0.0 --port 7864
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```
启用：
```bash
systemctl daemon-reload
systemctl enable --now workbuddy-web
systemctl status workbuddy-web
```

### 3. 常用排查命令
```bash
# 查看 WebUI 日志
journalctl -u workbuddy-web -f

# 查看底层代理容器
docker logs workbuddy2api --tail 50

# 查看当前账号数
curl -s http://127.0.0.1:7863/status -H "Authorization: Bearer 请在此填写你的上游APIKey"

# 重启底层代理
docker restart workbuddy2api
```

---

## 九、 开发实施清单（Checklist）

### 第一阶段：跑通基础功能
- [ ] 1. 创建 `/opt/workbuddy-manager/` 目录
- [ ] 2. 新建 `server.py`（复制第五节伪代码，补全 import 与静态文件挂载）
- [ ] 3. 新建 `index.html`（复制第七节骨架，**LDC 风格已内置**）
- [ ] 4. 本地用 `uvicorn server:app --port 7864` 跑通，浏览器访问 `http://localhost:7864`
- [ ] 5. 实测扫码流程：点【添加账号】→ 手机扫码 → 确认自动落盘 + 重启

### 第二阶段：鉴权与多用户（像 AGM 一样对外开放）
- [ ] 6. 生成 `users.json`（用第五·五节的 `make_hash` 工具造密码哈希；`secret` 改随机串）
- [ ] 7. 把第五·五节的鉴权代码追加进 `server.py`，给各业务接口挂 `Depends(current_user/require_admin)`
- [ ] 8. 前端加登录门禁（第五·五节第 4 小节），`viewer` 角色隐藏删除/添加按钮
- [ ] 9. 确认 `/api/login`、`/api/logout`、`/api/healthz` **未挂**鉴权依赖
- [ ] 10. 自测：未登录访问 `/api/accounts` 应返回 401；`viewer` 调删除应返回 403

### 第三阶段：上线与加固
- [ ] 11. 注册 systemd 服务常驻（第八节）
- [ ] 12. 1Panel 建反代 `wb.example.com` → `127.0.0.1:7864`，申请 SSL + 强制 HTTPS
- [ ] 13. （可选）1Panel 配 IP 白名单 / Cloudflare Access 加固
- [ ] 14. （可选）接入 sub2api：Base URL `http://172.17.0.1:7863`，Key 用上游 api_key（见上游 config.json）
- [ ] 15. （可选·进阶）升级为 **Next.js 15 + shadcn/ui (new-york) + Sonner + motion** 以 1:1 复刻 LDC 组件

> **开发提示 1**：`server.py` 里需给 FastAPI 挂载静态文件，把 `index.html` 作为根路径返回：
> ```python
> from fastapi.staticfiles import StaticFiles
> app.mount("/", StaticFiles(directory="/opt/workbuddy-manager", html=True), name="static")
> ```
> （注意：`mount("/")` 必须放在所有 `@app.get("/api/...")` 路由注册**之后**，否则会拦截 API。）
>
> **开发提示 2**：第五节代码已包含 `/api/accounts/{file}/test`（⚡ 测试调用）与 `/api/accounts/{file}/checkin`（🎁 签到）两个端点，前端骨架的按钮均有对应后端。
>
> **开发提示 3**：LDC 原项目是 **Next.js 15 + shadcn/ui**（React 生态）。本方案用「Vue3 CDN + LDC 设计令牌」是为了**免构建、快速出成果**；两者视觉一致，仅交互库不同。
>
> **开发提示 4**：鉴权 Cookie 走 HTTPS 时建议加 `secure=True`。若由 1Panel 反代终止 TLS 且未透传协议，可用 `request.headers.get("x-forwarded-proto") == "https"` 判断后再决定是否设置 `secure`。


