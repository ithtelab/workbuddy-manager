# 部署指南

本项目管理端**依赖上游 workbuddy2api**（提供账号池调度与 OpenAI 兼容接口）。
单独 clone 本仓库是跑不起来的 —— 为此我们提供了一键脚本，会在干净机器上安装好两者。

> **重要**：请勿把本项目管理端的数据目录与上游账号目录提交或公开分享，
> 其中含账号授权凭据。

---

## 〇、上游源码从哪来（随发布包分发）

上游 workbuddy2api 的源码**随本项目的发布包一起分发**（包内 `upstream/`），
装的时候不用去任何外部地址取。影响如下：

| 场景 | 现在的状态 |
|---|---|
| **已经在跑**的部署 | **不受影响**。上游源码在你机器上、镜像也是本地构建的；面板的一键更新会沿用现有源码重建上游，面板自己照常更新 |
| **新装** | 直接用包内那份（下面的脚本会自动识别） |
| **更新上游代码** | 跟着管理端一起更新（一键更新会把包内那份同步进来）；要改代码就改本地那份，或用 `UPSTREAM_SRC` 换一份 |

### 包内自带，开箱即用

最新版本的 Release 包内含 `upstream/`（上游源码），装的时候直接用，
**不需要联网取任何外部源码**：

```bash
wget https://github.com/ithtelab/workbuddy-manager/releases/latest/download/workbuddy-manager-<版本>.tar.gz
tar xzf workbuddy-manager-*.tar.gz && cd workbuddy-manager-*
sudo bash deploy/install.sh          # 自动使用包内的 upstream/
```

要改用你自己那份源码（或机器上已有的 `/opt/workbuddy2api`），按下面的优先级覆盖：

```bash
# 1) 显式指定本地源码（目录 / .tar.gz / .zip 都行）
sudo UPSTREAM_SRC=/opt/workbuddy2api bash deploy/install.sh
sudo UPSTREAM_SRC=/path/to/workbuddy2api-<版本>.tar.gz bash deploy/install.sh

# 2) 从你自己的 git 副本拉
sudo UPSTREAM_REPO=https://github.com/<你的账号>/workbuddy2api.git bash deploy/install.sh

# 3) 上游已手工装好，只想装面板
sudo bash deploy/install.sh --skip-upstream
```

优先级：`UPSTREAM_SRC` → 发布包自带的 `upstream/` → 目标目录里已有的 git 仓库
→ `UPSTREAM_REPO` 克隆。`UPSTREAM_DIR` 默认 `/opt/workbuddy2api`。

### 以后怎么更新上游代码

用面板里的**一键更新**即可 —— 它会把包内那份上游源码同步进 `/opt/workbuddy2api`
（只增改，不动 `config.json` / `auths/` / `data/`），**有变化才重建容器**。
随包分发的上游没有 `git pull` 可拉；要改代码请改本地那份（或用 `UPSTREAM_SRC`
覆盖一份新的），再：

```bash
cd /opt/workbuddy2api && docker compose up -d --build
```

这三样务必保留：`config.json`（含 `api_key`）、`auths/`（账号授权）、`data/`。

### 许可

上游为 MIT 许可（版权归原作者）。继续使用、修改、再分发都需保留它的 `LICENSE`
与版权声明 —— 源码目录里那份 `LICENSE` 不要删。

---

## 一、最简单的方式：Release 包 + 一键脚本

```bash
# 1) 下载 Release 包（内含已构建的前端，无需 Node.js）
wget https://github.com/ithtelab/workbuddy-manager/releases/latest/download/workbuddy-manager-<版本>.tar.gz
tar xzf workbuddy-manager-*.tar.gz
cd workbuddy-manager-*

# 2) 一键部署（自动检测上游；上游源码用 UPSTREAM_SRC 指定，见上一节）
sudo bash deploy/install.sh
```

脚本会自动完成：

1. 环境预检（Python ≥3.9、Docker、端口占用检查）
2. **安装上游 workbuddy2api** —— 取源码（本地目录或 git 地址）、生成随机 `api_key`、
   设置目录属主、构建并启动容器、等待就绪
3. 安装管理端 —— 部署代码、装依赖、注册 systemd 服务
4. 验证两条链路并打印访问地址与初始密码

**全程无需手工编辑任何配置文件。**

---

## 二、通过 git clone 部署

