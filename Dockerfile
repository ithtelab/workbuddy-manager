# WorkBuddy Manager 容器镜像（管理端 + 反代网关）
#
# 设计取舍说明（值得先读，因为它解释了为什么容器版有些功能是"降级"的）：
#
# 1. **本镜像不含上游 workbuddy2api**。上游是独立的 Go 服务、自带
#    docker-compose.yml 与 auths/data 卷，硬塞进来会让两边的升级互相牵制。
#    推荐做法：两边分别用 compose 起，管理端通过 WB2API_BASE 连接上游。
#
# 2. **容器内的一键更新不能自我重启**。更新进程可以下载并替换代码，但容器里
#    没有 systemd、也不能重启自己所在的容器。所以容器形态下的「更新」是：
#    替换代码 → 结束容器 → 由 compose 的 restart 策略用新代码拉起。
#    这需要 compose 里配 `restart: unless-stopped`（本仓库的 compose 已配好）。
#    新版本的依赖也由它按需补装：镜像里的依赖是 root 装的、site-packages 归 root，
#    而进程是 uid 10001 —— 所以更新时装到**用户站点**（/app/.local，见下面的
#    PYTHONUSERBASE 与 useradd 那一步），而不是往 root 的目录里写。
#
# 3. **挂载 docker.sock 是可选的，默认提供**。挂上它，容器内的管理端就能像
#    宿主机部署那样操作上游容器（重载配置 / 读日志 / 一键更新上游）。
#
#    关于安全性的一次修正：初版这里写的是"挂了等于把宿主 root 交给容器，比
#    少一个功能危险得多"，**这个说法不准确**。事实是——宿主部署时本服务
#    **本来就是以 root 运行的**（systemd 单元无 User=，安装脚本要求 root），
#    而 root 的宿主进程本来就能 `docker run -v /:/host` 拿到宿主文件系统。
#    也就是说，宿主部署的权限**已经等价于**挂 docker.sock，两者并无本质高下。
#    所以挂上它只是让容器版与宿主版能力对齐，而不是引入一个新的风险等级。
#
#    若你的威胁模型要求最小权限，把 compose 里那行卷注掉即可：此时依赖 docker
#    的功能会自动降级为「请到宿主机操作」，界面会如实提示（不会静默失败）。
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
FROM node:20-slim AS web-builder

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

# ── 主镜像 ────
FROM python:3.12-slim

# 环境变量：Python 不要写 pyc（容器是一次性的，写了也没用）、日志不缓冲
#
# PYTHONUSERBASE 指到 /app/.local：容器里以 uid 10001 运行，而依赖是 root 装的、
# site-packages 归 root，**更新时装新依赖只能走用户站点**。钉死这个路径有两个理由：
# ① 不依赖 Docker 把 HOME 设对；② /app 已经 chown 给 10001（见下方 useradd 那步），
# 目录必须**先存在**——CPython 的 site.py 只在用户站点目录存在时才把它加进 sys.path。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WB_RUN_MODE=docker \
    WB_MANAGER_HOST=0.0.0.0 \
    WB_MANAGER_PORT=7864 \
    WB_INSTALL_DIR=/app \
    WB_DATA_DIR=/app/data \
    WB_STATIC_DIR=/app/web/out \
    WB_AUTH_DIR=/opt/workbuddy2api/auths \
    WB_UPSTREAM_CONFIG=/opt/workbuddy2api/config.json \
    WB2API_BASE=http://127.0.0.1:7863 \
    WB_BASE_PATH="" \
    PYTHONUSERBASE=/app/.local

# 可选：Debian 软件源镜像（留空 = 官方源 deb.debian.org）。
#
# 为什么需要：官方源走 Fastly CDN，中国大陆访问实测**频繁 502 且极慢**
# （约 367 kB/s，常在下载某个 .deb 时中断），表现为构建在第 2 步就失败、
# 管理端镜像根本建不出来——而用户看到的是「打开 7864 连接被拒绝」。
# 换国内镜像即可正常构建：
#
#   docker compose build --build-arg DEBIAN_MIRROR=mirrors.aliyun.com
#
# 也可以直接填进 docker-compose.yml 的 build.args。
#
# 为兼容「没显式指定镜像源」的场景，这里**带一次自动降级**：官方源失败时
# 自动改用阿里云镜像重试；两者都失败才让构建失败，并把原因写清楚。
ARG DEBIAN_MIRROR=""

