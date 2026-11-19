# Windows 启动脚本

双击或命令行运行这里的脚本，管理**本机原生部署**的 WorkBuddy Manager
（Docker 部署用不到它们，见仓库根目录的 `docker-compose.yml`）。

| 脚本 | 作用 |
|---|---|
| `start.cmd` | 前台启动面板（**关掉窗口即停**），顺带拉起上游网关；首次运行会自动生成 `.env`、初始化上游 `config.json`、准备 Python 虚拟环境 |
| `stop.cmd` | 停掉面板与上游网关 |
| `update.cmd` | 一键更新：下载发布包 → **核对发布页 sha256（拿不到就拒绝安装）** → 解压到临时目录校验 → 整体替换 `server/` 与 `web/out/` → 同步依赖 |
| `service-tools.ps1` | 后台常驻方式：`start` / `stop` / `status` / `restart`（日志写 `data\manager.out.log`） |

```powershell
# 在前台看日志地跑（等价于双击 start.cmd）
.\windows\start.cmd

# 后台常驻
powershell -ExecutionPolicy Bypass -File .\windows\service-tools.ps1 start
powershell -ExecutionPolicy Bypass -File .\windows\service-tools.ps1 status
```

**更新脚本本身要从仓库取。** 发布包里不带这个目录（这些脚本是给本机原生部署用的，
跟着你的路径与习惯走），`update.ps1` 更新前后还会把它们备份到 `.tools\scripts_backup`
再按名字还原回这里 —— 也就是说它**不会**被发布包替换。

v1.0.86 起发布包不再带 `.sig`，仓库里的 `update.ps1` 也从「验签」改成了「核对 sha256」。
2.2.1 起这一步是**失败即拒绝**：拿不到发布页摘要、或摘要与包不一致，都不会继续安装。
只在确认包确实来自官方发布页时，才可加 `-AllowUnverified` 强制继续（会大声警告）。
所以用 `update.cmd` 的用户请**从仓库重新取一次本目录的脚本**（或改用面板的
「设置 → 系统更新」，那条路走的是已经改好的 Python 更新器）：停在旧脚本上，v1.0.87
起的新版本会被它自己拦下（「缺少对应的数字签名文件」）。

**这个目录不能挪。** 脚本按「自己所在目录的上一级」当项目根，去找 `server/`、
`web/out`、`.env`、`upstream/`；`update.ps1` 更新前后还会把这一套脚本备份到
`.tools\scripts_backup` 再按名字还原回**这里**。把它挪到别处（或把脚本单独复制出去）
会变成「启动时报找不到 .venv / 更新后根目录多出一套脚本」这类问题。

前置条件与原生模式的边界（更新上游不可用、手动构建 `wb2api.exe` 等）见
[`deploy/windows-native/README.md`](../deploy/windows-native/README.md) 与
[`deploy/README.md`](../deploy/README.md) 的「Windows 原生部署」一节。

## 启动器（安装版）

`launcher/` 是一个把上面这套脚本包成图形界面的安装器 + 启动器，产出**单文件 exe**：

| 产物 | 作用 |
|---|---|
| `build.ps1` | 从源码构建安装包：组装 payload、编译上游 `wb2api.exe`（本机无 Go 时自动下载便携工具链）、预置 Python 3.11 运行时与依赖、csc 编译 |
| `dist\WorkBuddyManagerSetup.exe` | 双击进入安装向导：选择目录安装、生成桌面与开始菜单快捷方式、注册「设置 → 应用」卸载入口 |
| `dist\WorkBuddyManagerSetup.exe.sha256` | 安装包的 SHA-256（与 CI 产物同构）；分发后可用 `certutil -hashfile WorkBuddyManagerSetup.exe SHA256` 核对 |
| 安装目录里的 `WorkBuddyManager.exe` | 控制窗口：启动 / 重启 / 关闭项目、运行状态、修改监听 IP 与端口、打开面板、勾选「开机自动启动（静默驻留托盘）」、点「检查更新」下载并校验新安装包；点 X 可选最小化到托盘或退出（可记住选择） |
| 安装目录里的 `Uninstall.exe` | 卸载程序：停止服务、清理快捷方式与注册表（含开机自启项），可选保留账号与配置数据 |