```bash
git clone https://github.com/ithtelab/workbuddy-manager.git
cd workbuddy-manager

# 需先在 web/ 构建前端（git 仓库不含构建产物）
cd web && npm ci && npm run build:export && cd ..

sudo bash deploy/install.sh
```

> 若机器上无 Node.js，请改用 Release 包，或先安装 Node.js ≥18。

---

## 三、已有 workbuddy2api 的情况

若已自行部署上游，用参数跳过安装，脚本不会改动已有配置与账号：

```bash
sudo bash deploy/install.sh --skip-upstream
```

也可用环境变量指定自定义路径与端口：

```bash
sudo APP_DIR=/opt/wbm \
     UPSTREAM_DIR=/srv/workbuddy2api \
     MANAGER_PORT=7864 UPSTREAM_PORT=7863 \
     bash deploy/install.sh
```

| 变量 | 默认值 | 说明 |
|---|---|---|
| `APP_DIR` | `/opt/workbuddy-manager` | 管理端目录 |
| `UPSTREAM_DIR` | `/opt/workbuddy2api` | 上游目录 |
| `UPSTREAM_REPO` | 上游 GitHub 地址 | 上游仓库地址 |
| `MANAGER_PORT` | `7864` | 管理端端口 |
| `UPSTREAM_PORT` | `7863` | 上游端口 |
| `PY` | `/usr/bin/python3` | Python 解释器路径 |

---

## 四、部署后

### 1. 获取登录密码

脚本会在末尾打印；也可随时查看：

```bash
journalctl -u workbuddy-web | grep -A3 '初始管理员'
```

默认用户 `admin`。**请登录后立即在「设置 → 管理用户」中修改密码。**

### 2. 添加账号

浏览器打开管理端 → 「账号」页 → 「添加账号」→ 用微信 / QQ 扫码。

授权成功后会自动签到、落盘并重载上游，无需手工操作。
（上游的扫码是交互式的，因此无法在脚本里自动化，必须由你扫码一次。）

### 3. 配置 HTTPS 反向代理

公网访问**务必**走 HTTPS，否则会话 Cookie 与密码可被窃听。

**1Panel**：网站 → 创建反向代理 → 目标 `http://127.0.0.1:7864`
→ 申请 Let's Encrypt 证书 → 开启强制 HTTPS。

**Nginx**（手工）：

```nginx
server {
    listen 443 ssl http2;
    server_name wb.example.com;

    ssl_certificate     /path/fullchain.pem;
    ssl_certificate_key /path/privkey.pem;

    client_max_body_size 16m;   # 需大于管理端 8 MiB 的网关请求体上限

    location / {
        proxy_pass http://127.0.0.1:7864;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;   # 必须：真实 IP 来源
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300s;    # 流式对话需较长超时
    }
}
```

> `X-Real-IP` 是管理端判定真实来源 IP 的**首选依据**（`X-Forwarded-For`
> 首段可被客户端伪造）。若前面还叠了 CDN，请把服务端
> `WB_TRUSTED_PROXY_HOPS` 设为 CDN + 反代的层数。

### 4. 子路径部署（可选）

如果域名根路径已被别的站点占用（例如 `https://example.com/` 是另一个系统），
管理端可以挂在子路径下，例如 `https://example.com/workbuddy-manager/`。

需要**两处**同时配置，缺一不可。

**① 把前缀交给前端构建**

宿主机 / 源码部署：

```bash
cd /opt/workbuddy-manager/web
NEXT_PUBLIC_BASE_PATH=/workbuddy-manager npm run build:export
```

容器部署时前端是**在镜像构建阶段**编译的（宿主机上没有 `web/out`），所以前缀要传给
构建而不是运行时：

```bash
docker compose build --build-arg BASE_PATH=/workbuddy-manager
docker compose up -d
```

也可以直接写进 `docker-compose.yml` 的 `build.args.BASE_PATH`（留空 = 根路径部署，与
改动前一致）。

**② 后端与反代对齐前缀**

加到 **systemd unit 的 `Environment=`** 里（容器部署则把 compose 的 `environment:` 里
那行 `WB_BASE_PATH` 取消注释），取值与上面完全一致：

```
WB_BASE_PATH=/workbuddy-manager
```

