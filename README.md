<div align="center">

# WorkBuddy Manager

**腾讯 CodeBuddy 账号池管理控制台 · OpenAI 兼容反代网关**

面向 [`workbuddy2api`](https://github.com/Sliverkiss/workbuddy2api) 的 Web 管理端：扫码纳管、定时签到与保活、密钥分发、IP 与模型白名单、日志与用量统计；上游源码随发布包分发（MIT），迁移见[部署指南](deploy/README.md#〇上游源码从哪来随发布包分发)。

![Next.js](https://img.shields.io/badge/Next.js-15-000000?logo=nextdotjs&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-22c55e)

[![Release](https://img.shields.io/github/v/release/ithtelab/workbuddy-manager?color=22c55e&label=Release)](https://github.com/ithtelab/workbuddy-manager/releases)
[![Changelog](https://img.shields.io/badge/更新日志-CHANGELOG-blue)](CHANGELOG.md)
[![Issues](https://img.shields.io/github/issues/ithtelab/workbuddy-manager?color=f59e0b&label=反馈)](https://github.com/ithtelab/workbuddy-manager/issues)

[English](README.en.md) · **简体中文** ｜ 本项目在 [LINUX DO](https://linux.do) 社区发布与交流

<a href="https://proxylane.dev/?utm_source=workbuddy-manager&utm_medium=partnership&utm_campaign=workbuddy-manager_sponsor_202610&utm_content=github_readme"><img src="docs/images/sponsor-proxylane.png" alt="ProxyLane（赞助位）" width="100%" /></a>

**赞助商**：[ProxyLane](https://proxylane.dev/?utm_source=workbuddy-manager&utm_medium=partnership&utm_campaign=workbuddy-manager_sponsor_202610&utm_content=github_readme) 住宅代理，覆盖 195 个国家，95%+ IP 获 Scamalytics 低风险评级。在 `WB_HTTP_PROXY` 的用户名里写上国家、城市和会话名，每个实例走一个住宅 IP，最长 72 小时不变，换 IP 也留在同一城市。

首次购买使用优惠码 `WORKBUDDY30` 享 7 折。

<img src="docs/images/dashboard.png" alt="WorkBuddy Manager 仪表盘" width="100%" />

**界面预览**

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

## 功能亮点

### 账号管理

- **扫码纳管** —— 微信 / QQ 扫码授权，成功后自动每日签到、写授权文件、重载上游
- **有效期监控** —— 进度条 + 临期预警；一键续期、连通性探测、手动签到
- **账号分组** —— 多个账号池分组管理，列表、签到、密钥各自独立
- **积分与到期** —— 实时余额、按余额分级着色、快过期积分倒计时
- **任务记录** —— 签到 / 旅行 / 活跃 / 保活的执行结果与积分流水分页可查

### 反代网关（对外 `/v1`）

- **OpenAI 兼容** —— 标准 SDK 直连，流式与非流式都支持
- **多密钥分发** —— 每把密钥独立设版本、有效期、IP 数、IP / 模型白名单与额度
- **模型别名映射** —— 把下游在用的名字映到实际模型，迁移无感
- **模型中心** —— 账号可用模型单列一页：上下文、最大输出、思考档位
- **全量审计** —— 每次调用记密钥、IP、模型、状态码、首字延迟、扣费与缓存命中

### 可视化设置

- **定时任务** —— 签到 / 旅行 / 活跃 / 保活四类开关与执行时刻
- **多上游** —— 「设置 → 上游」配多个实例，各带地址与 `api_key`
- **安全** —— 入站 IP 白 / 黑名单（CIDR）、审计日志、会话与令牌分离
- **更新与备份** —— 一键更新（核验发布包摘要）、PostgreSQL 异地备份
- **五语种界面** —— 简体中文 / 繁体 / English / 日本語 / 한국어

完整清单（每条带详解）见 [功能一览（完整版）](docs/features.md)。

---

## 界面预览

<img src="docs/images/accounts.png" alt="账号管理" width="100%" />

账号管理：分组切换、状态分档、积分与有效期

<img src="docs/images/keys.png" alt="密钥" width="100%" />

密钥：版本、额度、IP 与模型白名单

<img src="docs/images/stats.png" alt="用量统计" width="100%" />

用量统计：按天 / 按小时、失败与延迟

<img src="docs/images/logs.png" alt="调用日志" width="100%" />

调用日志：每一笔的模型、状态、首字延迟与扣费

其余界面截图见 [界面预览（全部）](docs/screenshots.md)。

---

## 快速开始

服务器部署需要 Docker（1 核 1G 起）；Windows 桌面用户可直接用图形安装器。三种方式任选：

**① Windows 安装器（桌面图形化，无需 Docker / 命令行）**

1. 从该版本 [Release](https://github.com/ithtelab/workbuddy-manager/releases) 下载
   `WorkBuddyManagerSetup.exe`（发版时由 CI 在 Windows runner 上自动构建并附带，同目录还有
   一个 `.sha256` 可核对）。v1.0.88 的安装器曾因卸载会误删文件被下线（见该版本 Release
   说明），**v1.0.89 起已修复**：卸载只删程序自己的文件，安装目录也改成「选中目录 +
   `WorkBuddyManager` 子目录」。
2. 按向导选择安装目录 → 安装 → 完成时可勾选「立即启动」。
3. 之后用桌面快捷方式打开控制台：点 **启动项目**，状态变「运行中」并自动打开管理面板；
   随时可 **重启 / 关闭项目**、修改监听 IP 与端口（保存后重启生效）、点 **打开面板** 回到页面。
4. 点 X 可选「最小化到托盘」或「退出程序」（可记住选择，托盘图标随时恢复）。

> 默认成功构建的单文件安装包内置全部运行环境（Python 运行时、上游网关、前端页面），
> 目标机器**无需安装任何依赖**，Windows 10 (1903+) / 11 双击即用。当前安装器没有
> Authenticode 签名，SmartScreen 可能显示警告；请确认文件来自可信 Release 或自行构建。
> 安装器自身不会自动升级，升级时重新获取新 exe 并覆盖安装即可；`.env`、账号和 `data`
> 等个人数据会保留。卸载：**设置 → 应用** 或开始菜单「卸载 WorkBuddy Manager」，
> 可选保留账号与配置。细节见 [Windows 启动器](windows/README.md#启动器安装版)。

**② Docker（自己起容器）**

```bash
# 端口只绑本机：公网访问请走反向代理（HTTPS + 访问控制），直接暴露管理面风险高
docker run -d --name workbuddy-manager -p 127.0.0.1:7864:7864 -v /opt/workbuddy-manager/data:/app/data ghcr.io/ithtelab/workbuddy-manager:latest
```

**③ 服务器一键脚本（装依赖、拉包、配 systemd）**

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/ithtelab/workbuddy-manager/main/deploy/install.sh)
```

装完浏览器打开 `http://<服务器>:7864`，首次启动的日志里会打印初始管理员密码。

> 上游源码随发布包一起分发，装完即可用。反向代理与 HTTPS、端口收敛、Windows 原生
> 部署（无需 Docker）、本地开发等细节见 [部署指南](deploy/README.md) 与
> [开发与架构](docs/development.md)。

---

## 使用指南

日常就三步：**纳管账号 → 分发密钥 → 把下游指到 `/v1`**。每步怎么做、下游接入示例
（OpenAI SDK、cc-switch 等）与常见问题见 [使用指南](docs/usage.md)。

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
| `GET` | `/v1/usage` | 网关密钥 | 密钥自查：已用 / 剩余额度、有效期与模型范围（只读面板记录，不产生调用） |
| `GET` | `/healthz` | 无 | 存活探测（含上游连通性） |
| `GET` | `/api/me` | 会话 | 当前登录用户 |
| `POST` | `/api/login` `/api/logout` | 无 | 登录 / 登出 |
| `GET` | `/api/accounts` | 会话 | 账号列表 |
| `GET` | `/api/accounts/credits-snapshot` | 会话 / 只读令牌 | 已登记积分的只读快照（给看板 / 对账脚本，不查上游） |
| `POST` | `/api/auth/start` `/api/auth/poll` | 管理员 | 扫码授权流程 |
| `POST` | `/api/accounts/{file}/checkin` `/test` `/refresh` | 管理员 | 签到 / 测活 / 刷新 |
| `DELETE` | `/api/accounts/{file}` | 管理员 | 删除账号 |
| `GET/POST/PATCH/DELETE` | `/api/keys[/{id}]` | 会话 / 管理员 | 密钥管理（发给下游调模型） |
| `GET/POST/PATCH/DELETE` | `/api/upstreams[/{id}]` | 会话（管理员） | 多上游接入点（密钥绑定上游 = 账号池分组）；`POST /api/upstreams/{id}/probe` 探测连通性 |
| `GET/POST/PATCH/DELETE` | `/api/tokens[/{id}]` | 会话（管理员） | 管理面 API Token（给脚本 / CI，见 [docs/api-tokens.md](docs/api-tokens.md)） |
| `GET` | `/api/logs` `/api/stats/*` | 会话 | 日志与用量 |
| `GET/POST/DELETE` | `/api/security/*` | 会话 / 管理员 | IP 规则与审计 |
| `GET/POST` | `/api/settings/*` | 会话 / 管理员 | 上游配置、模型映射 |
| `GET/POST` | `/api/settings/pg-sync*` | 会话（管理员） | PostgreSQL 异地备份：配置 / 测试连接 / 迁移 / 恢复 |

管理端接口细节可在服务启动后访问 `/docs` 查看（Swagger UI）。

---

## 安全说明

- 密钥只在创建时显示一次，库里只存哈希；账号凭据不返回前端
- 入站 IP 白 / 黑名单（支持 CIDR）、每把密钥的 IP 与模型白名单
- 敏感操作全部留审计日志（谁、什么时候、做了什么）
- 发布包校验：一键更新会核对发布页给的 sha256 与文件大小，不符即拒绝安装

完整的威胁模型与加固建议见 [安全说明](docs/security-notes.md) 与
[安全审计报告](docs/SECURITY-AUDIT.md)。

---

## 已知限制

- **账号出口线路**：管理端支持按账号绑定命名 HTTP/HTTPS 代理。聊天和上游后台任务还需 workbuddy2api 配套支持，配置与兼容条件见 [账号出口线路](docs/account-proxy-routes.md)。
- 请求的**请求体 / 响应体内容不做留存**，仅记录元数据（模型、状态、Token、延迟、来源），以保护隐私。
- 用量统计按「天 × 密钥 × 模型」聚合；如需小时粒度可扩展 `usage_daily` 表。

---

## 更多文档

- [使用指南](docs/usage.md) —— 三部曲怎么点、下游怎么接
- [功能一览（完整版）](docs/features.md) —— 每条功能的细节
- [界面预览（全部）](docs/screenshots.md) —— 全部截图
- [开发与架构](docs/development.md) —— 本地开发、架构、目录结构
- [安全说明](docs/security-notes.md) —— 威胁模型与加固
- [部署指南](deploy/README.md) —— 反向代理、HTTPS、Windows 原生、一键更新
- [账号出口线路](docs/account-proxy-routes.md) —— 每个账号走不同代理出口
- [API 令牌](docs/api-tokens.md) —— 只读令牌与作用域

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

## 相关项目

- [**sanguine886/workbuddy-sdk**](https://github.com/sanguine886/workbuddy-sdk)（Go，MIT）——
  社区维护的 Go 客户端库，完整覆盖本项目的两个 API 面：管理面 `/api/*`（账号池、密钥、
  统计、日志、安全、设置、用户、系统更新）与数据面 `/v1/*`（Chat Completions /
  Responses / Anthropic Messages / Models）。用 Go 写运维工具时可直接 `go get`，
  不必自己拼 HTTP 与登录态。

> 以上为社区项目，**与本仓库无代码依赖**；使用中遇到问题请到其[仓库](https://github.com/sanguine886/workbuddy-sdk/issues)反馈。

---

## 致谢

- [**LINUX DO**](https://linux.do) —— 本项目的发布与交流社区
- [**linux-do/cdk**](https://github.com/linux-do/cdk)（MIT）—— 界面设计令牌与浮动底栏组件来源，本项目 UI 视觉与其保持一致
- [**Sliverkiss/workbuddy2api**](https://github.com/Sliverkiss/workbuddy2api) —— 底层账号池与 OpenAI 兼容代理（MIT；源码随本项目的发布包分发，版权归原作者）
- [**lbjlaq/Antigravity-Manager**](https://github.com/lbjlaq/Antigravity-Manager) —— 管理端功能形态参考

---

## License

[MIT](LICENSE)