```powershell
# 构建安装包（产物在 windows\launcher\dist\）
powershell -ExecutionPolicy Bypass -File windows\launcher\build.ps1
```

安装包内嵌全部运行环境（Python 解释器与依赖、上游网关二进制、前端产物），
目标机器**无需安装 Python / Node / Go**，安装后即可直接启动。构建细节见
[`launcher/build.ps1`](launcher/build.ps1) 顶部注释。

### 安装目录与卸载（两点要知道）

**安装目录**：向导里选中的文件夹被当作**父目录**，程序装进它的 `WorkBuddyManager`
子目录（例如选 `D:\workspace` → 实际装到 `D:\workspace\WorkBuddyManager`）。
两种例外按原样使用：选中的目录本身已经是本程序的安装目录（覆盖安装），或者它的名字
就叫 `WorkBuddyManager`。

**卸载只删自己的东西**：安装时会把「这个程序放下了哪些文件」写进安装目录里的
`WorkBuddyManager.install.txt`；卸载只删清单里登记的、以及程序自有目录（`server`、
`web`、`upstream`、`data`、`.runtime` 等）里的文件。**任何不属于本程序的文件都会被
原样保留**，并在卸载完成页列出来。在不是安装目录的地方运行卸载（例如把它复制到别处
再点）会直接拒绝，不动任何文件。

### 发布资产、第三方来源与安全提示

发版时 `.github/workflows/release.yml` 会跑两个作业：Ubuntu 上打 `.tar.gz` / `.zip`
发布包，Windows 上（`installer` 作业）构建并上传 `WorkBuddyManagerSetup.exe` 与它的
`.sha256`。构建脚本自己取依赖，所以不需要在 runner 上预装 Go 或 Python。

**该资产偶尔会缺失**：Windows 作业失败时它不会出现在 Release 里（发布包本身不受影响），
这时按上面的命令自己构建一次即可。已经发布的版本不会因为重跑工作流而换掉产物 ——
发布说明里的 sha256 是对用户的承诺，换掉字节流会让所有人的更新校验失败。

默认构建的第三方来源如下：

- **Python**：来自 [Astral python-build-standalone](https://github.com/astral-sh/python-build-standalone)
  的 CPython 3.11 Windows x64 MSVC `install_only_stripped` 运行时。脚本优先解析最新的
  3.11 匹配包，网络解析失败时回退到 `3.11.17+20261009`；构建日志会打印实际下载 URL。
  Python 的 `LICENSE.txt` 会随 `.runtime\python\` 一起打包，构建脚本在预置运行时
  缺少该文件时拒绝复用/打包。
- **上游网关**：从固定的
  [上游源码载体](https://github.com/ithtelab/workbuddy-manager/releases/download/upstream-src/workbuddy2api-src.tar.gz)
  下载源码，再用 Go 编译 `./cmd/server` 生成 `wb2api.exe`；不是下载上游预编译二进制。
  上游源码中的 `upstream\LICENSE` 也会随包分发。
- **无代码签名**：当前安装器没有 Authenticode 签名，Windows SmartScreen 可能显示
  “Windows 已保护你的电脑”等警告。这不代表安装器必然恶意；请只从可信的项目 Release
  或自行构建来源取得文件，确认来源后再按 Windows 提示继续运行。

使用 `-SkipRuntime`，或默认构建时 Python 预置失败而退回 uv 时，安装包只内置 `uv.exe`，
首次启动需要联网准备 Python 环境；构建结束日志会明确显示当前属于哪种模式。

### 安装器自身升级

应用内的 `update.ps1` 只更新项目代码、前端和依赖，并保护本机 Windows 脚本；它**不会
自更新 `WorkBuddyManager.exe` / `Uninstall.exe`**。升级安装器本身的步骤是：

1. 停止正在运行的 WorkBuddy Manager。
2. 从新版本 Release 下载新的 `WorkBuddyManagerSetup.exe`，或在源码分支重新构建。
3. 运行新的安装器并选择原安装目录，确认覆盖安装。
4. 覆盖安装会保留 `.env`、`data`、`upstream\auths` 和 `upstream\config.json`；
   新的启动器与卸载程序会覆盖旧版本。

因此，安装器不是“自己去取新版”的自更新程序；发布包没有 exe 资产时，只能先自行构建
或等待维护者上传 Windows 安装器。