> **别写进 `.env`。** 服务端只读进程环境变量：仓库里没有任何地方调用 `load_dotenv`，
> `deploy/workbuddy-web.service` 没有 `EnvironmentFile=`，`docker-compose.yml` 用的是
> 内联 `environment:` 而不是 `env_file:`——写进 `.env` 不会生效。
> 这个坑的麻烦之处在于**症状很隐蔽**：配置看起来加了，但前缀没被剥掉，
> 表现为「页面能打开、接口全 404」，容易误判成反代写错了。
>
> （唯一的例外是 Windows 原生部署：`windows\start.ps1` 用 `uvicorn --env-file .env` 显式读取 `.env`。）

**③ 反向代理：两种 `proxy_pass` 写法都可以**

带尾斜杠（反代自己把前缀剥掉）：

```nginx
location /workbuddy-manager/ {
    proxy_pass http://127.0.0.1:7864/;   # 末尾这个 / 就是「剥掉前缀」
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_http_version 1.1;
    proxy_set_header Upgrade    $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 300s;
}
```

不带尾斜杠（反代原样转发，前缀由服务端自己剥掉）：

```nginx
location /workbuddy-manager/ {
    proxy_pass http://127.0.0.1:7864;    # 没有尾斜杠：请求带着前缀进来
    ...同样那几个 header 与超时...
}
```

> 第二种写法值得单独说明：**面板类工具（如 1Panel）新建反向代理时，界面拼出来的
> `proxy_pass` 常常是不带尾斜杠的那种**，而手写配置的人也很容易漏掉那个 `/`。
> 两种都能用的意义是——不必先搞清楚「该不该带尾斜杠」才能配通。

> 网关（`/v1`）会一起挂到前缀下：接入地址是
> `https://example.com/workbuddy-manager/v1`。密钥页展示的地址会自动带上前缀。

> 症状好认：页面能打开，但随即跳到域名根的 `/login` 并 404 —— 根路径属于另一个
> 站点，自然没有这个路由。这说明 ① 或 ② 漏了一个：前缀只在前端和后端**都**知道
> 的情况下才成立。

### 5. 加固建议

- 用防火墙或安全组只放行 `22 / 80 / 443`；`7863`、`7864` 保持仅本机
- 可在 1Panel 为站点配置 IP 白名单，或叠加 Cloudflare Access
- 定期轮换管理端密码与网关密钥

---

## 四·五、后续更新

部署后有两种更新方式，**都不需要重新执行安装脚本**。

### 方式一：网页一键更新（推荐）

登录后进入 **设置 → 系统更新**，选择：

| 模式 | 作用 | 适用场景 |
|---|---|---|
| **全部更新** | 上游 + 管理端 | 常规升级 |
| **仅上游** | 只更新 workbuddy2api | 上游有修复 / 新模型 |
| **仅管理端** | 只更新本控制台 | 界面或管理功能升级 |

更新在后台执行，页面实时显示进度与日志；期间服务可能短暂重启
（页面会自动重连）。**账号授权、上游配置、密钥与日志数据都会保留。**

> 上游更新会自动把端口绑定重新收敛为 `127.0.0.1`，避免上游仓库里的
> `7863:7863` 覆盖本项目的安全基线。

### 方式二：命令行

```bash
cd /opt/workbuddy-manager
sudo /opt/workbuddy-manager/venv/bin/python deploy/update.py --target both
```

`--target` 取值为 `manager` / `upstream` / `both`。

---

## 五、目录与端口

```
管理端   /opt/workbuddy-manager         :7864   仅本机（经反代对外）
  ├─ server/        后端代码
  ├─ web/out/       前端静态产物
  ├─ data/          SQLite、users.json（含会话密钥）
  └─ venv/          Python 虚拟环境

上游     /opt/workbuddy2api             :7863   仅本机
  ├─ auths/         账号授权文件（每个账号一个 JSON）
  ├─ data/state.json 账号池状态
  └─ config.json    上游配置（含 api_key）
```

---

## 六、常用运维命令

```bash
# 日志
journalctl -u workbuddy-web -f                    # 管理端
cd /opt/workbuddy2api && docker compose logs -f   # 上游

# 重启
systemctl restart workbuddy-web
cd /opt/workbuddy2api && docker compose restart

# 状态
systemctl status workbuddy-web
curl -s http://127.0.0.1:7864/healthz             # 含上游连通性

# 升级
cd /opt/workbuddy-manager && git pull              # 管理端代码
cd /opt/workbuddy2api && git pull && docker compose up -d --build  # 上游
```

