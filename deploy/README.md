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

# 2) 上游已手工装好，只想装面板
sudo bash deploy/install.sh --skip-upstream
```

优先级：`UPSTREAM_SRC` → 发布包自带的 `upstream/` → 目标目录里已有的完整源码。
上游已由本项目维护，不再从外部 git 仓库克隆。`UPSTREAM_DIR` 默认 `/opt/workbuddy2api`。

### 以后怎么更新上游代码

用面板里的**一键更新**即可 —— 它会把包内那份上游源码同步进 `/opt/workbuddy2api`
（只增改，不动 `config.json` / `auths/` / `data/`），**有变化才重建容器**。
随包分发的上游没有 `git pull` 可拉；要改代码请改本地那份（或用 `UPSTREAM_SRC`
覆盖一份新的），再：

```bash
cd /opt/workbuddy2api && docker compose up -d --build
```

这三样务必保留：`config.json`（含 `api_key`）、`auths/`（账号授权）、`data/`。

### 直接用预构建的上游镜像（不想本地构建时）

每次发版也会把**上游**构建成镜像推到 GHCR。它和管理端镜像**同源**：都用发布包里
那份源码（= `upstream-src` 资产 + 我们的全部补丁），所以不会出现「第三方上游镜像
与项目上游源码不一致」的版本漂移。

```bash
docker pull ghcr.io/ithtelab/workbuddy2api:<版本>
# latest 指向最近一次发布；长期跑建议钉版本号（理由同管理端镜像）
```

要跑多个上游（分组 / 多号池）时，把各自目录里 compose 的 `build: .` 换成 `image:`
即可，端口与卷不变：

```yaml
services:
  wb2api:
    image: ghcr.io/ithtelab/workbuddy2api:2.8.0
    container_name: workbuddy2api
    restart: unless-stopped
    environment:
      - TZ=Asia/Shanghai
    ports:
      - "127.0.0.1:7863:7863"
    volumes:
      - ./auths:/app/auths
      - ./data:/app/data
      - ./config.json:/app/config.json:ro
```

三点要知道：

- 镜像里**不带**真实配置（只有 `config.example.json`），`config.json` 必须自己挂；
  首次使用可从发布包的 `upstream/config.example.json` 复制一份再改。
- 镜像是**多架构**（amd64 / arm64），与管理端镜像一致。
- 用镜像之后，上游就**不再由面板的一键更新维护**（那条路同步源码并
  `docker compose up -d --build`）：升级上游 = 改 `image:` 的版本号 +
  `docker compose up -d`。面板「全部更新」里针对上游源码的那一步对镜像部署无效，
  这是预期行为。
- 镜像自带 `dev.workbuddy.upstream-id` label，面板读运行中容器的这个 label 判断
  「镜像部署且与本次发布同源」，同源时不会提示「随包上游与本机不一致」。读不到
  label 的旧镜像（≤ 2.8.2）仍按部署目录的 `.upstream-id` 判定，行为不变。

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

#### 前面是 Cloudflare（或任何 CDN）时**必须**再配一步

只把 `X-Real-IP` 设成 `$remote_addr` 在 CF 后面是不够的：请求经过 CDN 时
`$remote_addr` 是**边缘节点**的地址（`162.159.x` / `172.68.x` …），于是应用侧的
全局 IP 白/黑名单、登录失败锁定、密钥 IP 白名单、审计日志全部按「CDN 的 IP」
记账 —— 所有用户看起来都是同一个来源，IP 类管控既拦不准也锁不准（实测线上：
`request_logs` 访问量前几名全是 CF 网段，单条 1.8 万次）。要用 nginx 自带的
`realip` 模块从 `CF-Connecting-IP` 还原真实来源：

```bash
{
  curl -fsS https://www.cloudflare.com/ips-v4
  curl -fsS https://www.cloudflare.com/ips-v6
  echo
  echo 'real_ip_header CF-Connecting-IP;'
  echo 'real_ip_recursive on;'
} | sed -E '/^[0-9a-fA-F:.]+\/[0-9]+$/ { s/^/set_real_ip_from /; s/$/;/ }' \
  | sudo tee /etc/nginx/conf.d/cloudflare-realip.conf

