# Windows 原生部署：上游启停脚本

管理端支持上游以**本机进程**（而非 Docker 容器）运行时，需要一对启停脚本 ——
在 `.env` 里这样配置：

```dotenv
WB2API_MODE=native
WB2API_START_SCRIPT=C:/path/to/workbuddy2api/start-workbuddy2api.cmd
WB2API_STOP_SCRIPT=C:/path/to/workbuddy2api/stop-workbuddy2api.cmd
WB2API_LOG_FILE=C:/path/to/workbuddy2api/data/server.err.log
```

## 先看上游：它现在自带脚本了

**上游 `workbuddy2api` 2026-09-18 起已自带 Windows 原生脚本**（`start-workbuddy2api.cmd`
/ `stop-workbuddy2api.cmd` / `status-workbuddy2api.cmd`）。上游目录里已经有这三个文件的话
**直接用它们**，把上面的两个路径指过去即可，不需要本目录的模板：

- 上游脚本用 **PID 文件 + 进程路径校验**来判断「是不是我启的那个进程」。这比按进程名
  匹配可靠：同名的别的程序、或同时跑着两个实例，都不会被误杀。
- 上游脚本自带 `status-workbuddy2api.cmd`，除 PID 外还会打一次 `/healthz`，
  排障时比只看进程在不在有用。

本目录的两个 `.cmd` 是**给旧版上游用的**（那时官方只提供 Docker 部署）。如果你的
上游目录里没有上面那三个文件，就把这两个复制过去、按里面的 `TODO` 改路径。

## 两个必须遵守的约定

无论用上游自带的还是这里的模板，两条约定必须满足（上游那套已经满足）。

**1. 启动脚本必须立即返回。**

管理端是「调用脚本 → 等它结束 → 认为重启完成」。如果脚本在前台一直运行（直接
`wb2api.exe ...` 而不后台拉起），管理端会一直等到超时（60 秒）才失败 —— 上游其实
已经起来了，但界面报的是失败。上游脚本用 PowerShell `Start-Process` 解决，模板用
`start "workbuddy2api" /b ...`，两者都是后台拉起后立即返回。

**2. 日志要写到 `WB2API_LOG_FILE` 指向的文件。**

管理端「任务记录」页的自动任务日志（旅行领奖、签到、保活…）是从那个文件读的，
不是从 `docker logs`。两套脚本都把 stdout / stderr 分别重定向到 `server.out.log` /
`server.err.log`，所以 `.env` 的 `WB2API_LOG_FILE` 指向 `server.err.log`。

## 上游可执行文件从哪来

上游用 Go 写，Docker 之外的部署方式需要自己构建：

```powershell
cd C:\path\to\workbuddy2api
go build -o wb2api.exe ./cmd/server
```

构建后目录里应有 `wb2api.exe`、`config.json`、`auths\`、`data\`。
登录账号可以继续用上游自带的 `login.sh`（需要 Git Bash / WSL），
或在容器里登录后把 `auths\` 拷出来 —— 总之管理端只要求 `auths\` 里有
`workbuddy-*.json`。

## 能力边界

**更新上游**在原生模式下不可用（它要操作 Docker）：界面上会明确提示，点了也不会执行。
更新上游请手动拉代码、重新 `go build`，然后重启管理端。

**更新管理端本身**可以点：它会下载发布包、验签、替换 `server/` 与 `web/out/`。原生模式
下它**不会自动重启面板** —— 更新完成后请关掉运行中的窗口，重新执行 `windows\start.cmd`（或后台方式：`windows\service-tools.ps1 start`）。
（中文 Windows 上，1.0.72 及更早版本的更新进程会在打印日志时因编码问题中断，
表现为一直停在「正在更新：执行中」；若卡在这里，见 CHANGELOG 里 1.0.73 条目的处理办法。）
