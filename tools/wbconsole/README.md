# WorkBuddy Manager 本机控制台

一个 Go 写的小程序，编译成单个 exe，放在项目根目录。**双击打开控制台菜单**，四件事：
部署并启动 / 关闭 / 重启 / 查看状态。选「1」即完成整套项目的部署：建虚拟环境、装依赖、
编译上游网关、拉起上游与管理端 —— 全程不用敲命令。

- 双击 → 选「1」→ 浏览器打开 `http://127.0.0.1:7864`
- 已经部署过的机器上再选「1」，会直接拉起服务，不会重复装依赖
- 装依赖、编上游的进度直接打在这一个窗口里，看得见

它和 `windows/start.cmd`、`windows/service-tools.ps1` 覆盖的是同一件事，区别在于：

- **多一个菜单**：四项操作都在一个窗口里，不用记命令。
- **不会冒出第二个控制台**：管理端、上游、依赖探测都用 `CREATE_NO_WINDOW` 起，
  全程只有双击出来的这一个窗口。
- **管理端脱离本窗口**：关掉菜单窗口不会把它带走，服务继续跑。

## 构建

前置：Go 1.27 或更高（见 `go.mod`），没有任何第三方依赖。

```bat
tools\wbconsole\build.cmd
```

脚本**只产出一个 exe**，直接放在**项目根目录**：`WorkBuddy Manager.exe`。它等价于：

```powershell
cd tools\wbconsole
go build -trimpath -ldflags "-s -w" -o "..\..\WorkBuddy Manager.exe" .
```

**不要加 `-H windowsgui`** —— 那会让 exe 不带控制台，菜单就没法显示了。

exe 就放在项目根目录。程序按「自己所在目录向上找含 `server/main.py` 的目录」定位项目根，
**不依赖当前工作目录**，所以双击、快捷方式、命令行启动表现一致。也可以用
`WB_MANAGER_ROOT` 环境变量直接指定项目根。

除了菜单，也可以传第一个参数：

```powershell
"WorkBuddy Manager.exe" deploy    # 同菜单里的「1」；不带参数则进菜单
"WorkBuddy Manager.exe" stop
"WorkBuddy Manager.exe" restart
"WorkBuddy Manager.exe" status
```

## 图标

exe 图标取自前端自己的 favicon（`web/public/favicon/favicon.ico`），与站点图标一致。
`rsrc.syso` 是已经生成好的资源文件，跟着仓库走，直接 `go build` 就带图标。需要重新生成时：

```powershell
go install github.com/akavel/rsrc@latest
rsrc -arch amd64 -ico ..\..\web\public\favicon\favicon.ico -o rsrc.syso
```

## 子进程的创建标志

管理端是 `python -m uvicorn server.main:app`，stdout / stderr 重定向到
`data\manager.out.log` / `data\manager.err.log`，PID 写进 `data\manager.pid`。
创建标志踩过一次坑，改这段之前先读：

- **管理端、上游、依赖探测都必须带 `CREATE_NO_WINDOW`（`0x08000000`）**。它让子进程既不
  分配自己的控制台，也不继承本窗口 —— 所以全程不会多出第二个窗口，而且**关掉菜单窗口时
  管理端不会跟着一起死**。
- **不能改用 `DETACHED_PROCESS`（`0x08`）**。Windows 规定这两个标志同用时
  `CREATE_NO_WINDOW` 会被**忽略**，`python.exe` 反而会新开一个**可见**控制台 ——
  「部署完莫名多出一个 python.exe 黑窗」就是这么来的。实测：`0x208` 出可见窗口，
  `0x08000200` 无窗口。
- **建 venv、`pip install`、`go build` 上游这三步故意不加上面的标志**：让它们继承本窗口，
  进度看得见；继承不会另开窗口。

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