sudo nginx -t && sudo systemctl reload nginx
```

这个文件由 `nginx.conf` 里 `http {}` 中的 `include conf.d/*.conf` 加载，对所有
站点生效。要点：

- **`set_real_ip_from` 必须限定为 CDN 网段**。绕过 CDN 直连源站的请求，TCP
  对端不匹配这些网段，伪造的 `CF-Connecting-IP` 就不会被采信。
- CF 网段会变（官方列表会增减），建议把上面那段放进 cron（例如每月一次）后
  跟 `nginx -t && systemctl reload nginx`，别只跑一次就忘了。
- 应用侧**不用改**：`X-Real-IP` 仍由 nginx 覆盖成 `$remote_addr`，此时它已是
  真实客户端 IP；`WB_TRUSTED_PROXY_HOPS` 也不必上调 —— 那是给「直接采信
  `X-Forwarded-For`」的场景算层数用的。

验证：`sudo tail -1 /var/log/nginx/access.log` 的第一列应是真实客户端 IP；
或者不带密钥请求一次 `/v1/usage`（会返回 401），管理端「安全 → IP 访问日志」
里新增那条的来源 IP 应是你的公网 IP，而不是 CF 网段。

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

> **这一项（`WB_BASE_PATH`）别写在 `.env` 里**：它在构建期与前缀绑定，必须落在
> systemd 的 `Environment=` / compose 的 `environment:` 这类**显式变量**里，
> 症状很隐蔽 —— 配置看起来加了，但前缀没被剥掉，表现为「页面能打开、接口全 404」，
> 容易误判成反代写错了。
>
> 其余可选项（出口代理 `WB_HTTP_PROXY`、更新代理 `WB_UPDATE_PROXY`、GitHub 镜像
> `WB_GITHUB_MIRROR`、新账号默认线路
> `WB_DEFAULT_ACCOUNT_PROXY` 等）**写进安装目录的 `.env` 即可**：容器部署由 compose 的
> `${}` 取过去，systemd 部署由单元里的 `EnvironmentFile=` 读进来，Windows 原生由
> 启动脚本交给面板（`uvicorn --env-file .env`）。

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

部署后有两种更新方式，**都不需要重新执行安装脚本**。（下面两条都是按**宿主安装**
写的；容器部署请看本节末尾的「容器（Docker Compose）部署的更新」。）

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

### 国内网络：用镜像加速更新

直连 GitHub 超时、发布包下载卡在 0% 时，可以填一个 GitHub 加速服务的基址
（issue #216）。两种填法，任选其一：

```bash
# ① 面板：设置 → 系统更新 → GitHub 镜像加速，填 https://gh-proxy.com/
# ② 命令行 / .env（面板里没填时生效）
WB_GITHUB_MIRROR=https://gh-proxy.com/
```

版本检测与发布包下载都会**先走镜像、失败自动回退直连**，所以镜像临时不可用也
不会把更新卡死。程序会自动拼成 `https://gh-proxy.com/https://github.com/...`
这种「基址 + 完整原始地址」的形式，不用自己拼。

> **镜像会把请求转给第三方**，因此走镜像时**不会发送 GitHub Token**。下载公开
> 仓库的 Release 本身不需要认证；如果你配镜像的目的是绕开 API 限流，请注意这条
> 路径用的是镜像自己的配额。不放心的话，用 `WB_UPDATE_PROXY` 配一个自己能控制的
> 代理，效果类似且流量不经过第三方。
>
> 加速服务的可用性由服务方决定。换了服务只要改基址即可（`gh-proxy.org` 是同一家
> 的另一个域名）；留空 = 全部直连。

### 容器（Docker Compose）部署的更新

上面两种方式都是按宿主安装写的（方式二用的是宿主上的 venv）。容器部署用面板里的
「一键更新」即可；想在容器里跑命令行也行：

```bash
docker compose exec workbuddy-manager python /app/deploy/update.py --target both
```

> 这条命令结束时**会按设计终止容器**（更新进程发信号给 PID 1，再由 compose 的
> `restart: unless-stopped` 用新代码拉起），所以 `exec` 会跟着断开 —— 这是正常的。

**更新的代码落在哪**（决定了什么操作会让它「退回旧版」）：

| 内容 | 位置 | `docker restart` | 重建容器（`up -d --build`） |
|---|---|---|---|
| 管理端代码 | 容器可写层 `/app/server`、`/app/web/out` | 保留 | **退回镜像里的版本** |
| 更新时新装的依赖 | `/app/.local`（`PYTHONUSERBASE`） | 保留 | **丢失** |
| 数据库 / 日志 / 配置 | 宿主 `./data`（bind mount） | 保留 | 保留 |

所以**更新之后不要对管理端执行 `docker compose up -d`** —— 那会用镜像里的旧代码
重建容器。要重启用 `docker restart workbuddy-manager`；要升级镜像本身，走下面这步。

#### 一次性：离线更新（也是「更新后打不开面板」的救法）

**更新器本身也在被更新的代码里** —— 这一版跑的仍是上一版的更新逻辑。所以这两种
情况必须用发布包重建镜像，面板里的按钮帮不上忙：

- 更新后管理端起不来（例如新版本引入了新依赖，而容器里装不上）；
- 你要升级的是**镜像本身**（Dockerfile / compose / 基础依赖有变化）。

```bash
# 在部署目录（有 docker-compose.yml 的地方）
tar xzf workbuddy-manager-vX.Y.Z.tar.gz
cd workbuddy-manager-vX.Y.Z
docker compose up -d --build workbuddy-manager
```

发布包里带 `Dockerfile`、`docker-compose.yml`、`server/`、`web/out/` 与 `upstream/`，
所以这一步既能救砖、也能把镜像升到最新。`./data` 与 `../workbuddy2api` 都是挂载卷，
不会被覆盖。

#### 容器内操作 docker（更新上游 / 重载配置）

容器以 uid 10001 运行，而宿主的 `/var/run/docker.sock` 通常归 `root:docker`
（GID 998/999）—— 不在那个组里就是 `permission denied`。compose 已用 `group_add`
引用 `DOCKER_GID`，把本机的真实值填进 `.env` 即可：

```bash
echo "DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)" >> .env
docker compose up -d workbuddy-manager
```

不配也不会静默失败：更新上游时会提示你去取真实 GID。完全不想要容器操作 docker
的话，把 compose 里挂 `docker.sock` 那一行注释掉即可，相关功能会如实降级。

---

## 五、目录与端口

```
管理端   /opt/workbuddy-manager         :7864   监听 0.0.0.0（经反代对外；建议防火墙收紧）
  ├─ server/        后端代码
  ├─ web/out/       前端静态产物
  ├─ data/          SQLite、users.json（含会话密钥）
  └─ venv/          Python 虚拟环境

上游     /opt/workbuddy2api             :7863   仅本机（安装脚本把端口映射绑到 127.0.0.1）
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
视网络情况可能需要几分钟，属正常现象。不想本地编译就把 compose 的 `build: .`
换成预构建镜像（见「〇、上游源码从哪来 → 直接用预构建的上游镜像」）。

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
> 一键更新会自动处理这个差异：检测到 `WB2API_BASE` 用的是
> `host.docker.internal` 时，把上游端口绑到宿主网桥地址（如 `172.17.0.1`），
> 容器可达且不对公网开放；Docker Desktop 仍用 `127.0.0.1`。要指定别的地址
> （例如只绑内网网卡），在管理端环境变量里设 `WB_UPSTREAM_BIND=192.168.1.10`
> 后重新执行一次更新。
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
