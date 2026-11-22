# WorkBuddy Manager 容器镜像（管理端 + 反代网关）
#
# 设计取舍说明（值得先读，因为它解释了为什么容器版有些功能是"降级"的）：
#
# 1. **本镜像内置上游 workbuddy2api，默认一个容器跑两个服务**（7864 管理端 +
#    7863 账号池服务）。上游是 `CGO_ENABLED=0` 静态编译的 Go 二进制，与
#    python:3.12-alpine 的 musl 基础层天然兼容，放进同一个容器没有额外运行期依赖。
#
#    2026-11 的修订理由：分离部署要求用户先备一份上游源码、再对齐两个容器的 uid、
#    再让它们共享同一个 auths/ —— 任何一项没做对，症状都是「面板有账号、池子里 0 个」
#    或「未找到任务脚本」，而面板自己判断不出是哪一种。合并后这些前提全部消失：
#    两个进程同一个 uid、auths 只有一个属主、7863 就在 127.0.0.1。
#
#    代价是升级粒度变粗：升级账号池服务 = 重建本镜像。构建期用 ARG UPSTREAM_IMAGE
#    钉版本，随管理端一起发版。
#
# 2. **容器内的一键更新不能自我重启**。更新进程可以下载并替换代码，但容器里
#    没有 systemd、也不能重启自己所在的容器。所以容器形态下的「更新」是：
#    替换代码 → 结束容器 → 由 compose 的 restart 策略用新代码拉起。
#    这需要 compose 里配 `restart: unless-stopped`（本仓库的 compose 已配好）。
#    新版本的依赖也由它按需补装：镜像里的依赖是 root 装的、site-packages 归 root，
#    而进程是 uid 10001 —— 所以更新时装到**用户站点**（/app/.local，见下面的
#    PYTHONUSERBASE 与 adduser 那一步），而不是往 root 的目录里写。
#
# 3. **不需要挂 docker.sock**。账号池服务的二进制、任务脚本与配置模板都在本镜像
#    里，没有「从另一个容器里取文件」或「重建另一个容器」的需求。
#
#    旧形态要靠 socket 才能做「重载上游 / 读上游日志 / 一键更新上游」；现在这三件事
#    分别是：同容器内重启进程、读挂载目录里的日志文件、重建本镜像。挂 socket 只会
#    把宿主 root 白白交给一个持有全部账号凭据的 Web 进程，不换来任何能力。
#
# 4. **数据与凭据全部走卷**，不烘进镜像：data/（数据库、日志、更新状态）、
#    以及上游的 auths/ 与 config.json。
#
# 5. **前端由前端构建阶段自己搞定**（见下，修 issue #38）。
#    为什么需要它：`web/out` 是构建产物、被 .gitignore 排除，所以
#    `git clone && docker build .` 拿到的工作区里**没有**它 —— 原先直接
#    `COPY web/out` 会失败在：
#
#      ERROR: failed to build: ... "/web/out": not found
#
#    （issue #38 报的就是这个。发布包用户不受影响，因为包里带着构建好的
#    web/out。）
#
#    现在：有现成产物就直接用（发布包 / CI），没有就在容器内构建
#    （git clone）—— 两种部署都一条命令跑通，用户不必先装 Node。
#
# 6. **基础镜像用 Alpine（musl）+ 多阶段构建**，把镜像压到尽可能小：
#    · 前端阶段用 node:20-alpine，运行阶段用 python:3.12-alpine —— 基础层
#      比 Debian slim 小一个数量级（压缩后约 12MB vs 约 55MB）。
#    · Python 依赖装在独立的 deps 阶段（带临时编译工具兜底 musl 缺 wheel 的
#      情况），只把 site-packages 拷进运行镜像 —— 编译器、pip 缓存都不进成品。
#    · docker CLI 与 compose 插件是 Go 静态二进制（与 musl/glibc 无关），在
#      bins 阶段用 strip 去掉调试符号后再拷进来 —— 这两个二进制是体积大头，
#      strip 通常能省 30%~50%。
#    · 移除了 openssh-client：离线私钥签名已于 2026-10-14 取消，不再需要
#      ssh-keygen -Y verify（见 docs/release-signing.md）。
#    关键依赖（psycopg-binary / uvloop / pydantic-core 等）均有 musllinux_1_2
#    的 cp312 预编译 wheel（x86_64 与 aarch64 都齐），所以 Alpine 上无需源码
#    编译即可安装；deps 阶段的编译工具只是万一某天某个传递依赖没 wheel 时的兜底。

