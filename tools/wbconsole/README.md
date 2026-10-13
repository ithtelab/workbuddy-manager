# WorkBuddy Manager 一键部署

一个 Go 写的小程序，编译成单个 exe。**双击就把整套项目部署好并启动**：建虚拟环境、装依赖、
编译上游网关、拉起上游与管理端 —— 全程不弹窗口，也不用敲任何命令。

- 双击 → 等一会儿 → 浏览器打开 `http://127.0.0.1:7864`
- 装好之后再次双击，会直接拉起服务，不会重复装依赖
- 不弹窗口，所以可以直接放进「启动」目录做开机自启

它和 `windows/start.cmd`、`windows/service-tools.ps1` 覆盖的是同一件事，区别在于：

- **部署时不弹任何窗口**：用 `-H windowsgui` 编译，双击不会有控制台蹦出来。
- **顺手补齐部署**：缺 `.venv` 就建、缺依赖就装、缺 `wb2api.exe` 就 `go build`。
- **管理端脱离终端后台运行**：关掉任何窗口都不会把它带走。

## 构建

前置：Go 1.27 或更高（见 `go.mod`），没有任何第三方依赖。

```powershell
cd tools\wbconsole

# 普通版：本项目唯一的构建形态，双击即完成部署并启动
go build -trimpath -ldflags "-s -w -H=windowsgui -X main.defaultAction=deploy" `
  -o "..\..\bin\WorkBuddy Manager.exe" .
```

两个编译参数都不能省：

- `-H windowsgui` 让 exe 不自带控制台。省了它，双击会先弹一个黑窗，部署时的
  `python.exe` 还会再弹一个。
- `-X main.defaultAction=deploy` 让它被双击（没有参数）时直接走部署。

exe 放哪都行，但**建议放进项目目录内**（例如 `bin\`）。程序按「自己所在目录向上找含
`server/main.py` 的目录」定位项目根，**不依赖当前工作目录**，所以双击、快捷方式、
命令行启动表现一致。也可以用 `WB_MANAGER_ROOT` 环境变量直接指定项目根。

停止 / 重启 / 看状态传第一个参数即可：

```powershell
"WorkBuddy Manager.exe" stop      # 关闭管理端和上游
"WorkBuddy Manager.exe" restart
"WorkBuddy Manager.exe" status
```

exe 不带控制台，所以这些动作会照常执行但**没有回显**；要看结果就用
`windows\stop.cmd`，或把它重定向到文件（`> status.txt`）。

## 图标

exe 图标取自前端自己的 favicon（`web/public/favicon/favicon.ico`），与站点图标一致。
`rsrc.syso` 是已经生成好的资源文件，跟着仓库走，直接 `go build` 就带图标。需要重新生成时：

```powershell
go install github.com/akavel/rsrc@latest
rsrc -arch amd64 -ico ..\..\web\public\favicon\favicon.ico -o rsrc.syso
```

## 启动管理端时的硬要求

管理端是 `python -m uvicorn server.main:app`，stdout / stderr 重定向到
`data\manager.out.log` / `data\manager.err.log`，PID 写进 `data\manager.pid`。
子进程的创建标志踩过一次坑，改这段之前先读：

- **必须带 `CREATE_NO_WINDOW`（`0x08000000`）**。本程序用 `-H windowsgui` 编译，自己不带
  控制台；不加这个标志，`python.exe` 这类控制台程序会凭空弹出一个黑窗（`cmd.exe` 同理）。
- **不能改用 `DETACHED_PROCESS`（`0x08`）**。Windows 规定这两个标志同用时
  `CREATE_NO_WINDOW` 会被**忽略**，结果是 `python.exe` 反而新开一个**可见**控制台 ——
  「部署完弹出一个 python.exe 黑窗」就是这么来的。实测：`0x208` 出可见窗口，
  `0x08000200` 无窗口。
- 它同时让子进程**不继承**调用者的控制台，所以管理端不会跟着任何窗口一起被关掉。

## 目录约定

程序按项目根的相对路径工作，与发布包布局一致：

| 路径 | 用途 |
|---|---|
| `server\main.py` | 定位项目根的标志 |
| `.venv\Scripts\python.exe` | 管理端解释器；缺失时用 `py -3.12 -m venv` 创建 |
| `web\out\index.html` | 前端构建产物；缺失直接报错，不做构建 |
| `upstream\wb2api.exe` | 上游网关；缺失时 `go build -trimpath -ldflags=-s -w -o wb2api.exe ./cmd/server` |
| `upstream\start-workbuddy2api.cmd`、`stop-workbuddy2api.cmd` | 上游启停脚本 |
| `data\manager.pid`、`data\manager.*.log` | 管理端 PID 与日志 |

上游固定按 `upstream\` 找，与仓库 `.gitignore` 里锚定的 `/upstream/` 一致。
