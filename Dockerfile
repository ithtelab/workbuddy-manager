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
#    PYTHONUSERBASE 与 adduser 那一步），而不是往 root 的目录里写。
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
    WB_DATA_DIR=/app/data \
    WB_STATIC_DIR=/app/web/out \
    WB_AUTH_DIR=/opt/workbuddy2api/auths \
    WB_UPSTREAM_CONFIG=/opt/workbuddy2api/config.json \
    WB2API_BASE=http://127.0.0.1:7863 \
    WB_BASE_PATH="" \
    PYTHONUSERBASE=/app/.local

# 可选：Alpine 软件源镜像（留空 = 官方源）。兼容旧名 DEBIAN_MIRROR。
ARG ALPINE_MIRROR=""
ARG DEBIAN_MIRROR=""
# 容器时区（构建参数）。留空 = 不装 tzdata、系统按 UTC 跑，与 Debian slim 的
# 老行为一致（那边也没装 tzdata）。
#
# ⚠️ **一旦设了 TZ 就必须装 tzdata**：本项目的按日统计（`db.day_of()` 的
# `time.localtime`、SQL 里的 `'localtime'` 修饰符）、密钥每日 Token 限额的
# 复位时刻、任务日报的分桶全都取**本地时区**。Alpine 的 busybox 不带 tzdata，
# 缺了它 `/etc/localtime` 设不上、`localtime` 会静默退回 UTC —— 表现是
# 「时区明明是 +8，统计却按 UTC 分天」，而且不报任何错。
ARG TZ=""
# shadow 提供 groupmod / usermod（busybox 里没有）；su-exec 让入口脚本能以目标
# uid 降权起业务进程。**两样都必须装在最后这个阶段**：跨阶段只能 COPY 文件，
# 阶段里 apk add 的包不会跟过来 —— 曾经把它们装进 bins 阶段，运行阶段就报
# `groupmod: not found`，容器反复重启。
#
# 注释必须写在 RUN 之外：续行中间的 `#` 行会终止续行，把后面的 apk add 变成
# 一条独立指令（dev/check_dockerfile.py 会报「非法指令」）。
RUN set -eu; \
    MIRROR="${ALPINE_MIRROR:-$DEBIAN_MIRROR}"; \
    if [ -n "${MIRROR}" ]; then \
        echo "apk: 使用指定软件源 ${MIRROR}"; \
        sed -i -E "s#(https?://)[^/]+#\1${MIRROR}#g" /etc/apk/repositories; \
    fi; \
    apk add --no-cache git curl ca-certificates shadow su-exec; \
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

# 镜像内容的默认属主：app 用户（uid 10001），使容器内更新能写回代码目录。
# 注意与上游 auths/data 卷的 uid 对齐：上游容器以 uid 10001 运行，
# 这里用同一 uid 可避免跨容器写同一个卷时的权限问题。
# 运行期若 PUID/PGID 不同，入口脚本会把 /app 整棵改成目标属主（见 ENTRYPOINT）。
#
# /app/.local 是 PYTHONUSERBASE 的落点（见上面的 ENV）：容器内更新装新依赖时
# 只能写用户站点，且该目录必须**先存在**，否则重启后的新进程不在 sys.path 上找它。
# Alpine 用 busybox 的 adduser（非 useradd）：-D 不设密码、-u 指定 uid、-s 指定
# shell（用 /bin/sh，Alpine 没有 bash）、-h 指定 home、-G 指定组。
RUN addgroup -g 10001 app \
    && adduser -D -u 10001 -s /bin/sh -h /app -G app app \
    && mkdir -p /app/data /app/.local /opt/workbuddy2api \
    && chown -R 10001:10001 /app /opt/workbuddy2api \
    && chmod +x /app/deploy/docker-entrypoint.sh

# 入口脚本以 root 起、对齐挂载目录属主后立刻 su-exec 降权，业务进程始终是 10001
# 或 PUID 指定的 uid —— 不会以 root 运行。
#
# 为什么不再用 `USER app`：bind mount 的目录属主由宿主机决定（群晖 1026、普通
# Linux 1000、root 建的则是 0），镜像固定 10001 时会 `unable to open database
# file` 然后无限重启。要支持 PUID/PGID 就必须有一个「启动前执行」的时机，而
# USER 指令给不了 —— 它把身份钉死在构建期。
#
# 入口脚本已经 chmod +x；Windows 检出不会破坏它（.gitattributes 把 *.sh 锁 LF）。
ENTRYPOINT ["/app/deploy/docker-entrypoint.sh"]

EXPOSE 7864

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:7864/api/healthz || exit 1

CMD ["python", "-m", "uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "7864", "--timeout-graceful-shutdown", "20"]