---

## 七、卸载

```bash
systemctl disable --now workbuddy-web
rm -f /etc/systemd/system/workbuddy-web.service && systemctl daemon-reload
rm -rf /opt/workbuddy-manager

cd /opt/workbuddy2api && docker compose down
rm -rf /opt/workbuddy2api        # 注意：会删除账号授权文件
```

---

## 八、常见问题

**Q：脚本提示「未检测到 Docker」**
上游只提供 Docker 部署方式。请先安装 Docker 与 compose 插件，或用
`--skip-upstream` 跳过并自行部署上游。

**Q：管理端显示「反代上游不可用」**
上游容器未运行或端口不通。检查：

```bash
cd /opt/workbuddy2api && docker compose ps
curl -s http://127.0.0.1:7863/healthz
```

**Q：account 目录权限报错（Permission denied）**
容器内以 uid 10001 运行，宿主机目录需归属该 uid：

```bash
chown -R 10001:10001 /opt/workbuddy2api/auths /opt/workbuddy2api/data
```

**Q：首次构建上游很慢**
上游是 Go 项目，首次 `docker compose up --build` 需拉取基础镜像并编译，
视网络情况可能需要几分钟，属正常现象。

**Q：构建管理端镜像失败 / 启动后打开页面显示 ERR_CONNECTION_REFUSED**

先确认容器到底有没有起来——「端口拒绝连接」几乎都是**镜像没构建成功**，
而不是服务本身的问题：

```bash
docker compose ps        # 没有 workbuddy-manager 这一行 = 容器没起来
docker compose build     # 重新构建，看真正的报错
```

构建在国内网络下最常见的失败点是**软件源/依赖源不可达**，典型报错：

```
E: Failed to fetch http://deb.debian.org/... 502  Bad Gateway
E: The repository '...' is no longer signed.
SSL: UNEXPECTED_EOF_WHILE_READING        # pip 拉 PyPI 时
```

官方源（`deb.debian.org` / `pypi.org`）走国际 CDN，国内实测约 367 kB/s 且频繁
502。Dockerfile 内置了一次**自动降级**（失败后改用阿里云 / 清华镜像重试），
所以通常能自己建成功，只是会先白等几分钟。想直接跳过这段等待，显式指定镜像源：

```bash
docker compose build \
  --build-arg DEBIAN_MIRROR=mirrors.aliyun.com \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

docker compose up -d
```

也可以把这两个值写进 `docker-compose.yml` 的 `build.args`，之后 `up --build` 就不必再带参数。
可选镜像：`mirrors.aliyun.com` / `mirrors.tuna.tsinghua.edu.cn` / `mirrors.ustc.edu.cn`。

**Q：扫码后账号没出现**
检查上游日志与账号文件：

```bash
docker compose -f /opt/workbuddy2api/docker-compose.yml logs --tail 50
ls -l /opt/workbuddy2api/auths/
```

**Q：打开 `http://127.0.0.1:7863` 显示「404 page not found」，是不是装坏了？**

**不是，这是正常现象。** 7863 是**上游网关 workbuddy2api**，它是一个**纯 API 服务，
本身没有网页界面**——上游的定位就是「网关核心保持精简，不内嵌 Web 管理面板」，
因此根路径没有任何页面，访问任意非接口路径都会返回 Go 默认的 `404 page not found`。

两个端口的分工：

| 地址 | 是什么 | 怎么用 |
|---|---|---|
| `127.0.0.1:7864` | **WorkBuddy Manager（本管理端）** | 用浏览器打开，账号 / 密钥 / 日志 / 设置都在这里 |
| `127.0.0.1:7863` | 上游网关（workbuddy2api） | 只提供接口，用命令行或下游客户端调用 |

上游真正提供的接口只有这四个：

```bash
# 健康检查（无需鉴权）：healthy / total 即为账号数
curl -s http://127.0.0.1:7863/healthz

# 账号池状态（需带 api_key，脚本部署时由 install.sh 生成）
curl -s -H "Authorization: Bearer <api_key>" http://127.0.0.1:7863/status

# 可用模型
curl -s -H "Authorization: Bearer <api_key>" http://127.0.0.1:7863/v1/models

# 对话补全（OpenAI 兼容，下游客户端用这个）
POST http://127.0.0.1:7863/v1/chat/completions
```