# ── 账号池服务阶段 ───────────────────────────────────────
#
# 把上游镜像的运行时资产整体搬进运行镜像：Go 二进制（静态编译）、签到/登录/领奖
# 等辅助二进制、shell 脚本体、Python 任务脚本、配置模板。
#
# 为什么单开一个阶段而不是直接把镜像名当成 COPY 的来源：那种写法只接受阶段名
# 或字面镜像名，**不接受变量**，而 FROM 接受 —— 所以先把可覆盖的镜像建成阶段。
#
# 版本策略：默认 latest，发版时 CI 传与本次一致的 tag；想钉死就
#   docker build --build-arg UPSTREAM_IMAGE=ghcr.io/ithtelab/workbuddy2api:v1.4.2 .
ARG UPSTREAM_IMAGE=ghcr.io/ithtelab/workbuddy2api:latest
FROM ${UPSTREAM_IMAGE} AS upstream

# ── 前端构建阶段 ─────────────────────────────────────────
#
# 这一步的存在是为了修 issue #38：`git pull && docker build .` 会失败在
# `COPY web/out: not found` —— 因为 web/out 是构建产物、被 .gitignore 排除，
# clone 出来的工作区里没有它。
#
# 但两种部署形态下 web/ 目录**都存在**，只是内容不同：
#   · git clone：有源码，没有 out/
#   · 发布包 / CI：有 out/（已构建），没有源码
#
# 所以这里 `COPY web/ /src/` 两种情况都不会失败，再由下一步在**构建期**
# 判断走哪条路 —— 避免了「条件 COPY」这个 Docker 不支持的写法。
#
#   web/out 已存在 → 直接用它（发布包与 CI 走这条，秒过）
#   否则           → 容器内 npm ci + next build（git clone 走这条，
#                    用户不必先自行构建前端）
FROM node:20-alpine AS web-builder

WORKDIR /src

# 可选：npm 依赖镜像。与 Debian/PyPI 同一个成因——中国大陆访问官方 registry
# 实测缓慢且会断。留空 = 官方源（失败时自动降级到 npmmirror）。
#   docker compose build --build-arg NPM_REGISTRY=https://registry.npmmirror.com
ARG NPM_REGISTRY=""
# 子路径部署前缀（例如 /workbuddy-manager）。留空 = 部署在根路径。
# 构建期写进 NEXT_PUBLIC_BASE_PATH，Next 会把它内联进前端产物：
# 所有资源引用与 axios 请求都会带上该前缀。
ARG BASE_PATH=""
ENV NEXT_PUBLIC_BASE_PATH=$BASE_PATH

COPY web/ /src/

RUN set -eu; \
    if [ -f /src/out/index.html ]; then \
        echo "前端：使用已有的 web/out，跳过构建"; \
        cp -r /src/out /dist; \
    else \
        echo "前端：工作区没有构建产物，在容器内构建（git clone 形态）"; \
        apk add --no-cache libc6-compat; \
        mkdir -p /build && cp -r /src/. /build/; \
        cd /build && rm -rf out .next; \
        npm_ci() { npm ci --no-audit --no-fund ${1:+--registry="$1"}; }; \
        if [ -n "${NPM_REGISTRY}" ]; then \
            echo "npm: 使用指定源 ${NPM_REGISTRY}"; \
            npm_ci "${NPM_REGISTRY}"; \
        elif npm_ci ""; then \
            echo "npm: 依赖安装成功（官方源）"; \
        else \
            echo "npm: 官方源失败，改用 registry.npmmirror.com 重试…" >&2; \
            rm -rf node_modules; \
            npm_ci https://registry.npmmirror.com; \
            echo "npm: 改用镜像后安装成功（如需固定，构建时传 NPM_REGISTRY）"; \
        fi; \
        NEXT_OUTPUT_EXPORT=1 npx --no-install next build; \
        test -f out/index.html || { echo "前端构建失败：缺少 out/index.html" >&2; exit 1; }; \
        cp -r out /dist; \
    fi; \
    test -f /dist/index.html

# ── Python 依赖阶段 ───────────────────────────────────────
#
# 只负责把依赖装进系统 site-packages，随后由运行镜像 COPY 过去。
# 好处：编译工具（build-base 等）只存在于这个中间层，不进最终镜像。
FROM python:3.12-alpine AS deps

