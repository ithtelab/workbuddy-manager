<div align="center">

# WorkBuddy Manager

**腾讯 CodeBuddy 账号池管理控制台 · OpenAI 兼容反代网关**

一套给 [`workbuddy2api`](https://github.com/Sliverkiss/workbuddy2api) 配套的 Web 管理端：
扫码批量纳管账号、自动签到、密钥分发、IP 管控、调用日志与用量统计，一个面板全搞定。

> 上游 workbuddy2api 的源码**随本项目的发布包一起分发**（MIT）。
> 已部署的不受影响；重装 / 迁移时怎么取得源码，见
> [部署指南的开头一节](deploy/README.md#〇上游源码从哪来随发布包分发)。

![Next.js](https://img.shields.io/badge/Next.js-15-000000?logo=nextdotjs&logoColor=white)
![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=white)
![TypeScript](https://img.shields.io/badge/TypeScript-5-3178C6?logo=typescript&logoColor=white)
![Tailwind CSS](https://img.shields.io/badge/Tailwind_CSS-4-06B6D4?logo=tailwindcss&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-22c55e)

[![Release](https://img.shields.io/github/v/release/ithtelab/workbuddy-manager?color=22c55e&label=Release)](https://github.com/ithtelab/workbuddy-manager/releases)
[![Changelog](https://img.shields.io/badge/更新日志-CHANGELOG-blue)](CHANGELOG.md)
[![Issues](https://img.shields.io/github/issues/ithtelab/workbuddy-manager?color=f59e0b&label=反馈)](https://github.com/ithtelab/workbuddy-manager/issues)
[![LINUX DO](https://img.shields.io/badge/社区-LINUX%20DO-1f6feb)](https://linux.do)

[English](README.en.md) · **简体中文**

本项目在 [**LINUX DO**](https://linux.do) 社区发布与交流，欢迎佬友来玩。

<img src="docs/images/dashboard.png" alt="WorkBuddy Manager 仪表盘" width="100%" />

</div>

---

## 这是什么

`workbuddy2api` 是一个把腾讯 CodeBuddy 账号池包装成 OpenAI 兼容接口的反代服务（Go 编写）。它的能力很完整，但只有命令行：加账号要跑脚本、看状态要 `curl /status`、发密钥没有界面。

本项目补上这一块 —— 一个可以公网运营的 Web 控制台：

| 你原本要做的 | 现在在面板上 |
|---|---|
| 服务器上跑 `login.sh` 扫码加号 | 点「添加账号」扫码，自动签到并纳管 |
| `curl /status` 看哪个号挂了 | 仪表盘实时展示健康度、冷却、有效期 |
| 手动改 `config.json` 调签到/并发 | 中文可视化设置，开关 + 数字框 |
| 所有下游共用一个全局 Key | 多密钥分发，各自独立配额、IP 与模型白名单 |
| 无法知道谁用了多少 | 每次调用的模型、Token、延迟、来源 IP 全量留痕 |
| 无任何 IP 防护 | 入站白/黑名单 + 每密钥 IP 上限与白名单 |

**与 workbuddy2api 的关系**：本项目是为它做**可视化**的配套项目 —— 让能力强大的
上游网关变得看得见、管得动。面板不侵入上游：**没有修改它一行代码**，账号轮询、
并发与熔断仍由它负责。两者配合的方式很自然：

- **上游负责能力，面板负责呈现**：账号调度、令牌刷新、限流熔断由 workbuddy2api
  完成；面板把这些能力可视化，并补上密钥分发、IP 管控、用量统计这些运营环节
- **互为参照、一起演进**：上游新增能力时本项目跟随适配，面板侧发现的运营需求也
  会反哺上游。上游在它的 README 里把本项目列为「社区前端面板」之一，我们希望
  一起把这个生态做得更好用
- **上游专注自己的核心**：面板不要求上游为它改代码，让上游能保持精简

欢迎参与共建：面板与上游源码的问题、想法都提到
[本仓库](https://github.com/ithtelab/workbuddy-manager/issues)
（上游源码随本项目的发布包分发）。

---

## 功能一览

### 账号管理
- **扫码纳管** —— 微信 / QQ 扫码授权，成功后自动每日签到、写入授权文件、重载上游容器
- **Token 监控** —— 有效期进度条，即将过期（<1h）自动预警；一键手动签到、连通性探测、刷新令牌
  - 进度条旁标「**最后续期**」时间：剩余天数会被刷新重新拉满，单看天数容易读反
    （`7 天` 可能是刚续期，`60 天` 反而可能是从没刷新过），配合续期时间才判断得准
  - **自动续期**：有效期不足 3 天时后台自动向腾讯换取新令牌（上游只在保活时刻或
    该号正在被使用时才刷新，长期闲置的号会一路走到过期）。续期结果记在「任务」页
  - **刷新令牌**按钮会**真的续期**并保存，不只是重载上游；令牌已失效时会明确提示
    需要重新扫码或登录
- **运行时状态** —— 与上游账号池状态合并展示（在线 / 冷却中 / 已禁用 / 已过期）；
  **模型级限流**单独标记（账号仍在线、只是某个模型暂时受限，悬停看恢复时间）
- **积分余额** —— 显示各账号当前可花费积分并按余额分级着色；**直接向腾讯查询实时值**
  （上游 `/status` 的积分滞后可达数小时）：打开页面自动拉取、签到后即时更新，
  另有「刷新积分」按钮可手动刷新全部账号。每个数字旁标注 **`实时`** 或
  **`缓存 Ns 前`**，一眼看出是刚查的还是 60 秒内复用的缓存
- **积分到期倒计时** —— 积分按套餐分批过期、过期即作废，每笔各算各的到期时间。
  余额旁显示最近一笔的额度与倒计时（如 `300 · 8 天后到期`，按紧迫度分三档着色），
  悬停可看全部套餐的额度、具体到期时刻与合计；首页积分卡片同时提示最近一笔的金额
  与到期日期，避免攒着攒着就白白过期
- **临时停用** —— 某个号在拖后腿时先摘掉它，比删除更合适（删除会丢凭证、只能重新
  扫码）。停用期间**不被选中处理对话，但签到与令牌保活照常执行**，积分与凭证都是
  活的，随时可以启用回来（上游版本较旧时会自动回退到「完全退出账号池」的方式，
  并在提示里说明差别）
- **积分变动流水** —— 所有让余额增加的渠道都会留痕：上游只在旅行领奖时打日志，
  签到与活跃上报**根本不打**，因此改为每次查积分后比对余额、只要增加就记一条
  （如 `余额 +100（1300 → 1400）`），在「自动任务与积分记录」里按「积分变动」筛选查看
- **任务记录页**（底栏「任务」）—— 签到记录、上游原始日志、自动任务与积分收益
  集中在一处，不与账号列表挤在同一页；30 秒自动刷新。三块均**固定高度内滚**
  （与右侧原始日志一致）并支持**时间范围筛选**，页面高度恒定（手机上按视口自适应）。
  单次拉取 200 条，命中上限时会提示「仅显示最近 N 条」，需要更早记录就缩小时间范围
- **自动任务与积分记录** —— 猫猫旅行 / 活跃上报 / 自动签到 / 保活的执行结果与
  **积分收益**一目了然（如旅行领奖 `+100`、领养 Buddy `+300`），可按类型筛选并汇总累计积分。
  已适配上游签到健壮性改造后的日志：**「今日已签到」显示为正常**（幂等成功），
  每轮签到有汇总行（`共 4 个，成功 1，已签到 1，失败 1，跳过 1`），
  刷新/保存令牌失败也会记到对应账号上
  上游日志是英文，展示时已翻成中文（「领奖成功：第 12 次行程，获得 100 积分」），
  悬浮可看英文原文
  上游把这些结果只打在容器日志里、**容器重建即丢失**，因此由后台每 45 秒采集解析后
  落库长期保留；也可点「立即采集」随时拉取最新

### 反代网关（对外 `/v1`）
- **OpenAI 兼容** —— 下游用标准 SDK 直连，支持流式（SSE）与非流式
- **多密钥分发** —— 每把密钥独立设置**限定版本**（国内版 / 国际版）、有效期、最大 IP 数、IP 白名单、模型白名单、Token 配额
- **密钥安全** —— 库中仅存 SHA-256 哈希，明文只在创建时展示一次
- **模型别名映射** —— 把 `gpt-4o-mini` 之类映射到实际模型，方便下游无感迁移
- **国内版 / 国际版切换** —— 页面右上角一键切换（上游单实例双版本共存，共用账号池）：
  账号、模型、测试台、任务记录、请求日志与用量统计全部按版本过滤；「添加账号」跟随切换
  （国际版会走地区注册与一次性 trial）。**密钥也可限定版本**——国内版密钥只能调国内版
  模型，反之亦然
- **模型中心** —— 账号实际可用模型单列一页：显示名、描述、上下文、最大输出、
  **推理档位**、**积分倍率**、多模态/仅推理等能力标记，按系列分组，支持搜索、
  能力筛选与**按积分倍率排序**（挑省积分的模型时最实用）。数据直连腾讯模型接口
  （那里有比上游更全的字段）；取不到时回退上游清单并**如实标注来源**，绝不编造数据
- **国际版能力边界**（上游如此，非本端缺失）：无签到 / 猫猫旅行 / 开学季 / 夜猫，
  积分仅一次性 trial；**保活与活跃上报照常**。任务页对国际版会写明原因，
  不显示成空白
- **聊天测试台** —— 不建密钥直接试调模型（与下游同一账号池）：真实模型选择 +
  **思考强度**（接 `reasoning_effort`，只列该模型支持的档位）、流式输出可中断，
  **右下角实时显示本次消耗积分**（取自上游 `usage.credit`）。仅管理员可用
- **入站 IP 管控** —— 全局白/黑名单（支持 CIDR），白名单模式可做到只放行可信来源
- **全量审计** —— 每次调用记录密钥、IP、模型、状态码、**首字延迟**、总耗时、Token 消耗、**实际扣费**与**提示词缓存命中**
  （取自上游 `usage.credit`；上游未返回时显示 `—`，与「扣了 0」区分开）

### 可视化设置
- **定时任务**：签到 / 猫猫旅行 / 活跃上报 / 保活四类任务各自独立开关与执行时刻
  （整点数组，如 `9, 21`），附白话说明，不再手改 JSON
- **系统提示词**：`prompt.mode` 可在 `passthrough`（**默认**，透传客户端 system）
  与 `custom`（网关提示词替换客户端 system）之间切换——需要保留下游自带 system
  prompt 时保持默认即可；若依赖网关提示词稳定行为，或想从源头消除模板句误报，
  改为 `custom`。另可指定自定义提示词文件（仅 custom 模式生效）。该分组带显式风险警示
- **限流与冷却**：软限流冷却基数与退避上限（`600s` / `2h` 这类时长）
- **并发与熔断**：单账号并发、失败阈值、熔断冷却与封顶、闲置补偿权重、
  快过期积分窗口（到期在窗口内的积分优先消耗，留空或 0 = 关闭）
- **功能开关 / 会话粘性**：出站指纹脱敏、会话绑定时长与清理周期
- **可用模型**：从上游实时拉取，如实标注来源，并提供
  手动「重新拉取」（上游自身缓存 1 小时）。列表由上游随机挑一个账号拉取，
  **取决于该账号授权**，故不同账号可见的模型数量可能不同
- 输入即校验（时刻限 0-23 且去重排序、时长须为 `30s / 10m / 2h / 1d`），
  服务端二次兜底，非法值直接拒绝而不是写坏配置
- 只提交改动项，不会误覆盖未展示的配置；冷门参数保留「高级设置」直接编辑
- 读取失败时明确提示原因并锁定保存，**杜绝空配置覆盖真实文件**

### 移动端
- **任务记录页在手机上整合为单卡片**：顶部分段切换「签到记录 / 自动任务 / 原始日志」，
  一次只显示一块，避免三块竖排又长又碎；桌面端仍是三块并列
- **手机上可用**：账号列表与任务记录在窄屏自动改为纵向卡片（一屏看全昵称/UID/状态/积分/有效期/操作），
  桌面端仍是表格；设置页标签栏可横向滚动；各个表格在窄屏可左右滑动而不撑破页面
- 头部操作按钮在手机上自动收敛文案，底栏保持居中不动

### 界面与交互
- **浮动底栏**：可拖拽、带磁吸放大动效，**以中心为锚点**，悬停放大时不会左右漂移；弹窗打开时位置保持不动
- **语义化配色**：Token 有效期按剩余时间分四档——已过期（红）/ 即将过期（琥珀）/ 偏紧（蓝）/ 健康（绿），账号列表与仪表盘共用同一套规则
- **醒目提示**：操作结果以 Q 弹气泡呈现，带回弹动画、语义色描边与倒计时进度条，并有 🎉 / ⛔ / ⚠️ / 💡 图标区分
- 危险操作（删除账号、清空日志、退出登录等）一律二次确认；个人信息面板提示「点击空白处或按 Esc 关闭」
- 深浅色主题可切换，跟随系统

### 一键更新
- **网页上直接更新**，无需登录服务器：「设置 → 系统更新」
- **自动检测新版本**：管理端（Release）与上游（最新提交）有更新时主动提醒，
  并显示版本对比与上游最新提交说明
- 三种模式：**全部更新** / **仅上游**（workbuddy2api）/ **仅管理端**
- 实时进度与日志；账号授权、上游配置、密钥与日志数据全部保留
- 上游更新后自动重新施加端口收敛，避免安全基线被上游默认配置覆盖
- 仅管理员可用，更新目标为固定枚举（不接受客户端传入命令或路径）
- **发布包强制验签（供应链防护）**：更新器内置维护者公钥，先验签后解压；
  签名缺失/被篡改/不匹配都会拒绝安装。面板显示「已验签」标记。
  验签通过后 `deploy/` 也会随包更新（它里面的更新器本身也需要能升级）；
  若你要手工维护 `deploy/`，设 `WB_SYNC_DEPLOY=0` 即可保持不动。
  发版与密钥管理见 [docs/release-signing.md](docs/release-signing.md)

### 更新日志
- 内置「设置 → 更新日志」，读取部署目录的 `CHANGELOG.md`（`server/` 内另有副本兜底）
- **离线可用**：文件随发布包分发，不需要访问 GitHub
- 按版本折叠（默认展开最新），标记当前运行版本，未发布内容显示为「开发中」
- 分类带配色标签（安全 / 新增 / 修复 / 改进），无需引入 markdown 依赖
- 一键更新会同步根目录的 `CHANGELOG.md` 与 `README.md`；即便旧部署缺这两个文件，
  `server/` 里的副本也会被更新覆盖，界面照样能看到最新日志

### 访问控制
- 管理端用户名 + 密码登录，PBKDF2-SHA256 加盐存储
- **管理端身份只认签名 Cookie**（HMAC-SHA256）。网关的 API 密钥**不能**用于登录
  管理端——密钥只授权 `/v1` 的模型调用，与后台权限彻底分离
- 改密码 / 改角色 / 删用户会**立即吊销该用户的全部登录态**（其他用户不受影响）
- **审计日志**：登录成功与失败、改密码、增删用户均留痕（时间 / 操作者 / 来源 IP），
  在「安全」页可查
- 被入侵后的凭据清理：`python3 deploy/purge_credentials.py`（轮换会话密钥 +
  清空遗留 api_keys + 吊销全部会话，写盘前自动备份）
- 角色分级：`admin` 可读写，`viewer` 只读（适合给同事看状态）
- HttpOnly 签名 Cookie 会话，同 IP 连续失败 5 次锁定 10 分钟

---

## 界面预览

### 仪表盘
> 账号健康度、上游连接状态、近 14 天调用趋势

<img src="docs/images/dashboard.png" alt="仪表盘" width="100%" />

<details>
<summary><b>深色模式</b>（点击展开）</summary>

<img src="docs/images/dashboard-dark.png" alt="仪表盘 深色模式" width="100%" />

</details>

### 账号管理
> Token 有效期按剩余时间分档着色（已过期 / 即将过期 / 偏紧 / 健康）

<img src="docs/images/accounts.png" alt="账号管理" width="100%" />

<details>
<summary><b>扫码添加账号</b>（点击展开）</summary>

<img src="docs/images/add-account.png" alt="扫码添加账号" width="100%" />

</details>

### 任务记录
> 签到结果、上游原始日志、自动任务与积分收益集中一页（30 秒自动刷新）

<img src="docs/images/tasks.png" alt="任务记录" width="100%" />

### API 密钥
> 限定版本（国内版 / 国际版）、独立配额、IP 限制、模型白名单，明文仅创建时展示一次

<img src="docs/images/keys.png" alt="API 密钥" width="100%" />

### 请求日志
> 按时间 / 密钥 / 状态 / 模型 / IP 筛选，含**实际调用账号**、**首字延迟**、总耗时、Token 计量与**提示词缓存命中**

「首字」= 从发起上游请求到收到第一个含正文的 delta，反映**上游响应快慢**；
「总耗时」含模型生成全程，回答越长越大，用于看单次请求的整体开销。
非流式请求没有中间过程，首字显示 `—`。

**账号**是这次调用实际落在哪个上游号上，形如 `张叔叔(299e342b)`。账号由上游选择、
也不在响应里回传，本面板是读上游的容器日志再按时间对回去的，所以**比请求晚几秒**
才补上（刚打完的请求可能还是 `—`）；容器日志读不到时（上游不在本机、没挂
`docker.sock`、原生部署）这一列一直是 `—`，不影响其余功能。

Token 后面的缓存标记（绿色「缓存 N%」/ 琥珀色「未命中」）来自上游返回的用量数据，
用来判断同一段前缀**有没有真的吃到缓存**——命中部分计费便宜得多。前缀缓存按账号存，
所以「换了账号」与「没命中」常常一起出现，配合账号列一起看。上游不返回这项数据时
标记不显示（详情里写「未采集」），与「没有命中」不是一回事。

<img src="docs/images/logs.png" alt="请求日志" width="100%" />

### 用量统计
> 按天、按模型、按密钥多维统计 Token 消耗

<img src="docs/images/stats.png" alt="用量统计" width="100%" />

### 模型中心
> 账号可用模型一览：显示名、上下文、最大输出、推理档位与系列分组

数据直连腾讯模型接口，因此有**显示名**和**推理档位**（上游 `/v1/models` 会丢掉这两项）。
顶部统计卡与列表全部由真实数据计算；来源取不到时回退上游简表并如实标注，不编造字段。

<img src="docs/images/models.png" alt="模型中心" width="100%" />

### 聊天测试台
> 不建密钥直接试调模型；真实模型选择、思考强度与实时积分消耗

请求经管理端登录态转发到上游（与下游同一账号池），**仅管理员可用**——
试调会真实扣积分。右下角显示本次会话累计消耗，每条回答下标注该次扣费与 token 数。

<img src="docs/images/playground.png" alt="聊天测试台" width="100%" />

### 安全与 IP 管控
> 全局白/黑名单、CIDR 规则、访问审计与拦截记录

<img src="docs/images/security.png" alt="安全与 IP 管控" width="100%" />

### 设置
> 上游配置可视化，中文说明 + 开关 / 数字框

<img src="docs/images/settings.png" alt="设置" width="100%" />

---

## 架构

```
   下游客户端 / sub2api（OpenAI SDK）
              │  Authorization: Bearer wbk_xxx
              ▼
   ┌──────────────────────────────────────────────┐
   │  WorkBuddy Manager                     :7864 │
   │  ┌────────────────────────────────────────┐  │
   │  │ 反代网关  /v1  /v2  /healthz           │  │
   │  │  密钥鉴权 → IP 管控 → 模型映射          │  │
   │  │  → 流式转发 → 日志与用量落库(SQLite)    │  │
   │  ├────────────────────────────────────────┤  │
   │  │ 管理接口  /api/*                       │  │
   │  │  登录 / 账号 / 密钥 / 日志 / 用量        │  │
   │  │  / 安全 / 设置                          │  │
   │  ├────────────────────────────────────────┤  │
   │  │ Web 前端（Next.js 静态导出）            │  │
   │  └────────────────────────────────────────┘  │
   └───────────────┬──────────────────────────────┘
                   │ 复用 auths/*.json  调用 /status /v1/models
                   ▼
   ┌──────────────────────────────────────────────┐
   │  workbuddy2api（Go，不改动）            :7863 │
   │  账号轮询 · 并发调度 · 熔断 · 令牌刷新          │
   └───────────────┬──────────────────────────────┘
                   ▼
        腾讯 CodeBuddy / copilot.tencent.com
```

**单进程单端口**：前端由 `next build` 静态导出，交由 FastAPI 托管，`/api` 与 `/v1` 同源，无需 CORS。

**技术栈**

| 层 | 选型 |
|---|---|
| 前端 | Next.js 15（App Router）· React 19 · TypeScript · shadcn/ui · Tailwind CSS v4 · motion · recharts · sonner |
| 后端 | Python 3.11+ · FastAPI · uvicorn · httpx · SQLite（标准库，无重依赖） |
| 部署 | systemd 常驻 + 1Panel 反向代理 + Let's Encrypt HTTPS |

---

## 快速开始

### 一、本地开发

```bash
# 0) 可选：本机没有真实的 workbuddy2api 时，起一个模拟上游
#    自带模型列表与示例账号，便于查看完整界面
python dev/mock_upstream.py         # 监听 127.0.0.1:7863

# 1) 后端（终端 A）
python -m pip install -r server/requirements.txt
WB_ADMIN_PASSWORD=admin123 \
WB_DATA_DIR=./data \
WB_AUTH_DIR=/opt/workbuddy2api/auths \
WB2API_BASE=http://127.0.0.1:7863 \
python -m uvicorn server.main:app --reload --port 7864

# 2) 前端（终端 B）—— next dev 会把 /api、/v1 反代到 :7864
cd web
npm install
npm run dev                          # http://localhost:3000
```

首次启动会自动生成 `users.json` 与随机签名密钥。若未设置 `WB_ADMIN_PASSWORD`，会在日志中打印一次随机管理员密码。

> **代理注意事项**
> 若本机装有代理软件（Clash / V2Ray 等），**尤其是 TUN 模式**，访问 `127.0.0.1:7863` 可能被代理劫持，表现为接口长时间无响应。
> 本项目对内部请求默认 `trust_env=False`（不读取系统代理）；确需走代理时设置 `WB_HTTP_PROXY`。
> TUN 模式下请在代理软件中把 `127.0.0.1` 加入直连 / 绕过列表。

#### 运行测试

```bash
# 配置读写的回归测试：时刻数组 / 时长字符串 / 部分提交不覆盖同段其他键
python -m unittest discover -s server/tests -t . -v
```

> 测试用临时目录模拟上游 `config.json`，不触碰真实配置，可安全反复运行。
> 这一组测试专门守住两个曾经写坏配置的坑：把整点数组当成数字间隔、
> 把时长字符串当成秒数。

### 二、Windows 原生部署（无需 Docker）

前提：`workbuddy2api` 已在本机运行，并有可用的启停脚本与文件日志。

```powershell
git clone https://github.com/ithtelab/workbuddy-manager.git
cd workbuddy-manager
Copy-Item .env.example .env
```

在 `.env` 中至少设置以下项目（Windows 路径建议使用 `/`）：

```dotenv
WB_MANAGER_HOST=127.0.0.1
WB_SECURE_COOKIE=false
WB2API_MODE=native
WB_UPSTREAM_DIR=C:/path/to/workbuddy2api
WB_AUTH_DIR=C:/path/to/workbuddy2api/auths
WB_UPSTREAM_CONFIG=C:/path/to/workbuddy2api/config.json
WB2API_START_SCRIPT=C:/path/to/workbuddy2api/start-workbuddy2api.cmd
WB2API_STOP_SCRIPT=C:/path/to/workbuddy2api/stop-workbuddy2api.cmd
WB2API_LOG_FILE=C:/path/to/workbuddy2api/data/server.err.log
```

安装依赖、构建前端并后台启动：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r server\requirements.txt
Set-Location web
npm ci
npm run build:export
Set-Location ..
powershell -ExecutionPolicy Bypass -File .\service-tools.ps1 start
```

用 `service-tools.ps1 status|restart|stop` 管理后台进程。Windows 原生模式支持
保存配置后重启上游及读取上游日志；网页一键更新依赖 Linux/Docker，当前会明确拒绝，
请手动更新代码后重启服务。

> **启停脚本优先用上游自带的**：上游 `workbuddy2api` 2026-09-18 起自带
> `start/stop/status-workbuddy2api.cmd`（PID 文件 + 进程路径校验，不会误杀同名进程），
> 直接把 `WB2API_START_SCRIPT` / `WB2API_STOP_SCRIPT` 指过去即可。若你的上游目录里
> 没有这三个文件（旧版上游），`deploy/windows-native/` 下有一对可直接改用的模板。

> **两个约定必须满足**（上游脚本已满足）：启动脚本要**立即返回**（前台运行会让
> 「重启」等到超时才报失败），日志要写到 `WB2API_LOG_FILE`（否则「任务记录」读不到
> 自动任务日志）。细节见 `deploy/windows-native/README.md`。

### 三、Docker 部署

仓库自带 `Dockerfile` 与 `docker-compose.yml`，适合已经用 Docker 跑上游的用户：

```bash
git clone https://github.com/ithtelab/workbuddy-manager.git
cd workbuddy-manager
# 按需改 compose 里的 WB2API_BASE 与卷路径（默认假设上游在 ../workbuddy2api）
docker compose up -d --build
docker compose logs workbuddy-manager | grep -A2 密码   # 首启随机密码
```

> **前端会在镜像里自动构建**：`web/out`（前端产物）不入库，所以 `git clone` 得到的
> 工作区里没有它。构建时若发现没有，就自动在容器内 `npm ci && next build`
> （约 1-2 分钟，首次会拉取 Node 镜像）；若已有（例如从 Release 包解压出来的），
> 则直接复用、跳过这一步。两条路都不需要你事先装 Node 或手动构建。
>
> 国内网络下 npm 官方源可能很慢，可加镜像：
> `docker compose build --build-arg NPM_REGISTRY=https://registry.npmmirror.com`

也可以直接用构建好的镜像（每次发版会推到 GHCR）：

```bash
docker pull ghcr.io/ithtelab/workbuddy-manager:latest
```

> 镜像**同时提供 `linux/amd64` 与 `linux/arm64`**（Apple Silicon、ARM 云主机可直接拉取，
> 无需 QEMU 模拟）。`docker pull` 会按你的机器架构自动选择对应的那一份。

**想用自己构建的镜像？fork 一下就行** —— 上面那份是维护者发版时构建的。如果你要
改点什么再自己用（换默认配置、加个依赖，或者只是不想依赖别人的镜像仓库），fork
本仓库后把 fork 专用工作流放进 `.github/workflows/`，推一次代码就自动构建：

```bash
mkdir -p .github/workflows
cp deploy/fork-image/build-image.yml .github/workflows/
git add .github/workflows/build-image.yml && git commit -m "ci: 构建自己的镜像" && git push
```

工作流**不用改任何内容**：镜像归属、触发分支、镜像里记的来源信息都按你 fork 的实际
情况自动决定（谁 fork 就推到谁名下，默认分支改了名也照常触发）。构建完成后（几分钟）
拉取你自己那一份，真实用户名见那次运行的摘要：

```bash
docker pull ghcr.io/<你的用户名>/workbuddy-manager-multiarch:latest
```

> 镜像名比上游多一个 `-multiarch` 后缀，这**不是笔误**：`workbuddy-manager` 这个
> 包名可能已被一个未链接到本仓库的同名包占用，那种包 fork 拿不到写权限、推送会
> 失败。也可以顺便推一份到 Docker Hub（加两个 Secret 即自动启用）。完整说明见
> [deploy/fork-image/README.md](deploy/fork-image/README.md)。

**容器版与宿主版的能力是一致的** —— compose 里默认挂载了三样东西让它们对齐：

| 挂载 | 作用 |
|---|---|
| 上游仓库目录 | 读上游 compose 做端口收敛；`git pull` 更新上游；读写 `config.json` 与 `auths/`（**扫码添加账号会写 auths**，所以不能只读） |
| `./data` | 数据库、日志、更新状态。必须持久化 |
| `/var/run/docker.sock` | 让容器内的管理端能重启/重建上游容器 —— 即「更新上游」「保存设置后自动重载」「读上游日志」 |

> **关于 docker.sock 的取舍**：挂它等于把宿主 root 权限交给本容器。但这**不是新增的风险等级**——宿主部署时本服务本来就是 root 运行（systemd 单元无 `User=`、安装脚本要求 root），而 root 进程本来就能 `docker run -v /:/host` 拿到宿主文件系统，两者权限等价。
> 若你的要求是最小权限，把那一行注释掉即可：依赖 docker 的功能会**自动降级为「请到宿主机操作」**，界面如实提示，不会静默失败。

还有两处与宿主部署的差异（界面都会提示）：

- **更新管理端会重启整个容器**：容器无法自我重启。流程是「替换代码 → 结束容器 → 由 compose 的 `restart` 策略用新代码拉起」，所以 compose 里必须保留 `restart: unless-stopped`。
- **端口默认只绑定 `127.0.0.1`**：管理端持有全部账号凭据，应当藏在反向代理之后。确需直接访问请自行改 compose，并确保 HTTPS。

> 与宿主机安装一样，一键更新**强制验签**发布包。镜像本身不参与这套签名
> （那是另一条信任链，依赖 GHCR 的 digest 与 GitHub 账号安全）。

### 四、部署到服务器（一键脚本）

本项目依赖上游 workbuddy2api（账号池与 OpenAI 兼容接口），**单独 clone 本仓库无法运行**。
**发布包里已自带上游源码**（源码由本项目随包分发），
一键脚本会装好两者；要改用自己那份源码见
[部署指南](deploy/README.md#〇上游源码从哪来随发布包分发)：

```bash
# 推荐：用 Release 包（内含已构建的前端，无需 Node.js）
wget https://github.com/ithtelab/workbuddy-manager/releases/latest/download/workbuddy-manager-<版本>.tar.gz
tar xzf workbuddy-manager-*.tar.gz && cd workbuddy-manager-*

sudo bash deploy/install.sh
```

脚本自动完成：

1. 环境预检（Python / Docker / 端口）
2. **安装上游 workbuddy2api** —— 克隆、生成随机 `api_key`、修正目录属主、
   构建并启动容器、等待就绪
3. 安装管理端 —— 部署代码、装依赖、注册 systemd 服务
4. 验证并打印访问地址与初始密码

**全程无需手工编辑配置。** 若已自备上游，加 `--skip-upstream` 即可跳过，
脚本不会改动已有配置与账号。

> 通过 `git clone` 部署时**不需要**手动构建前端：安装脚本会发现缺少
> `web/out`（前端产物不入库）并自动 `npm ci && npm run build:export`
> （需机器上有 Node.js；没有则提示改用 Release 包 —— 那里面已含构建好的产物）。

首次启动的管理员密码：

```bash
journalctl -u workbuddy-web | grep -A3 '初始管理员'
```

**公网访问请务必配置 HTTPS 反向代理**（否则会话 Cookie 与密码可被窃听）。
1Panel 用户：网站 → 创建反向代理 → 目标 `http://127.0.0.1:7864` →
申请 Let's Encrypt 证书 → 开启强制 HTTPS。
完整部署说明（含 Nginx 配置、加固建议、常见问题）见
[deploy/README.md](deploy/README.md)。

<details>
<summary><b>环境变量一览</b></summary>

| 变量 | 默认 | 说明 |
|---|---|---|
| `WB_MANAGER_PORT` | `7864` | 监听端口 |
| `WB2API_BASE` | `http://127.0.0.1:7863` | workbuddy2api 地址 |
| `WB2API_KEY` | 读 config.json | 上游 API Key |
| `WB2API_MODE` | `docker` | 上游运行方式：`docker` 或 `native` |
| `WB2API_CONTAINER` | `workbuddy2api` | 重载用的容器名 |
| `WB_AUTH_DIR` | `/opt/workbuddy2api/auths` | 账号授权目录 |
| `WB_UPSTREAM_CONFIG` | `/opt/workbuddy2api/config.json` | 上游配置文件 |
| `WB2API_START_SCRIPT` | 上游目录下的 `.cmd` | native 模式启动脚本 |
| `WB2API_STOP_SCRIPT` | 上游目录下的 `.cmd` | native 模式停止脚本 |
| `WB2API_LOG_FILE` | `data/server.err.log` | native 模式上游日志 |
| `WB_DATA_DIR` | `./data` | 本服务数据目录 |
| `WB_STATIC_DIR` | `./web/out` | 静态导出目录 |
| `WB_ADMIN_PASSWORD` | 随机生成 | 首次启动的 admin 密码 |
| `WB_SECURE_COOKIE` | `auto` | 会话 Cookie 的 `Secure` 标志：`auto` 依 `X-Forwarded-Proto` 判定、也可写死 `true`/`false` |
| `WB_SESSION_DAYS` | `1` | 会话**总时长**上限（天），到点必须重新登录 |
| `WB_SESSION_IDLE_HOURS` | `12` | 会话**空闲**上限（小时）：多久没操作就失效（滑动续期窗口） |
| `WB_GATEWAY_RATE_PER_MIN` | `120` | 对外网关**每密钥**限流：60 秒内放行的请求数上限（`0` = 不限制） |
| `WB_GATEWAY_MAX_BODY_MB` | `32` | 对外网关的请求体上限（MB） |
| `WB_SYNC_DEPLOY` | `1` | 更新管理端时是否让 `deploy/` 一起更新（`0` = 保持本地不动） |
| `WB_HTTP_PROXY` | 空 | 出口代理，留空 = 全部直连 |

完整清单见 [`.env.example`](.env.example)。

</details>

---

## 使用指南

### 1. 纳管账号

登录后进入「账号」页，点右上角 **添加账号** → 用微信 / QQ 扫码 → 授权成功后自动签到、落盘并重载上游容器。

### 2. 分发密钥

进入「密钥」页点 **新建密钥**，按需设置：

- **限定版本** —— 国内版密钥只能调国内版模型，国际版密钥只能调 `global:` 开头的
  国际版模型，跨版本调用会被拒绝（`/v1/models` 也只返回对应版本的模型）。
  默认跟随当前所在版本；选「不限制」则两版都能调
- **有效期** —— 留空或 0 表示永不过期
- **最大 IP 数** —— 限制同一密钥可使用的来源 IP 数量
- **IP 白名单** —— 更严格，仅允许指定 IP / CIDR 调用
- **模型白名单** —— 在所选版本之内再限制到具体模型
- **Token 配额** —— Token 用尽后自动拒绝（按 prompt + completion 累计）
- **积分配额** —— 按**真实扣费**累计的额度，用尽后同样拒绝（429）。

  为什么除了 Token 还要有积分：**同样 1M Token，便宜模型与贵模型的扣费能差
  几十倍**，拿 Token 数当预算估不出实际花了多少。数据来自上游每次响应末帧
  `usage.credit`（真实扣费，不是估算），与「用量统计」页看到的是同一份。

  两个额度**各自独立**，任一超限即拒绝，都留 0 = 不限。上游没返回该字段的调用
  **不计入**（而不是按 0 记）：那代表「不知道扣了多少」，按 0 记等于把它当免费，
  数字会假装准确。代价是老版本上游（2026-09-13 之前）下这个额度**不会增长**，
  界面上用量一直是 0 —— 那种情况下请用 Token 配额。

密钥明文**只在创建时展示一次**，请立即保存。

### 3. 下游接入

完全兼容 OpenAI 协议，Base URL 指向本服务的 `/v1`：

```bash
curl https://wb.example.com/v1/chat/completions \
  -H "Authorization: Bearer wbk_xxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5.2",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": true
  }'
```

Python 示例：

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://wb.example.com/v1",
    api_key="wbk_xxxxxxxx",
)

resp = client.chat.completions.create(
    model="glm-5.2",
    messages=[{"role": "user", "content": "你好"}],
    stream=True,
)
for chunk in resp:
    print(chunk.choices[0].delta.content or "", end="")
```

> 流式请求会自动注入 `stream_options.include_usage=true`，以便精确统计 Token 消耗。

<details>
<summary><b>OpenAI Responses API（Codex / DeepSeek Harness 等）</b></summary>

本服务同时提供 Responses 协议（`/v1/responses`，SDK 的 `baseURL` 不含 `/v1`
时也支持 `/responses`）。请求体用 `input` 而非 `messages`，`instructions` 承载
system 文本：

```python
from openai import OpenAI

client = OpenAI(base_url="https://wb.example.com/v1", api_key="wbk_xxxxxxxx")

resp = client.responses.create(
    model="glm-5.2",
    instructions="你是一个简洁的助手",
    input="你好",
    stream=True,
)
for event in resp:
    if event.type == "response.output_text.delta":
        print(event.delta, end="")
```

工具调用同样支持：客户端发扁平形状的 `tools`（`{type:"function", name, parameters}`），
返回的是 `function_call` 输出项与 `response.function_call_arguments.delta` 事件。

> 只发 `openai-responses` 协议的客户端（如 DeepSeek Harness 的自定义提供方）
> 把「API 协议」选成 `openai-responses` 即可；它谈的协议与
> `openai-completions` 不是同一个，需要单独建一个提供方。

</details>

<details>
<summary><b>Anthropic Messages API（Claude Code / Cursor / Cline 等）</b></summary>

只认 Anthropic 协议的客户端可直接把 Base URL 指向本服务——`ANTHROPIC_BASE_URL`
配到根路径即可（协议层面 `/v1/messages` 与官方一致）：

```bash
export ANTHROPIC_BASE_URL=https://wb.example.com
export ANTHROPIC_AUTH_TOKEN=wbk_xxxxxxxx   # 也接受 x-api-key 头
export ANTHROPIC_MODEL=glm-5.2
```

模型名与 OpenAI 侧**同一套**（含 `global:` 前缀的版本规则与密钥版本隔离），
`/v1/messages/count_tokens` 亦可用。

</details>

<details>
<summary><b>可用模型</b></summary>

以「设置 → 可用模型」实时拉取结果为准，常见如下（上下文均为 131072）：

`glm-5.2` · `glm-5.1` · `glm-5v-turbo` · `kimi-k2.7` · `minimax-m3` · `hy3` · `hy3-preview`

</details>

---

## 接口说明

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| `POST` | `/v1/chat/completions` | 网关密钥 | OpenAI 兼容对话（流式 / 非流式） |
| `POST` | `/v2/chat/completions` | 网关密钥 | 同上（v2 路径） |
| `POST` | `/v1/responses` `/responses` | 网关密钥 | OpenAI Responses API 兼容（流式 / 非流式） |
| `POST` | `/v1/messages` | 网关密钥 | Anthropic Messages API 兼容（Claude Code 等） |
| `POST` | `/v1/messages/count_tokens` | 网关密钥 | 按字符数粗估输入 token |
| `GET` | `/v1/models` | 网关密钥 | 模型列表 |
| `GET` | `/healthz` | 无 | 存活探测（含上游连通性） |
| `GET` | `/api/me` | 会话 | 当前登录用户 |
| `POST` | `/api/login` `/api/logout` | 无 | 登录 / 登出 |
| `GET` | `/api/accounts` | 会话 | 账号列表 |
| `POST` | `/api/auth/start` `/api/auth/poll` | 管理员 | 扫码授权流程 |
| `POST` | `/api/accounts/{file}/checkin` `/test` `/refresh` | 管理员 | 签到 / 测活 / 刷新 |
| `DELETE` | `/api/accounts/{file}` | 管理员 | 删除账号 |
| `GET/POST/PATCH/DELETE` | `/api/keys[/{id}]` | 会话 / 管理员 | 密钥管理（发给下游调模型） |
| `GET/POST/PATCH/DELETE` | `/api/tokens[/{id}]` | 会话（管理员） | 管理面 API Token（给脚本 / CI，见 [docs/api-tokens.md](docs/api-tokens.md)） |
| `GET` | `/api/logs` `/api/stats/*` | 会话 | 日志与用量 |
| `GET/POST/DELETE` | `/api/security/*` | 会话 / 管理员 | IP 规则与审计 |
| `GET/POST` | `/api/settings/*` | 会话 / 管理员 | 上游配置、模型映射 |

管理端接口细节可在服务启动后访问 `/docs` 查看（Swagger UI）。

---

## 目录结构

```
workbuddy-manager/
├─ server/                       # FastAPI 后端
│  ├─ main.py                    # 入口：路由注册 + 静态托管
│  ├─ config.py                  # 全部环境变量与 http_client 工厂
│  ├─ db.py                      # SQLite（密钥/日志/用量/IP/设置）
│  ├─ security.py                # PBKDF2 + 签名 Cookie + 防爆破
│  ├─ keysvc.py                  # 密钥生成、校验、限额判定
│  ├─ iputil.py                  # 真实 IP 解析 + CIDR 匹配
│  ├─ services/
│  │  ├─ tencent.py              # 腾讯登录 / 签到 / 探测协议
│  │  └─ wb2api.py               # workbuddy2api 交互（含配置写入校验）
│  ├─ tests/                     # 配置读写回归测试
│  └─ routers/                   # auth accounts keys logs stats security settings gateway
├─ web/                          # Next.js 15 前端
│  ├─ app/(main)/                # dashboard accounts keys logs stats security settings
│  ├─ app/(auth)/login/          # 登录页
│  ├─ components/ui/             # shadcn 原语（含 floating-dock）
│  └─ components/common/         # 浮动底栏、统计卡、各业务组件
├─ dev/mock_upstream.py          # 本地联用的模拟上游
├─ deploy/                       # systemd unit + 一键部署脚本
└─ docs/                         # 设计与实现文档 + 界面截图
```

---

## 安全说明

- 网关密钥仅存 SHA-256 哈希，明文只在创建时返回一次
- 管理端密码使用 PBKDF2-SHA256（26 万次迭代）加盐存储
- 会话使用 HttpOnly + SameSite=Lax 签名 Cookie，生产环境自动启用 `Secure`
- 同 IP 登录失败 5 次锁定 10 分钟
- 所有文件操作做路径穿越校验
- **真实 IP 取自反代覆盖写入的 `X-Real-IP`**（`X-Forwarded-For` 首段可伪造），
  避免 IP 白/黑名单、每密钥 IP 限制与登录锁定被冒充绕过
- 登录失败**按 IP + 用户名双维度锁定**，防单机与换 IP 的分布式爆破
- 管理面支持**作用域化 API Token**（只读 / 管理员，可吊销、可过期，库中仅存哈希、
  全程审计），供脚本 / CI 免登录调用；**高危接口与令牌管理本身只接受会话登录**，
  令牌泄露也无法提权或自助持久化（见 [docs/api-tokens.md](docs/api-tokens.md)）
- 生产环境默认关闭 `/docs`、`/openapi.json`（`WB_ENABLE_DOCS=1` 开启）
- 网关限制请求体大小（8 MiB）与每密钥调用频率（默认 120 次/分钟）
- 已配置 CSP、`X-Frame-Options`、`X-Content-Type-Options` 等安全响应头
- `users.json`、`data/*.db`、`.env`、账号授权文件均已在 `.gitignore` 中排除

> 完整审查结论见 [安全审查报告](docs/SECURITY-AUDIT.md)（含已修复的高危问题与验证证据）。

### 已知的统计口径

- 用量按**本地时区**归日（写入、回填、展示统一口径）。历史上写入用本地、
  回填用 UTC，会在 UTC+8 的机器上把凌晨的调用算成两天；如你的数据受此影响，
  可在「用量 → 重建统计」以请求日志为准重建一次

### 安全建议（部署后）

1. **改掉初始密码**，不要沿用部署脚本中的默认值
2. **务必经 HTTPS 访问**：7863 / 7864 建议只监听 `127.0.0.1`，由反向代理对外
3. 如需前置 CDN，请把 `WB_TRUSTED_PROXY_HOPS` 设为 CDN + 反代的层数
4. 发现问题请走[私密渠道](https://github.com/ithtelab/workbuddy-manager/security/advisories/new)，
   **不要**公开提交 Issue

> ⚠️ 公网暴露**必须**启用 HTTPS，否则会话 Cookie 与密码可被中间人窃取。
> 建议再叠加 1Panel IP 白名单或 Cloudflare Access 加固。

---

## 已知限制

- **出站 IP 池未包含**：当前仅做**入站** IP 管控。若要为每个腾讯账号绑定独立**出口 IP / 代理**（上游请求由 workbuddy2api 发出），需要在其 Go 服务侧增加代理池支持，不在本仓库范围内。
- 请求的**请求体 / 响应体内容不做留存**，仅记录元数据（模型、状态、Token、延迟、来源），以保护隐私。
- 用量统计按「天 × 密钥 × 模型」聚合；如需小时粒度可扩展 `usage_daily` 表。

---

## 更新日志与反馈

- **更新日志**：[CHANGELOG.md](CHANGELOG.md) —— 各版本的新增、修复与变更
- **下载发布包**：[Releases](https://github.com/ithtelab/workbuddy-manager/releases) —— 每个版本提供可直接部署的
  `.tar.gz` / `.zip`（含已构建的前端产物），解压后执行 `sudo bash deploy/install.sh` 即可
- **反馈问题**：[提交 Bug](https://github.com/ithtelab/workbuddy-manager/issues/new?template=bug_report.yml) ·
  [功能建议](https://github.com/ithtelab/workbuddy-manager/issues/new?template=feature_request.yml)

> 反馈时请附上版本号与错误日志，并**先移除其中的密钥、Token 等敏感信息**。
> 上游 workbuddy2api 自身的问题也提到[本仓库](https://github.com/ithtelab/workbuddy-manager/issues)
> ——上游源码随本项目的发布包分发。

### 版本发布流程

维护者打 tag 即可自动构建并发布：

```bash
git tag v1.0.1 && git push origin v1.0.1
```

CI 会构建前端、打包产物、从 CHANGELOG 提取对应版本段落作为发布说明，
并创建 Release 附带压缩包。

---

## 赞助与推广

> 说明：以下为合作方推广信息。本项目部署需要一台能跑 Docker 的服务器，提供给有需要
> 的用户参考；**本仓库与其没有技术依赖**，用别家服务器同样能正常部署。

### 爱维云 · 免备案云服务器

[![爱维云 · lovevps.cn](docs/images/lovevps.png)](https://lovevps.cn/)

**长期七五折**，下单时填优惠码 **`catfk`** ｜ 官网：<https://lovevps.cn/>

- **免备案、即开即用** —— 香港（5 个可用区）、美国、日本、新加坡、马来西亚、德国等
  节点；国内另有济南、厦门、襄阳、绍兴、广州、西安、深圳等多个机房
- **线路优化** —— CN2 / 9929 / BGP 精品线路；高防机型提供 200G 流量防御
- **住宅 IP** —— 美国节点提供原生住宅宽带 IP
- **弹性计费** —— 按需创建与释放，配置可随时升降
- **资质** —— 持 IDC / ISP / CDN 经营许可（B1-20263321、苏B2-20263329）

---

## 相关项目

- [**sanguine886/workbuddy-sdk**](https://github.com/sanguine886/workbuddy-sdk)（Go，MIT）——
  社区维护的 Go 客户端库，完整覆盖本项目的两个 API 面：管理面 `/api/*`（账号池、密钥、
  统计、日志、安全、设置、用户、系统更新）与数据面 `/v1/*`（Chat Completions /
  Responses / Anthropic Messages / Models）。用 Go 写运维工具时可直接 `go get`，
  不必自己拼 HTTP 与登录态。

> 以上为社区项目，**与本仓库无代码依赖**；使用中遇到问题请到其[仓库](https://github.com/sanguine886/workbuddy-sdk/issues)反馈。

## 致谢

- [**LINUX DO**](https://linux.do) —— 本项目的发布与交流社区
- [**linux-do/cdk**](https://github.com/linux-do/cdk)（MIT）—— 界面设计令牌与浮动底栏组件来源，本项目 UI 视觉与其保持一致
- [**Sliverkiss/workbuddy2api**](https://github.com/Sliverkiss/workbuddy2api) —— 底层账号池与 OpenAI 兼容代理（MIT；源码随本项目的发布包分发，版权归原作者）
- [**lbjlaq/Antigravity-Manager**](https://github.com/lbjlaq/Antigravity-Manager) —— 管理端功能形态参考

## Hello World

按照惯例，来一段带注释的 Hello World。本项目服务端是 Python（FastAPI），那就用它打招呼：

```python
# -*- coding: utf-8 -*-
# 经典的 Hello World：先定义问候语，再打印出来
greeting = "Hello, World!"   # 定义字符串变量 greeting，存放问候语
print(greeting)              # 在控制台输出：Hello, World!
```

## License

[MIT](LICENSE)