`api_key` 在上游的 `config.json` 里：

```bash
grep api_key /opt/workbuddy2api/config.json
```

> **想看上游状态的话，不需要开 7863**：管理端已经把它可视化了——
> 「仪表盘 → 反代上游」显示连接状态、健康 / 冷却 / 禁用账号数与粘性会话；
> 「设置 → 上游配置 → 服务信息」显示上游地址与接入密钥（已隐藏）。
> 这正是本管理端存在的意义：把上游的命令行能力变成看得见的界面。

> **顺带一提（安全）**：管理端默认只绑 `127.0.0.1:7864`，而上游默认监听
> `0.0.0.0:7863`——也就是**公网可直接访问上游端口**（仍需 api_key，但少一层
> 暴露少一类风险）。
>
> 想收紧的话，**别直接照抄「改成 127.0.0.1」**：上游能不能只绑回环，取决于
> 管理端怎么连它。
>
> - 管理端**跑在宿主机**（systemd 部署，连 `127.0.0.1:7863`）：可以绑回环。
> - 管理端**跑在容器里**（compose 部署，连 `host.docker.internal:7863`）：
>   这个地址并非回环，绑回环后**可能连不上上游**。实测 Docker Desktop
>   （Windows / macOS）会把 `host.docker.internal` 转发到宿主回环、连得上；
>   而 Linux 上容器访问宿主回环通常**不通**。
>
> 更可靠的做法有两条，按你的部署形态选：
>
> **① 让两个容器进同一个 Docker 网络（推荐，最干净）**
> 这样管理端用**服务名**直连上游，上游**根本不需要对外发布端口**：
>
> ```yaml
> # 管理端 compose：把上游地址写成服务名
> WB2API_BASE: http://workbuddy2api:7863
> ```
>
> 然后删掉上游 compose 里的 `ports: ["7863:7863"]`（或改成
> `"127.0.0.1:7863:7863"` 只留给宿主调试）。本仓库 compose 里
> 已经把这个地址作为推荐值写在注释里了。
>
> **② 只绑到内网网卡**
> 把上游的端口映射从 `"7863:7863"` 改成 `"<服务器内网IP>:7863:7863"`，
> 公网访问不到，而管理端仍连得上。
>
> ⚠️ 别指望用 `ufw deny 7863` 之类的**宿主机防火墙规则**去挡：Docker 发布端口
> 走的是 iptables 的 `DOCKER` 链，会绕过 ufw 的 `INPUT` 规则，看着配了实际没生效。
>
> 无论用哪种方式，改完都回管理端「仪表盘 → 反代上游」确认仍是「正常」——
> 别只看端口在监听。

**Q：公网访问点「添加账号」二维码加载不出来**
管理端需要访问腾讯接口生成授权链接，请确认服务器可访问外网。

---

## 附：Windows 原生部署（无需 Docker）

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
powershell -ExecutionPolicy Bypass -File .\windows\service-tools.ps1 start
```

用 `windows\service-tools.ps1 status|restart|stop` 管理后台进程。

> Windows 的启动脚本都在 `windows\` 目录里，**不要把它们挪出去**：脚本按自己所在
> 目录的上一级当项目根（找 `server/`、`web/out`、`.env`、`upstream/`）。双击
> `windows\start.cmd` 前台启动（关窗口即停）、`stop.cmd` 停止、`update.cmd` 一键更新；
> 后台常驻用 `windows\service-tools.ps1 start`。Windows 原生模式支持
保存配置后重启上游及读取上游日志；网页一键更新依赖 Linux/Docker，当前会明确拒绝，
请手动更新代码后重启服务。

> **启停脚本优先用上游自带的**：上游 `workbuddy2api` 2026-09-18 起自带
> `start/stop/status-workbuddy2api.cmd`（PID 文件 + 进程路径校验，不会误杀同名进程），
> 直接把 `WB2API_START_SCRIPT` / `WB2API_STOP_SCRIPT` 指过去即可。若你的上游目录里
> 没有这三个文件（旧版上游），`windows-native/` 下有一对可直接改用的模板。

> **两个约定必须满足**（上游脚本已满足）：启动脚本要**立即返回**（前台运行会让
> 「重启」等到超时才报失败），日志要写到 `WB2API_LOG_FILE`（否则「任务记录」读不到
> 自动任务日志）。细节见 `windows-native/README.md`。