# git：一键更新要 git fetch；curl：健康检查与容器健康探针
# openssh-client：发布包验签（ssh-keygen -Y verify 需要 OpenSSH 8.0+）
# docker-cli：让「保存设置后重载上游」「上游日志」「更新上游」在本容器内可用
#   （需要挂 /var/run/docker.sock，见 docker-compose.yml；不挂则这几项自动降级
#    为"请到宿主机操作"，界面会如实提示，不会静默失败）
#   注意只装 CLI（~50MB），不装 dockerd —— 我们只要控制宿主上的 docker。
#
# set_mirror 替换的是「协议://主机」中的主机部分，而不是写死 deb.debian.org：
# 降级时源里已经没有那个字符串了，只认官方主机名会让第二次替换变成空操作
#（这一点是靠实测发现的）。Debian 12+ 用 deb822 格式的 debian.sources，
# 老版本用 sources.list，两处都要覆盖。
#
# apt-get 的 Retries / Timeout 调小，是为了减少官方源不通时的无谓等待
# （默认重试实测要空等约 200 秒才判定失败）。但主要瓶颈是带宽，真想快
# 仍应显式指定镜像源。
RUN set -eu; \
    set_mirror() { \
        for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
            if [ -f "$f" ]; then \
                sed -i -E "s#(https?://)[^/ ]+#\1$1#g" "$f"; \
            fi; \
        done; \
    }; \
    install_pkgs() { \
        apt-get -o Acquire::Retries=1 -o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15 update \
        && apt-get install -y --no-install-recommends \
            git curl ca-certificates openssh-client; \
    }; \
    if [ -n "${DEBIAN_MIRROR}" ]; then \
        echo "apt: 使用指定软件源 ${DEBIAN_MIRROR}"; \
        set_mirror "${DEBIAN_MIRROR}"; \
    fi; \
    if install_pkgs; then \
        echo "apt: 依赖安装成功"; \
    else \
        echo "apt: 当前软件源失败，改用 mirrors.aliyun.com 重试…" >&2; \
        rm -rf /var/lib/apt/lists/*; \
        set_mirror mirrors.aliyun.com; \
        install_pkgs; \
        echo "apt: 改用国内镜像后安装成功（如需固定，构建时传 DEBIAN_MIRROR 或写入 compose build.args）"; \
    fi; \
    rm -rf /var/lib/apt/lists/*
# docker-cli 走官方静态包（Debian 仓库里的 docker.io 会拖进 dockerd，太重）。
# 静态包的目录名与 Docker 的架构名**并不一致**（amd64→x86_64、arm64→aarch64）：
# 原先这里写死 x86_64，arm64 机器上会装进一个跑不起来的二进制，直到运行时
# 调用 docker 才报「格式错误」。改为按目标架构选包。
# TARGETARCH 由 buildx 按目标平台注入（多架构构建必需）；普通 `docker build`
# 下它为空，退回 uname -m —— 这样在 arm64 机器上直接 `docker compose up --build`
# 也是对的，不强制用户先装 buildx。
ARG DOCKER_CLI_VERSION=27.3.1
ARG TARGETARCH
# 可选：docker 静态包镜像基址。官方源 download.docker.com 走国外 CDN，
# 中国大陆实测下载极慢（构建会长时间卡在这一步）。留空 = 官方源。
# 可选值：https://mirrors.aliyun.com/docker-ce / https://mirrors.tuna.tsinghua.edu.cn/docker-ce
ARG DOCKER_CLI_BASE=""
RUN set -eux; \
    case "${TARGETARCH:-$(uname -m)}" in \
        amd64 | x86_64)  DOCKER_ARCH=x86_64 ;; \
        arm64 | aarch64) DOCKER_ARCH=aarch64 ;; \
        arm | armv7l)    DOCKER_ARCH=armhf ;; \
        *) echo "docker-cli 静态包不支持的架构：${TARGETARCH:-$(uname -m)}" >&2; exit 1 ;; \
    esac; \
    DOCKER_URL="https://download.docker.com/linux/static/stable/${DOCKER_ARCH}/docker-${DOCKER_CLI_VERSION}.tgz"; \
    if [ -n "${DOCKER_CLI_BASE}" ]; then \
        echo "docker-cli: 使用镜像 ${DOCKER_CLI_BASE}"; \
        DOCKER_URL="${DOCKER_CLI_BASE}/linux/static/stable/${DOCKER_ARCH}/docker-${DOCKER_CLI_VERSION}.tgz"; \
    fi; \
    curl -fsSL --retry 5 --retry-delay 2 --retry-all-errors \
        "${DOCKER_URL}" \
        -o /tmp/docker.tgz; \
    tar -xzf /tmp/docker.tgz -C /tmp; \
    mv /tmp/docker/docker /usr/local/bin/docker; \
    chmod +x /usr/local/bin/docker; \
    rm -rf /tmp/docker /tmp/docker.tgz; \
    docker --version

# compose 插件：**必须单独装**，官方 docker 静态包里没有它（实测 tar 清单里
# 只有 docker / dockerd / ctr / containerd*，没有 compose）。
#
# 为什么需要（issue #28）：容器版的「一键更新上游」要在本容器内重建上游容器，
# 而重建靠的就是 compose。原先 update.py 的判据是「docker compose 不可用就退回
# docker-compose」，但这个镜像里**两个都没有** —— 于是必然走进退回分支，
# 报 `FileNotFoundError: 'docker-compose'`（exit 127），更新做到一半失败，
# 而用户看到的只是「重建失败」。
#
# 装在 /usr/local/lib/docker/cli-plugins（官方约定的插件目录），文件名必须是
# `docker-compose`（连字符），`docker compose` 子命令才认得它。
#
# **必须停在 compose v2，不要升到 v5**（发版前自审实测）：
#   v5.0.0 移除了内置 builder，`up --build` 改为调用**外部的 buildx 插件**
#   （`docker-compose/pkg/compose/build_bake.go` 里 `exec.CommandContext` 直接
#   执行 buildx，要求 buildx ≥ 0.17，且**没有回退分支**）。而我们的镜像只装了
#   docker CLI + compose，没有 buildx —— 升到 v5 会让「一键更新上游」重新坏在
#   `up --build` 上（正是 issue #28 报的那个失败）。
#   v2 有 `build_classic.go`（内置 builder）作为回退，所以不需要 buildx。
ARG COMPOSE_VERSION=v2.40.3
# 可选：GitHub Releases 加速前缀。官方 GitHub 下载在国内会超时/极慢。
# 用法是「前缀 + 原始 URL」，例如 https://ghfast.top/ （留空 = 直连 GitHub）
#   https://ghfast.top/https://github.com/... 
ARG COMPOSE_URL_PREFIX=""
RUN set -eux; \
    case "${TARGETARCH:-$(uname -m)}" in \
        amd64 | x86_64)  COMPOSE_ARCH=x86_64 ;; \
        arm64 | aarch64) COMPOSE_ARCH=aarch64 ;; \
        arm | armv7l)    COMPOSE_ARCH=armv7 ;; \
        *) echo "compose 插件不支持的架构：${TARGETARCH:-$(uname -m)}" >&2; exit 1 ;; \
    esac; \
    COMPOSE_URL="https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${COMPOSE_ARCH}"; \
    if [ -n "${COMPOSE_URL_PREFIX}" ]; then \
        echo "compose: 使用加速前缀 ${COMPOSE_URL_PREFIX}"; \
        COMPOSE_URL="${COMPOSE_URL_PREFIX}${COMPOSE_URL}"; \
    fi; \
    mkdir -p /usr/local/lib/docker/cli-plugins; \
    curl -fsSL --retry 5 --retry-delay 2 --retry-all-errors \
        "${COMPOSE_URL}" \
        -o /usr/local/lib/docker/cli-plugins/docker-compose; \
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose; \
    docker compose version

WORKDIR /app

# 先装依赖（利用层缓存：代码改动不必重装依赖）
#
# 可选：PyPI 镜像。与上面的 DEBIAN_MIRROR 同一个成因——官方 PyPI 走国际 CDN，
# 中国大陆访问实测会出现 `SSL: UNEXPECTED_EOF_WHILE_READING`，重试几次才通，
# 赶上抖动就直接构建失败。留空 = 官方源（失败时自动降级到清华镜像）。
#
#   docker compose build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=""
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

# 再拷代码与已构建的前端
COPY server /app/server
COPY --from=web-builder /dist /app/web/out
COPY deploy /app/deploy
COPY CHANGELOG.md README.md /app/

# 非 root 运行。目录归属交给 app 用户，使容器内更新能写回代码目录。
# 注意与上游 auths/data 卷的 uid 对齐：上游容器以 uid 10001 运行，
# 这里用同一 uid 可避免跨容器写同一个卷时的权限问题。
#
# /app/.local 是 PYTHONUSERBASE 的落点（见上面的 ENV）：容器内更新装新依赖时
# 只能写用户站点，且该目录必须**先存在**，否则重启后的新进程不在 sys.path 上找它。
RUN useradd -u 10001 -m -s /bin/bash app \
    && mkdir -p /app/data /app/.local /opt/workbuddy2api \
    && chown -R 10001:10001 /app /opt/workbuddy2api

USER app

EXPOSE 7864

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:7864/api/healthz || exit 1

CMD ["python", "-m", "uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "7864", "--timeout-graceful-shutdown", "20"]