# 可选：Alpine 软件源镜像（留空 = 官方源 dl-cdn.alpinelinux.org）。
# 中国大陆访问官方源偶有卡顿，填 mirrors.aliyun.com 可绕开。
#   docker compose build --build-arg ALPINE_MIRROR=mirrors.aliyun.com
ARG ALPINE_MIRROR=""
# 兼容旧参数名：以前叫 DEBIAN_MIRROR，很多用户 compose 里留着
# mirrors.aliyun.com 这类值 —— 阿里云/清华同样提供 alpine 源，直接复用。
ARG DEBIAN_MIRROR=""
# 可选：PyPI 镜像。官方源走国际 CDN，国内会出现 SSL EOF / 重试。
#   docker compose build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=""

RUN set -eu; \
    MIRROR="${ALPINE_MIRROR:-$DEBIAN_MIRROR}"; \
    if [ -n "${MIRROR}" ]; then \
        echo "apk: 使用指定软件源 ${MIRROR}"; \
        sed -i -E "s#(https?://)[^/]+#\1${MIRROR}#g" /etc/apk/repositories; \
    fi; \
    apk add --no-cache build-base libffi-dev openssl-dev

# build-base 仅用于「某个依赖没有 musllinux wheel」时的源码编译兜底；
# 绝大多数情况下 pip 直接拉预编译 wheel，用不到它，但它不进最终镜像。
COPY server/requirements.txt /app/server/requirements.txt
RUN set -eu; \
    pip_install() { \
        pip install --no-cache-dir --retries 5 --timeout 60 \
            --index-url "$1" -r /app/server/requirements.txt; \
    }; \
    if [ -n "${PIP_INDEX_URL}" ]; then \
        echo "pip: 使用指定镜像源 ${PIP_INDEX_URL}"; \
        pip_install "${PIP_INDEX_URL}"; \
    elif pip_install https://pypi.org/simple; then \
        echo "pip: 依赖安装成功（官方源）"; \
    else \
        echo "pip: 官方源失败，改用清华镜像重试…" >&2; \
        pip_install https://pypi.tuna.tsinghua.edu.cn/simple; \
        echo "pip: 改用镜像后安装成功（如需固定，构建时传 PIP_INDEX_URL 或写入 compose build.args）"; \
    fi

# ── Docker 工具阶段 ───────────────────────────────────────
#
# docker CLI 与 compose 插件都是 Go 静态二进制，和 musl/glibc 无关，可直接在
# Alpine 里跑。这里下载后 strip 掉调试符号（体积大头），再拷进运行镜像。
# 只装 CLI（约 50MB 未 strip），不装 dockerd —— 我们只要控制宿主上的 docker。
FROM alpine:3.21 AS bins

