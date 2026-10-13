# WorkBuddy Manager 本机控制台

一个 Go 写的小程序，编译成单个 exe，给**本机原生部署**的 WorkBuddy Manager 提供双击即用的
控制台：部署并启动 / 关闭 / 重启 / 查看状态。双击开一个终端出菜单，全程不用敲命令。

它和 `windows/start.cmd`、`windows/service-tools.ps1` 覆盖的是同一件事，区别在于：

- 关掉这个窗口**不会**带走管理端 —— 管理端脱离终端后台运行，更接近 `service-tools.ps1`。
- 顺手补齐部署：缺 `.venv` 就建、缺依赖就装、缺 `wb2api.exe` 就 `go build`。
- 「查看状态」同时看 PID 文件、端口和 `/api/healthz`，排障时比只看进程在不在有用。

## 构建

前置：Go 1.27 或更高（见 `go.mod`）。没有任何第三方依赖。

```powershell
cd tools\wbconsole

# 菜单版：双击开终端，列出菜单
go build -trimpath -ldflags "-s -w" -o "..\..\bin\WorkBuddy Manager.exe" .

# 静默版：双击不弹窗，直接部署并启动
go build -trimpath -ldflags "-s -w -H=windowsgui -X main.defaultAction=deploy" `
  -o "..\..\bin\WorkBuddy 静默启动.exe" .
```

exe 放哪都行，但**建议放进项目目录内**（例如 `bin\`）。程序按「自己所在目录向上找含
`server/main.py` 的目录」定位项目根，**不依赖当前工作目录**，所以双击、快捷方式、
命令行启动表现一致。也可以用 `WB_MANAGER_ROOT` 环境变量直接指定项目根。

## 图标

exe 图标取自前端自己的 favicon（`web/public/favicon/favicon.ico`），与站点图标一致。
`rsrc.syso` 是已生成好的资源文件，跟着仓库走，直接 `go build` 就带图标。需要重新生成时：

```powershell
go install github.com/akavel/rsrc@latest
rsrc -arch amd64 -ico ..\..\web\public\favicon\favicon.ico -o rsrc.syso
```

## 启动管理端时的硬要求

管理端是 `python -m uvicorn server.main:app`，stdout / stderr 重定向到
`data\manager.out.log` / `data\manager.err.log`，PID 写进 `data\manager.pid`。
子进程的创建标志踩过一次坑，改这段之前先读：

- **必须带 `CREATE_NO_WINDOW`（`0x08000000`）**。静默版是 `-H windowsgui`，自己不带控制台；
  不加这个标志，`python.exe` 这类控制台程序会凭空弹出一个黑窗（`cmd.exe` 同理）。
- **不能改用 `DETACHED_PROCESS`（`0x08`）**。Windows 规定这两个标志同用时
  `CREATE_NO_WINDOW` 会被**忽略**，结果是 `python.exe` 反而新开一个**可见**控制台 ——
  「部署完弹出一个 python.exe 黑窗」就是这么来的。实测：`0x208` 出可见窗口，
  `0x08000200` 无窗口。
- 它还保证子进程**不继承**本终端的控制台，所以关掉窗口时管理端继续跑。

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