ARG DOCKER_CLI_VERSION=27.3.1
ARG TARGETARCH
# 可选：docker 静态包镜像基址。download.docker.com 走国外 CDN，国内实测很慢。
# 可选值：https://mirrors.aliyun.com/docker-ce / https://mirrors.tuna.tsinghua.edu.cn/docker-ce
ARG DOCKER_CLI_BASE=""
# compose 插件：**必须单独装**，官方 docker 静态包里没有它（实测 tar 清单里
# 只有 docker / dockerd / ctr / containerd*，没有 compose）。
#
# 为什么需要（issue #28）：容器版的「一键更新上游」要在本容器内重建上游容器，
# 而重建靠的就是 compose。原先 update.py 的判据是「docker compose 不可用就退回
# docker-compose」，但这个镜像里**两个都没有** —— 于是必然走进退回分支，
# 报 `FileNotFoundError: 'docker-compose'`（exit 127），更新做到一半失败。
#
# **必须停在 compose v2，不要升到 v5**：v5 移除了内置 builder，`up --build`
# 改为调用外部 buildx 插件且无回退分支；我们只装了 CLI + compose，升到 v5 会让
# 「一键更新上游」重新坏在 `up --build` 上（正是 issue #28 的那个失败）。
ARG COMPOSE_VERSION=v2.40.3
# 可选：GitHub Releases 加速前缀，用法是「前缀 + 原始 URL」，例如 https://ghfast.top/。
ARG COMPOSE_URL_PREFIX=""
# 下载后会 strip 掉这两个 Go 二进制的调试符号（对功能无影响，显著减小镜像）。
# 本阶段只要 curl（下载）与 binutils（strip）；su-exec / shadow 是运行阶段的事，
# 装在这里既不生效，还会让「是否装了」的检查误判成已完成（踩过一次）。
RUN set -eux; \
    apk add --no-cache curl binutils; \
    case "${TARGETARCH:-$(uname -m)}" in \
        amd64 | x86_64)  DOCKER_ARCH=x86_64;  COMPOSE_ARCH=x86_64 ;; \
        arm64 | aarch64) DOCKER_ARCH=aarch64; COMPOSE_ARCH=aarch64 ;; \
        arm | armv7l)    DOCKER_ARCH=armhf;   COMPOSE_ARCH=armv7 ;; \
        *) echo "不支持的架构：${TARGETARCH:-$(uname -m)}" >&2; exit 1 ;; \
    esac; \
    DOCKER_URL="https://download.docker.com/linux/static/stable/${DOCKER_ARCH}/docker-${DOCKER_CLI_VERSION}.tgz"; \
    if [ -n "${DOCKER_CLI_BASE}" ]; then \
        echo "docker-cli: 使用镜像 ${DOCKER_CLI_BASE}"; \
        DOCKER_URL="${DOCKER_CLI_BASE}/linux/static/stable/${DOCKER_ARCH}/docker-${DOCKER_CLI_VERSION}.tgz"; \
    fi; \
    curl -fsSL --retry 5 --retry-delay 2 --retry-all-errors "${DOCKER_URL}" -o /tmp/docker.tgz; \
    tar -xzf /tmp/docker.tgz -C /tmp; \
    install -m 0755 /tmp/docker/docker /usr/local/bin/docker; \
    rm -rf /tmp/docker /tmp/docker.tgz; \
    COMPOSE_URL="https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${COMPOSE_ARCH}"; \
    if [ -n "${COMPOSE_URL_PREFIX}" ]; then \
        echo "compose: 使用加速前缀 ${COMPOSE_URL_PREFIX}"; \
        COMPOSE_URL="${COMPOSE_URL_PREFIX}${COMPOSE_URL}"; \
    fi; \
    mkdir -p /usr/local/lib/docker/cli-plugins; \
    curl -fsSL --retry 5 --retry-delay 2 --retry-all-errors \
        "${COMPOSE_URL}" -o /usr/local/lib/docker/cli-plugins/docker-compose; \
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose; \
    strip --strip-all /usr/local/bin/docker /usr/local/lib/docker/cli-plugins/docker-compose; \
    docker --version; \
    docker compose version

# ── 主镜像 ────────────────────────────────────────────────
FROM python:3.12-alpine

# 环境变量：Python 不要写 pyc（容器是一次性的，写了也没用）、日志不缓冲
#
# PYTHONUSERBASE 指到 /app/.local：容器里以 uid 10001 运行，而依赖是 root 装的、
# site-packages 归 root，**更新时装新依赖只能走用户站点**。钉死这个路径有两个理由：
# ① 不依赖 Docker 把 HOME 设对；② /app 已经 chown 给 10001（见下方 adduser 那步），
# 目录必须**先存在**——CPython 的 site.py 只在用户站点目录存在时才把它加进 sys.path。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WB_RUN_MODE=docker \
    WB_MANAGER_HOST=0.0.0.0 \
    WB_MANAGER_PORT=7864 \
    WB_INSTALL_DIR=/app \
    WB_DATA_DIR=/opt/workbuddy/manager \
    WB_STATIC_DIR=/app/web/out \
    WB_AUTH_DIR=/opt/workbuddy/workbuddy2api/auths \
    WB_UPSTREAM_CONFIG=/opt/workbuddy/workbuddy2api/config.json \
    WB2API_BASE=http://127.0.0.1:7863 \
    WB2API_EMBEDDED=1 \
    WB_BASE_PATH="" \
    PYTHONUSERBASE=/app/.local

# 可选：Alpine 软件源镜像（留空 = 官方源）。兼容旧名 DEBIAN_MIRROR。
ARG ALPINE_MIRROR=""
ARG DEBIAN_MIRROR=""
# 容器时区（构建参数）。**默认 Asia/Shanghai**：本项目的按日统计（`db.day_of()`
# 的 `time.localtime`、SQL 里的 `'localtime'` 修饰符）、密钥每日 Token 限额的复位
# 时刻、任务日报的分桶全都取**本地时区**；镜像不带 tzdata 时 Alpine 会静默退回
# UTC，表现为「时区明明是 +8，统计却按 UTC 分天」，且不报任何错。
#
# 所以这里不能留空等运行期设 TZ —— `docker run -e TZ=...` 只改环境变量，没 tzdata
# 照样退回 UTC。想用 UTC 就显式传 --build-arg TZ=UTC。
ARG TZ="Asia/Shanghai"
# 运行阶段的系统包。**su-exec 与 shadow 必须在这里装**：入口脚本降权用 su-exec、
# 按 PUID/PGID 对齐 app 账号用 groupmod/usermod，而 bins 阶段装的那份不会跨阶段
# 拷过来。漏了就是启动即 `groupmod: not found`（set -e 下直接退出 → 反复重启），
# 或修好一半后卡在 `su-exec: not found`。
#
# 注意：下面这个 RUN 是反斜杠续行，**不要在中间插 # 注释** —— 续行会把 `#` 之后
# 的内容整段当注释吃掉，后面的 apk add 就不执行了（结构自检会拦住）。
RUN set -eu; \
    MIRROR="${ALPINE_MIRROR:-$DEBIAN_MIRROR}"; \
    if [ -n "${MIRROR}" ]; then \
        echo "apk: 使用指定软件源 ${MIRROR}"; \
        sed -i -E "s#(https?://)[^/]+#\1${MIRROR}#g" /etc/apk/repositories; \
    fi; \
    apk add --no-cache git curl ca-certificates bash wget su-exec shadow; \
    if [ -n "${TZ}" ]; then \
        echo "时区：${TZ}"; \
        apk add --no-cache tzdata; \
        cp "/usr/share/zoneinfo/${TZ}" /etc/localtime; \
        echo "${TZ}" > /etc/timezone; \
    fi

WORKDIR /app

# 拷入依赖（来自 deps 阶段）与 Docker 工具（来自 bins 阶段）。
# 依赖是预编译 wheel 装出来的纯文件，跨阶段 COPY 后行为一致。
COPY --from=deps /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages

COPY --from=bins /usr/local/bin/docker /usr/local/bin/docker
COPY --from=bins /usr/local/lib/docker/cli-plugins/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose

# 代码与已构建的前端
COPY server /app/server
COPY --from=web-builder /dist /app/web/out
COPY deploy /app/deploy
COPY CHANGELOG.md README.md /app/

# 账号池服务的整套运行时资产（见顶部第 1 条）。两处必须处理的差异：
#   · 上游镜像的 /app/config.json 就是 config.example.json 的内容 —— 改名存起来，
#     避免和运行期真配置混淆（真配置由入口脚本在数据目录里生成）；
#   · 上游的 auths/ 与 data/ 只是空目录 —— 删掉，运行时一律用数据卷里那份，
#     否则脚本按 __file__ 自定位会读到镜像内的空目录。
COPY --from=upstream /app /app/upstream
RUN mv /app/upstream/config.json /app/upstream/config.example.json \
    && rm -rf /app/upstream/auths /app/upstream/data \
    && chmod +x /app/upstream/wb2api /app/upstream/*.sh \
    && test -f /app/upstream/scripts/task_runner.py

# 建 app 用户，默认 uid/gid 10001。入口脚本启动时会按 PUID/PGID 把它改成目标值
# —— 这里的 10001 只是默认值，不是死规定。
#
# /app/.local 是 PYTHONUSERBASE 的落点（见上面的 ENV）：容器内更新装新依赖时
# 只能写用户站点，且该目录必须**先存在**，否则重启后的新进程不在 sys.path 上找它。
# Alpine 用 busybox 的 adduser（非 useradd）：-D 不设密码、-u 指定 uid、-s 指定
# shell（用 /bin/sh，Alpine 没有 bash）、-h 指定 home、-G 指定组。
RUN addgroup -g 10001 app \
    && adduser -D -u 10001 -s /bin/sh -h /app -G app app \
    && mkdir -p /app/data /app/.local /opt/workbuddy2api \
              /opt/workbuddy/manager /opt/workbuddy/workbuddy2api \
    && chmod +x /app/deploy/docker-entrypoint.sh \
    && sed -i 's/\r$//' /app/deploy/docker-entrypoint.sh \
    && chown -R 10001:10001 /app /opt/workbuddy2api /opt/workbuddy

USER app

EXPOSE 7863 7864

# 两个进程都起来才算健康：面板的 /api/healthz + 账号池服务的 /healthz。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS -o /dev/null http://127.0.0.1:7864/api/healthz \
        && curl -fsS -o /dev/null http://127.0.0.1:7863/healthz || exit 1

CMD ["python", "-m", "uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "7864", "--timeout-graceful-shutdown", "20"]
