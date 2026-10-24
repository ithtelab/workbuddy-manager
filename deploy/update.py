#!/usr/bin/env python3
"""WorkBuddy Manager 一键更新 worker。

由管理端后台调用（脱离父进程运行，因此重启服务不会中断更新流程），
也可单独在命令行执行：

    python3 deploy/update.py --target both
    python3 deploy/update.py --target upstream
    python3 deploy/update.py --target manager

设计要点
--------
1. **自包含**：仅用标准库，避免「更新过程中依赖被替换」导致脚本自身失败。
2. **状态外置**：进度写入 JSON 文件，管理端读取该文件展示实时日志。
   （更新会重启管理端，若用 HTTP 流式返回会被中断）
3. **幂等与安全**：上游更新会保留账号文件与配置；并**强制把端口绑定收敛为
   仅本机/容器网桥**，避免 upstream 仓库里的 `7863:7863` 覆盖我们的安全加固。
   容器经 `host.docker.internal` 连上游时绑宿主网桥地址（issue #243）。
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import http.client
import os
import re
import shutil
import signal
import site
import socket
import stat
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def _force_utf8_stdio() -> None:
    """中文 Windows 上重定向 stdout/stderr 默认 cp936（GBK）：print('✓'/'⚠️')
    会 UnicodeEncodeError 直接杀死更新进程（时间戳前缀 11 字符 + ✓ 正是
    'position 11' 报错的来源）。无论由谁启动、stdout 重定向到哪，这里
    自我防御：stdio 强制 UTF-8，个别无法编码的字符以 replace 兜底，
    日志绝不因此中断。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError, OSError):
            pass


_force_utf8_stdio()


# ── 运行环境（与 server/config.py 保持一致的默认值）──────────
def _default_install_dir() -> Path:
    """本脚本可能位于 `deploy/` 或 `server/deploy/`（随 server 替换的那份）。"""
    here = Path(__file__).resolve()
    if here.parent.name == 'deploy' and here.parent.parent.name == 'server':
        return here.parent.parent.parent
    return here.parent.parent


INSTALL_DIR = Path(os.environ.get('WB_INSTALL_DIR') or _default_install_dir())
UPSTREAM_DIR = Path(os.environ.get('WB_UPSTREAM_DIR') or '/opt/workbuddy2api')
UPSTREAM_PORT = int(os.environ.get('WB_UPSTREAM_PORT') or 7863)
MANAGER_PORT = int(os.environ.get('WB_MANAGER_PORT') or 7864)
MANAGER_REPO = os.environ.get('WB_MANAGER_REPO') or 'ithtelab/workbuddy-manager'


def upstream_health_url() -> str:
    """就绪探测用的上游健康检查地址（按部署形态取，不写死回环）。

    容器部署下 manager 与上游是两个容器：宿主把上游端口发布在 127.0.0.1 上，那是
    **宿主机**的回环，manager 容器里没有这个端口——写死回环会让探测整整 90 轮
    connection refused、空等约 3 分钟，最后打一条「未在预期时间内就绪」的假警告
    （上游其实早就起来了）。取值顺序：

      ① `WB_UPSTREAM_HEALTH_URL` —— 整条 URL 显式指定（反代 / 子路径场景）；
      ② `WB2API_BASE` 的 scheme+host —— 面板自己就是用这个地址访问上游的，
         容器部署下它是容器 DNS（如 http://workbuddy2api:7863）；
      ③ 回落 `http://127.0.0.1:{WB_UPSTREAM_PORT}/healthz` —— 宿主机原生部署。

    后两条与面板 server/config.py 同源：探测地址跟着**面板实际怎么连上游**走，
    两边不会再各说各话。
    """
    explicit = (os.environ.get('WB_UPSTREAM_HEALTH_URL') or '').strip()
    if explicit:
        return explicit
    base = (os.environ.get('WB2API_BASE') or '').strip()
    if base:
        try:
            parts = urllib.parse.urlsplit(base if '//' in base else f'http://{base}')
            host = parts.hostname or ''
            port = parts.port  # 端口非法时在这里抛 ValueError
            if parts.scheme and host:
                netloc = f'[{host}]' if ':' in host else host  # IPv6 字面量要带方括号
                if port:
                    netloc = f'{netloc}:{port}'
                return f'{parts.scheme}://{netloc}/healthz'
        except ValueError:
            pass  # 畸形值不致命：更新流程不该因为一个地址拼不出来就中断
    return f'http://127.0.0.1:{UPSTREAM_PORT}/healthz'


def _probe_opener() -> urllib.request.OpenerDirector:
    """健康探测**直连**，不走代理。

    探测问的是「上游进程起来没有」。容器部署里 `WB_HTTP_PROXY` / `WB_UPDATE_PROXY`
    往往是为拉 GitHub 配的（issue #106），而上游在容器内网——把内部地址交给代理
    只会必失败，又变成一条假警告（与写死回环同一类错）。确需经代理才能连上上游的
    部署，可用 `WB_UPSTREAM_HEALTH_URL` 指一个可达地址。
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ── 下载代理（issue #106）──────────────────────────────────────
# 容器里能连上 api.github.com（版本检测走它）却连不上 Release 资产所在的
# github.com / objects.githubusercontent.com 是常见情形——两条链路可达性不同。
# 版本检测成功会把「下载不可达」掩盖成「更新器坏了」，所以这里给一个明确的旋钮。
# 取值顺序：WB_UPDATE_PROXY（更新专用）> WB_HTTP_PROXY（面板的出口代理，
# 用户已经为它配过代理时不必再配一遍）> 环境变量里的 http_proxy / https_proxy
# （urllib 的默认行为）。本机地址由 no_proxy 自动放行。
_RAW_PROXY = (os.environ.get('WB_UPDATE_PROXY')
              or os.environ.get('WB_HTTP_PROXY') or '').strip()
# 面板能吃 socks5（装了 socksio），但更新器用的是 urllib —— 它只认 http/https，
# 把手给 socks5:// 只会得到一条看不懂的报错（而下载失败的报错本来就容易误判）。
# 所以非 http 的取法一律忽略，并在 main() 里记一条日志说明怎么配。
PROXY_UNSUPPORTED = ('' if not _RAW_PROXY
                     or _RAW_PROXY.lower().startswith(('http://', 'https://'))
                     else _RAW_PROXY)
UPDATE_PROXY = '' if PROXY_UNSUPPORTED else _RAW_PROXY
if UPDATE_PROXY:
    # 本机与内网不走代理：更新器要读本地状态文件，代理配置不该把内网也带偏。
    os.environ.setdefault('no_proxy', '127.0.0.1,localhost')
    os.environ.setdefault('NO_PROXY', os.environ['no_proxy'])


def _opener() -> urllib.request.OpenerDirector:
    """按配置构造 opener：设了 WB_UPDATE_PROXY 就用它，否则用环境变量默认。"""
    handlers: list[urllib.request.BaseHandler] = []
    if UPDATE_PROXY:
        handlers.append(urllib.request.ProxyHandler(
            {'http': UPDATE_PROXY, 'https': UPDATE_PROXY}))
    handlers.append(urllib.request.HTTPSHandler())
    return urllib.request.build_opener(*handlers)


def redact_proxy(url: str) -> str:
    """把代理地址里的用户名/密码换成 ***。

    为什么必须做：下载失败时的提示会原样拼进 `UPDATE_PROXY`，而它是
    `http://user:password@host` 形态 —— 这条提示会写进 update-status.json，管理端
    任何登录用户都能读（状态接口不是管理员专用）。凭据只在部署者的 .env 里，
    不该因为一次下载失败就泄露给只读账号。
    """
    if not url:
        return ''
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    if not (parts.username or parts.password):
        return url
    host = parts.hostname or ''
    if parts.port:
        host = f'{host}:{parts.port}'
    return urllib.parse.urlunsplit(
        (parts.scheme, f'***@{host}', parts.path, parts.query, parts.fragment))


def _download_hint(exc: BaseException) -> str:
    """下载失败的**可操作**提示：区分「网络到不了 GitHub」与其它原因。"""
    text = str(exc)
    looks_like_network = any(k in text for k in (
        'timed out', 'Timeout', 'Remote end closed', 'Connection reset',
        'No address associated', 'Temporary failure', 'Name or service',
        'Connection refused', 'unreachable',
    ))
    if not looks_like_network:
        return ''
    if UPDATE_PROXY:
        return (f'（已配置代理 {redact_proxy(UPDATE_PROXY)}，仍连不上 —— 检查代理地址是否'
                f'可从本机访问、以及它是否放行 github.com）')
    if urllib.request.getproxies():
        return '（当前使用的是环境变量里的代理；若代理不可用，可用 WB_UPDATE_PROXY 指定一个）'
    return ('（当前网络似乎无法直连 GitHub。容器/内网环境可在 .env 里设置 '
            'WB_UPDATE_PROXY=http://<宿主IP>:<端口>，或设置镜像 WB_GITHUB_MIRROR='
            'https://gh-proxy.com/ 后重试）')


# ── GitHub 镜像（issue #216）──────────────────────────────────
# 国内机器直连 github.com / api.github.com 经常超时。gh-proxy 这类加速服务的
# 用法是「镜像基址 + 完整原始地址」：
#   https://gh-proxy.com/https://github.com/<owner>/<repo>/releases/download/...
# 只在**用户显式配置**时启用：镜像会把请求转给第三方，这是部署者才能决定的事。
# 同理，GitHub Token 绝不能发给镜像（见 _github_headers）—— 镜像本身也不需要它，
# 它按自己的出口访问 GitHub。
_GITHUB_PREFIXES = (
    'https://github.com/',
    'https://api.github.com/',
    'https://codeload.github.com/',
    'https://raw.githubusercontent.com/',
    'https://objects.githubusercontent.com/',
)


def normalize_mirror(value: str) -> str:
    """把镜像基址规范成 `https://host/`（或带子路径的 `https://host/prefix/`）。

    非法值返回空串 = 不启用。带查询串 / 片段的地址直接拒绝：拼出来的地址必须是
    「基址 + 完整原始地址」，多一个 `?` 或 `#` 都会让后半截被当成镜像自己的参数，
    结果下载到一个错误页（而错误页的大小检查只在极端情况下才拦得住）。
    """
    raw = (value or '').strip()
    if not raw or any(c.isspace() for c in raw):
        return ''
    if '://' not in raw:
        raw = 'https://' + raw
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        return ''
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        return ''
    # 主机段只允许主机名/IP 字面量会出现的字符：`https://not a url` 这类
    # 输入会被 urlsplit 当成合法 hostname，拼出来的地址必然连不上。
    if not re.fullmatch(r'[A-Za-z0-9._\-:\[\]]+', parts.netloc):
        return ''
    if parts.query or parts.fragment:
        return ''
    return f'{parts.scheme}://{parts.netloc}{parts.path.rstrip("/")}/'


GITHUB_MIRROR = normalize_mirror(os.environ.get('WB_GITHUB_MIRROR') or '')


def mirror_url(url: str) -> str:
    """给 GitHub 地址套上镜像前缀；未配置镜像或非 GitHub 地址原样返回。"""
    if not GITHUB_MIRROR or not url:
        return url
    if not url.startswith(_GITHUB_PREFIXES):
        return url
    return f'{GITHUB_MIRROR}{url}'


def is_mirrored(url: str) -> bool:
    """这个地址是不是我们拼出来的镜像地址（用来决定「能不能带凭据」）。"""
    return bool(GITHUB_MIRROR) and bool(url) and url.startswith(GITHUB_MIRROR)


SERVICE_NAME = os.environ.get('WB_SERVICE_NAME') or 'workbuddy-web'
DATA_DIR = Path(os.environ.get('WB_DATA_DIR') or INSTALL_DIR / 'data')
STATUS_FILE = Path(os.environ.get('WB_UPDATE_STATUS') or DATA_DIR / 'update-status.json')
# 运行形态：systemd（宿主机安装）或 docker（容器内运行）。
#
# 为什么必须区分「重启方式」：容器里没有 systemd，`systemctl restart` 只会失败。
# 容器自身的重启必须由**外部**（compose 的 restart 策略）完成——容器里无法重启
# 自己，这是 Docker 的模型决定的，不是缺功能。
# 取 auto：能探测到容器就按容器处理，否则按 systemd。
RUN_MODE = (os.environ.get('WB_RUN_MODE') or 'auto').strip().lower()

STEP_TIMEOUT = int(os.environ.get('WB_UPDATE_STEP_TIMEOUT') or 900)

# 离线升级包（面板「上传更新包」用；issue #153）：指向本机的一份发布包 tar.gz。
# 走这条路时**不查 GitHub**：版本号从包里的 .version 读；完整性核对复用
# verify_download（用户在面板里填了发布页 sha256 的话，这里就一并核对）。
OFFLINE_PACKAGE = (os.environ.get('WB_UPDATE_PACKAGE') or '').strip()
OFFLINE_PACKAGE_SHA256 = (os.environ.get('WB_UPDATE_PACKAGE_SHA256') or '').strip().lower()

# ── 发布包完整性校验 ─────────────────────────────────────
# 更新器从项目的 GitHub Release 拉包（HTTPS），并核对**发布页给的 sha256 摘要**
# 与文件大小 —— 也就是绝大多数开源项目让用户做的那个手工校验，这里自动做了。
# 能挡住的是「下载被截断 / 传输途中被替换」，这也是实测里真会发生的两类问题
# （CDN 与代理链路会截断，截断的 tar.gz 往往解压到一半才报错、报错位置还看不出原因）。
#
# 历史上这里还有一层「维护者离线私钥签名」（OpenSSH 签名 + 代码内嵌公钥），
# 2026-10-14 起取消：它换来的是「GitHub 账号被拿下也拦得住」，代价是每次发版
# 维护者都得在带私钥的机器上手工签一遍 —— 权衡后按主流做法只保留摘要校验。
# 取舍与边界见 docs/release-signing.md。


# ── 状态写入 ─────────────────────────────────────────────
class Reporter:
    def __init__(self, target: str) -> None:
        self.start = time.time()
        self.state: dict = {
            'running': True,
            'ok': None,
            'target': target,
            'step': '初始化',
            'logs': [],
            'started_at': int(self.start),
            'finished_at': None,
            'duration': 0,
            'pid': os.getpid(),
        }
        self.flush()

    def log(self, message: str, level: str = 'info') -> None:
        line = f'[{time.strftime("%H:%M:%S")}] {message}'
        self.state['logs'].append({'ts': int(time.time()), 'level': level, 'text': message})
        # 终端输出便于命令行单独运行
        print(line, flush=True)
        self.flush()

    def step(self, name: str) -> None:
        self.state['step'] = name
        self.log(f'== {name} ==')
        self.flush()

    def finish(self, ok: bool) -> None:
        self.state['running'] = False
        self.state['ok'] = ok
        self.state['finished_at'] = int(time.time())
        self.state['duration'] = round(time.time() - self.start, 1)
        self.flush()

    def set_target_version(self, tag: str) -> None:
        """记录本次要更新到的版本。

        管理端重启会把本进程一并终止（见 update_manager 的说明），
        管理端读到「运行中但进程已不在」时，用这个版本号核对代码是否已就位，
        从而区分「更新成功、只是被重启带走」与「真的崩了」。
        """
        self.state['target_version'] = str(tag or '').strip().lstrip('vV')
        self.flush()

    def set_integrity(self, status: str, digest: str = '') -> None:
        """记录发布包完整性校验结果，供界面显示。

        status: digest（sha256 与发布页一致）/ none（回退路径拿不到发布页摘要，
        只打印了本地摘要供人工比对）。界面据此给一行说明 —— 「这次装的东西
        有没有被核对过」不该只留在日志里。
        """
        self.state['integrity'] = {'status': status, 'digest': digest}
        self.flush()

    def flush(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = STATUS_FILE.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.state, ensure_ascii=False), encoding='utf-8')
            tmp.replace(STATUS_FILE)
        except Exception:  # noqa: BLE001
            pass


# ── 命令执行 ─────────────────────────────────────────────
def run(cmd: list[str], cwd: Path | None = None, rep: Reporter | None = None,
        timeout: int = STEP_TIMEOUT, check: bool = True) -> tuple[int, str]:
    if rep:
        rep.log(f'$ {" ".join(cmd)}')
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        if rep:
            rep.log(f'命令不存在: {exc}', 'error')
        if check:
            raise
        return 127, str(exc)
    except subprocess.TimeoutExpired:
        if rep:
            rep.log(f'命令超时（{timeout}s）', 'error')
        if check:
            raise
        return 124, 'timeout'

    out = (proc.stdout or '') + (proc.stderr or '')
    if rep and out.strip():
        for line in out.strip().splitlines()[-30:]:
            rep.log(f'  {line}')
    if proc.returncode != 0 and check:
        raise RuntimeError(f'命令失败（exit {proc.returncode}）: {" ".join(cmd)}')
    return proc.returncode, out


def _github_headers(url: str = '') -> dict:
    """GitHub API 请求头；设了 WB_GITHUB_TOKEN / GITHUB_TOKEN 则认证。

    未认证限额只有 60 次/小时/IP，共享出口（TUN/系统代理）极易被同出口的
    其他用户打满，表现为偶发 HTTP 403；认证后为 5000 次/小时。

    **走镜像时绝不带 Authorization**（issue #216）：镜像会把请求转给第三方，
    凭据不该为了「下载快一点」而离开本机。镜像按自己的出口访问 GitHub，
    也不需要我们的配额。
    """
    headers = {
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'workbuddy-manager-updater',
    }
    if is_mirrored(url):
        return headers
    token = (os.environ.get('WB_GITHUB_TOKEN')
             or os.environ.get('GITHUB_TOKEN') or '').strip()
    if token:
        headers['Authorization'] = f'Bearer {token}'
    return headers


def _url_candidates(url: str, *, use_mirror: bool = True) -> list[str]:
    """同一个资源的候选地址：镜像优先，直连兜底（未配镜像时只有直连）。"""
    if not use_mirror:
        return [url]
    proxied = mirror_url(url)
    return [proxied, url] if proxied != url else [url]


# 最近一次「镜像失败、已回退直连」的原因。由 http_json 写入、_fetch_release_info
# 读出并记进日志 —— 不记的话，用户明明配了镜像却看到直连被限流的 403，
# 会以为镜像压根没生效。用模块级变量而不是给 http_json 加 rep 参数：
# 测试里有 `http_json = lambda url, timeout=20: …` 这类替换，加参数会破坏它们。
_mirror_fallback_error = ''


def http_json(url: str, timeout: int = 20, *, use_mirror: bool = True) -> dict:
    """取一个 JSON；配了镜像就先走镜像，失败再直连（issue #216）。

    **404 不回退**：镜像明确回答「仓库/Release 不存在」时，直连只会给出同一个
    结论（或更糟 —— 被限流的 403 把真实原因盖掉）。403/429 这类「换条路可能
    就好」的才值得回退。
    """
    global _mirror_fallback_error
    _mirror_fallback_error = ''
    candidates = _url_candidates(url, use_mirror=use_mirror)
    last: BaseException | None = None
    for idx, candidate in enumerate(candidates):
        req = urllib.request.Request(candidate, headers=_github_headers(candidate))
        try:
            with _opener().open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            # 把 GitHub 的可读原因带出来（如 "API rate limit exceeded for x.x.x.x"），
            # 否则调用方只能打出 "HTTP 403"，无从判断是该等配额重置还是换网络
            detail = ''
            try:
                detail = exc.read().decode('utf-8', errors='replace').strip()[:200]
            except Exception:  # noqa: BLE001
                pass
            last = RuntimeError(f'HTTP {exc.code} {exc.reason} {detail}'.strip())
            if exc.code == 404:
                raise last from exc
        except (OSError, http.client.HTTPException, ValueError) as exc:
            # 网络不通、镜像返回的不是 JSON（错误页 / 维护公告）都算这条候选失败
            last = exc
        if idx + 1 < len(candidates):
            _mirror_fallback_error = str(last)
    assert last is not None
    raise last


def download(url: str, dest: Path, rep: Reporter) -> None:
    """下载 Release 资产到 dest。

    timeout=120 是**单次 socket 操作**的超时（只在无数据流动时计时），不是
    总时长——代理/TUN 链路慢时全量下载远超 120 秒属正常，不会被它中断。
    对偶发网络失败（连接重置、响应中断、5xx）自动重试一次：更新是低频操作，
    多花一次下载的代价远小于整次更新失败；4xx 是请求本身的问题（版本不存在、
    地址错），重试不会变好，直接抛出。

    配了 GitHub 镜像（issue #216）时先走镜像，失败再直连 —— 镜像本身也会抖动，
    不能因为它挂了就让整个更新不可用。
    """
    last: BaseException | None = None
    for candidate in _url_candidates(url):
        if candidate != url:
            rep.log(f'经镜像下载 {candidate}')
        else:
            rep.log(f'下载 {candidate}')
        last = _download_once(candidate, dest, rep)
        if last is None:
            return
        if candidate != url:
            rep.log(f'镜像下载失败（{last}），回退直连…', 'warn')
    assert last is not None
    # 只有「看着像网络到不了 GitHub」的失败才补一句可操作提示；其余照原样抛出，
    # 保持调用方与既有测试看到的异常类型不变。
    hint = _download_hint(last)
    raise RuntimeError(f'{last} {hint}') if hint else last


def _download_once(url: str, dest: Path, rep: Reporter) -> BaseException | None:
    """下载到 dest；成功返回 None，失败返回最后一次异常（不抛出）。"""
    last: BaseException | None = None
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'workbuddy-manager-updater'})
            with _opener().open(req, timeout=120) as resp, open(dest, 'wb') as fh:
                shutil.copyfileobj(resp, fh)
            last = None
            break
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return exc
            last = exc
        except (OSError, http.client.HTTPException) as exc:
            # URLError/socket.timeout/ConnectionReset 等都是 OSError 子类；
            # IncompleteRead 走 HTTPException。两类都值得再试一次。
            last = exc
        if attempt == 1:
            rep.log(f'下载失败（{last}），自动重试一次…', 'warn')
    if last is not None:
        return last

    size = dest.stat().st_size
    rep.log(f'  完成（{size / 1024 / 1024:.2f} MB）')
    if size < 100_000:
        # 镜像有时会返回一张「仓库不存在 / 请稍后再试」的 HTML 错误页
        return RuntimeError('下载内容异常偏小，可能是错误页面')
    return None


def _sha256_of(path: Path) -> str:
    """分块算 sha256（发布包十几 MB，分块读更省内存，也便于以后复用）。"""
    digest = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def verify_download(archive: Path, info: dict, rep: Reporter) -> None:
    """核对下载到的发布包：文件大小 + sha256 摘要（发布页/API 给的），不符即中止。

    为什么保留这两项（而不是「下载完直接装」）：

      · **大小** —— 实测里最常出的事故是**下载被截断**（CDN / 代理 / TUN 链路），
        截断的 tar.gz 往往解压到一半才报错，报错位置与真实原因毫无关系；
      · **摘要** —— 防的是「传输途中被换掉」，而且它就是发布页上给用户的同一个
        哈希：想人工复核的人拿它就能对得上。

    摘要在 API 路径能拿到（GitHub Releases API 给每个资产带 `digest`）；回退路径
    （只有 302 重定向）拿不到，那时打印本地 sha256 供人工比对，并**如实记成
    未比对** —— 不假装校验过。
    """
    size = archive.stat().st_size
    expected_size = info.get('pkg_size')
    if isinstance(expected_size, int) and expected_size > 0 and size != expected_size:
        raise RuntimeError(
            f'发布包大小不符，已拒绝安装：下载到 {size} 字节，发布页是 {expected_size} 字节。\n'
            '  通常是下载被中断（网络或代理不稳）—— 再点一次更新即可。'
        )

    digest = _sha256_of(archive)
    expected = str(info.get('pkg_digest') or '')
    if expected.startswith('sha256:'):
        want = expected.split(':', 1)[1].strip().lower()
        if digest != want:
            raise RuntimeError(
                '❌ 发布包 sha256 与发布页不一致，已拒绝安装。\n'
                f'  发布页：{want}\n'
                f'  下载到：{digest}\n'
                '  多为下载不完整（重试一次）；若始终不一致，'
                '请到项目 Releases 页面自行核对后再决定是否手动安装。'
            )
        rep.log(f'✓ 发布包校验通过（sha256 {digest}，与发布页一致）')
        rep.set_integrity('digest', digest)
        return

    rep.log(f'发布包 sha256：{digest}')
    rep.log('本次未比对发布页摘要（当前走的是回退路径），只校验了下载是否完整', 'warn')
    rep.set_integrity('none', digest)


# ── 上游更新 ─────────────────────────────────────────────
# 上游 compose 里与我们安全基线不同的那行端口绑定（见 enforce_local_bind）。
def _compose_wide_bind() -> str:
    return f'"{UPSTREAM_PORT}:{UPSTREAM_PORT}"'


def _upstream_bind_host() -> str:
    """宿主机上发布上游端口时使用的地址（issue #243）。

    默认 `127.0.0.1`（仅本机）。但管理端跑在容器里、通过
    `WB2API_BASE=http://host.docker.internal:7863` 连上游时，宿主机回环对
    容器**不可达**（Linux 上尤其明确）；把端口收敛到 127.0.0.1 会让更新后
    面板一直提示「上游不可用」。

    这种部署下改为绑定 `host.docker.internal` 解析出的宿主机网桥地址
    （如 172.17.0.1 / 172.18.0.1）：容器可达，且不是公网地址。
    Docker Desktop 的 192.168.65.0/24 是虚拟机内部地址，宿主并不拥有，
    仍用 127.0.0.1（Desktop 会把它转发给宿主回环）。

    `WB_UPSTREAM_BIND` 可显式覆盖（如只绑内网网卡 `192.168.1.10`）。
    """
    explicit = (os.environ.get('WB_UPSTREAM_BIND') or '').strip()
    if explicit:
        return explicit
    base = (os.environ.get('WB2API_BASE') or '').strip()
    if not base:
        return '127.0.0.1'
    try:
        parts = urllib.parse.urlsplit(base if '//' in base else f'http://{base}')
        host = (parts.hostname or '').lower()
    except ValueError:
        return '127.0.0.1'
    if host != 'host.docker.internal':
        return '127.0.0.1'
    try:
        ip = socket.gethostbyname(host)
    except OSError:
        return '127.0.0.1'
    if not ip or ip.startswith('127.') or ip.startswith('192.168.65.'):
        return '127.0.0.1'
    return ip


def _compose_bound_bind(host: str | None = None) -> str:
    return f'"{host or _upstream_bind_host()}:{UPSTREAM_PORT}:{UPSTREAM_PORT}"'


def _port_converged(text: str) -> str:
    """把 compose 文本里的公网端口绑定收敛为仅本机/容器网桥（纯文本替换）。"""
    new = _compose_bound_bind()
    for old in (_compose_wide_bind(),
                f'"0.0.0.0:{UPSTREAM_PORT}:{UPSTREAM_PORT}"',
                f'"127.0.0.1:{UPSTREAM_PORT}:{UPSTREAM_PORT}"'):
        text = text.replace(old, new)
    return text


def enforce_local_bind(rep: Reporter) -> None:
    """把 compose 的端口绑定收敛到安全地址。

    upstream 仓库里是 `7863:7863`（公网可达）；我们的基线要求只绑本机。
    容器经 host.docker.internal 访问时，绑宿主网桥地址（issue #243），
    否则容器连不上；每次更新后都要重新施加，否则会悄悄回退。
    """
    compose = UPSTREAM_DIR / 'docker-compose.yml'
    if not compose.is_file():
        rep.log('未找到 docker-compose.yml，跳过端口收敛', 'warn')
        return
    text = compose.read_text(encoding='utf-8')
    bound = _compose_bound_bind()
    if bound in text:
        rep.log(f'端口绑定已是 {_upstream_bind_host()}（安全基线）')
        return
    if any(old in text for old in (_compose_wide_bind(),
                                   f'"0.0.0.0:{UPSTREAM_PORT}:{UPSTREAM_PORT}"',
                                   f'"127.0.0.1:{UPSTREAM_PORT}:{UPSTREAM_PORT}"')):
        compose.write_text(_port_converged(text), encoding='utf-8')
        rep.log(f'已将端口绑定收敛为 {bound}（安全基线）')
    else:
        rep.log('未匹配到端口绑定行，请人工确认 compose 配置', 'warn')


def _normalize_eol(raw: bytes) -> bytes:
    """文本内容的 CRLF → LF；二进制原样返回（issue #202）。

    为什么必须在这一步归一：随包分发的上游源码是在 Windows 上打的包，**包内就带
    CRLF**（实测 293/298 个文件）。同步是字节拷贝，于是宿主源码树、进而网关镜像里的
    脚本首行变成 `#!/usr/bin/env bash\\r` —— 解释器路径对不上，一键登录 / 签到 / 积分
    这些依赖脚本的功能整体失效。

    判据与报告方本地兜底一致：**能按 UTF-8 解码且不含 NUL** 的当文本，其余（图片、
    压缩包等）一个字节都不动。猜错方向的代价是把二进制改坏，比行尾问题严重得多，
    所以宁可漏归一一个奇怪的文本，也不碰任何可能非文本的东西。

    顺带解决第二件事：内容只差行尾的文件此前会被算成「有改动」→ 白等一次网关重建
    （报告方实测：不归一 295 个文件全算改动，归一后只报 7 改写 + 2 新增）。
    """
    if b'\x00' in raw:
        return raw
    try:
        raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw
    return raw.replace(b'\r\n', b'\n')


def _sync_bundled_upstream(src: Path, rep: Reporter) -> int:
    """把发布包内自带的上游源码同步进 `UPSTREAM_DIR`（保留用户数据与定制）。

    返回**改动文件数**（改写的 + 新增的）；0 表示没变或本次不适用——
    调用方据此决定要不要重建上游容器（源码没变就不该白等一次构建）。

    上游源码由本项目维护，随管理端 Release 分发（包内 `upstream/`）。在这里
    同步而不是另拉一个仓库，是因为**包是验过签的**：上游代码与面板走同一条
    下载与校验路径，不存在第二个更新源。

    三条克制：
      · git 目录也同步：外部上游已停止维护，本地 git 历史不再作为更新来源；
        仍保留 `config.json` / `auths/` / `data/` 和用户自定义的 compose；
      · 顶层 `config.json` / `auths/` / `data/` 一律不动（api_key、账号凭据、运行数据）；
      · 只增改、不删除：包里没有的文件保留原地（宁可留旧文件，也不误删用户的）。
    """
    incoming = src / 'docker-compose.yml'
    if not incoming.is_file():
        rep.log('本次包内未含上游源码（upstream/），跳过上游代码同步')
        return 0
    UPSTREAM_DIR.mkdir(parents=True, exist_ok=True)
    if not (UPSTREAM_DIR / 'docker-compose.yml').is_file():
        rep.log(f'{UPSTREAM_DIR} 里还没有上游源码，按发布包完整写入')
    if (UPSTREAM_DIR / '.git').is_dir():
        rep.log('检测到上游目录是 git 部署：外部仓库已停更，本次按发布包内源码同步；'
                '本地 git 历史不再作为更新来源', 'warn')

    # 用户对 compose 的定制（加网络 / 改卷 / 改端口之外的东西）要保住。
    # 没有 git 可比，就拿「包内那份」当基准：两边都归一化掉我们的端口收敛，
    # 不一致就说明本地有额外改动（issue #28 的情形）。
    local_compose = UPSTREAM_DIR / 'docker-compose.yml'
    keep_compose: str | None = None
    try:
        local_text = local_compose.read_text(encoding='utf-8')
        if _port_converged(local_text).strip() != _port_converged(
                incoming.read_text(encoding='utf-8')).strip():
            keep_compose = local_text
            rep.log('检测到 docker-compose.yml 有本地定制，同步后原样恢复')
    except OSError:
        pass

    skip_top = {'config.json', 'auths', 'data'}
    changed = 0
    added = 0
    for f in sorted(src.rglob('*')):
        if not f.is_file():
            continue
        rel = f.relative_to(src)
        if rel.parts[0] in skip_top or '.git' in rel.parts or '__pycache__' in rel.parts:
            continue
        dst = UPSTREAM_DIR / rel
        try:
            incoming = _normalize_eol(f.read_bytes())
            if dst.is_file():
                if dst.read_bytes() == incoming:
                    continue
                # 注意：**内容一致、只差行尾**时也要重写（changed 照计）——已经带 CRLF
                # 更新过的机器，本地脚本是坏的（容器里执行不了），只比原始字节会两边
                # 都是 CRLF、被判成「没变」，于是永远修不回来。归一遍之后，下次更新
                # 就真的「没变」了。
                changed += 1
            else:
                added += 1
            dst.parent.mkdir(parents=True, exist_ok=True)
            # 先写临时文件再原子替换：中途断电/被杀不会留下半个 .go 文件（网关构建
            # 会因此直接失败），也不会让容器读到半截脚本。
            tmp = dst.with_name(dst.name + '.wbtmp')
            tmp.write_bytes(incoming)
            shutil.copymode(f, tmp)
            os.replace(tmp, dst)
        except OSError as exc:
            rep.log(f'  同步 {rel} 失败：{exc}', 'warn')

    if keep_compose is not None:
        local_compose.write_text(keep_compose, encoding='utf-8')
    # 标记文件是面板判断「本机上游是否已同步」的唯一依据。它没写进去时，
    # 上面所有文件都可能已经同步成功，但界面仍会一直提示不一致（issue #240
    # 的现场就是这类：目录不是持久化挂载，或写权限不足）。这里把它单独验一遍。
    src_id = src / '.upstream-id'
    if src_id.is_file():
        try:
            want = src_id.read_text(encoding='utf-8').strip()
            got = (UPSTREAM_DIR / '.upstream-id').read_text(encoding='utf-8').strip() \
                if (UPSTREAM_DIR / '.upstream-id').is_file() else ''
            if want and got != want:
                rep.log(
                    f'上游标识未能更新：{UPSTREAM_DIR}/.upstream-id '
                    f'仍是 {got[:8] or "缺失"}，应为 {want[:8]}。'
                    '请确认该目录已从宿主机挂载且容器用户可写；'
                    '否则面板会一直提示「随包上游与本机不一致」。',
                    'error')
            else:
                rep.log(f'上游标识已同步：{want[:8]}')
        except OSError as exc:
            rep.log(f'核对上游标识失败：{exc}', 'warn')
    if changed or added:
        rep.log(f'上游源码已随包更新：改写 {changed} 个、新增 {added} 个文件')
    else:
        rep.log('上游源码与包内一致，无需改动')
    return changed + added


def update_upstream(rep: Reporter) -> None:
    """同步发布包内自带的上游源码，并在有变化时重建容器。

    上游已由本项目维护，没有独立的远端仓库/分支；无论本地目录是不是 git
    部署，包内 `upstream/` 都是唯一更新源。管理端与上游共用同一次下载与
    解压（`--target both` 不会下两遍）。
    """
    rep.step('同步随包上游')

    new_root, _info = _release_root(rep)
    changed = _sync_bundled_upstream(new_root / 'upstream', rep)
    if changed:
        # 包内那份 compose 是**上游原样**（`7863:7863`，公网可达），同步会盖掉
        # 端口收敛，所以这里必须**重新施加**安全基线，否则上游会重新暴露到 0.0.0.0。
        enforce_local_bind(rep)
        rep.log('上游源码有变化，重建容器使其生效…')
        rebuild_upstream(rep)
    else:
        rep.log('上游源码与包内一致，无需重建')
    _clear_version_cache(rep)


def rebuild_upstream(rep: Reporter) -> None:
    """预检 Dockerfile → 重建上游容器 → 等待就绪。

    抽出来是因为它有**两个调用点**：
      · 常规的上游更新（上面那段 git 流程之后）；
      · 面板更新时同步了包内自带的上游源码之后（源码变了必须重建才生效）。
    """
    # 先确认真的能操作 docker，再谈重建。失败时**跳过上游而不是抛错**：
    # 上游的问题不该把管理端也拖死 —— `--target both` 里管理端是用户唯一能操作的面板，
    # 而 socket 权限这种事用户在面板里改不了，抛错只会让整个更新白跑。
    unusable = _docker_unusable_hint()
    if unusable:
        rep.log(f'跳过上游重建：{unusable}', 'error')
        return

    missing = _missing_copy_sources()
    if missing:
        rep.log('构建预检未通过：Dockerfile 引用了不存在的文件', 'error')
        for m in missing:
            rep.log(f'  缺少 {m}', 'error')
        rep.log('这是上游代码本身的问题（不是你配置的问题）。'
                '可用上一版管理端发布包重装回退，或等维护者修复后发布新版。', 'error')
        raise RuntimeError('上游 Dockerfile 引用了不存在的文件：' + ', '.join(missing))

    # 6) 重建并启动
    rep.log('重建并启动上游容器（首次可能需数分钟）…')
    compose_cmd = _compose_cmd(rep)
    if compose_cmd is None:
        # 容器镜像已内置 compose 插件（见 Dockerfile）；真走到这里说明用户
        # 用的是旧镜像或**自建的**管理端镜像。给出可执行的修法，而不是
        # 让他对着 'docker-compose': No such file 发呆。
        raise RuntimeError(
            '找不到可用的 compose 命令（docker compose / docker-compose 都没有）。\n'
            '  容器部署请更新到最新版管理端镜像（已内置 compose 插件）；\n'
            '  自建镜像请在 Dockerfile 里安装 compose；\n'
            '  宿主机部署请安装 docker compose 插件或 docker-compose。\n'
            '  自查：容器内执行 `docker compose version` 与 `ls /usr/local/lib/docker/cli-plugins/`。'
        )
    rc, out = run(compose_cmd + ['up', '-d', '--build'], cwd=UPSTREAM_DIR, rep=rep, check=False)
    if rc != 0:
        hint = _diagnose_build_failure(out)
        rep.log(f'重建失败（exit {rc}）', 'error')
        if hint:
            rep.log(f'原因判断：{hint}', 'error')
        # exit 127 = 选中的 compose 命令**实际不存在**。这只有两种可能：
        # ① 探测通过、执行时文件没了（罕见）；② 执行的是**旧版更新器**——
        #    旧版没有上面那段探测，会直接去跑 docker-compose（issue #55）。
        # 两种都要把它指出来：否则用户只能看到一行「命令不存在」，无从判断。
        if rc == 127:
            rep.log('诊断：选中的 compose 命令在执行时不存在。'
                    '若上面的日志里**没有**「compose 探测」那一行，说明本次执行的是'
                    '旧版更新器（新版会先探测并打出结果）——请重新更新管理端并确认'
                    '容器已用新镜像重启，再重试。', 'error')
        # 构建失败时 compose 不会动已在运行的容器，明确说明当前服务状态
        _report_service_state(rep)
        raise RuntimeError('上游重建失败' + (f'：{hint}' if hint else '，请查看上方日志'))

    # 6) 等待就绪（地址按部署形态取：容器里不是回环，见 upstream_health_url）
    wait_upstream_ready(rep)


def _clear_version_cache(rep: Reporter) -> None:
    """清掉版本检测缓存，使界面立即重新判断「有没有更新」。"""
    try:
        (DATA_DIR / 'version-check.json').unlink(missing_ok=True)
        rep.log('已清除版本检测缓存')
    except Exception:  # noqa: BLE001
        pass


# 安全关键文件：更新逻辑本身，改了值得人工看一眼
_REVIEW_FILES = ('update.py',)


def _explain_deploy_risk(rep: Reporter, modified: list[str], added: list[str],
                         here: Path, backup: Path | None = None) -> None:
    """把 deploy/ 差异翻译成「要不要紧张、下一步做什么」。

    为什么要单独一段说明：早先只说「已跳过同步」，管理员看到一排文件名
    无法判断严重性——实测有用户专门来问「这要不要紧」。差异本身分三类，
    处理方式完全不同：

      1. 只新增了工具脚本（如 check-upstream.sh）→ **无需任何操作**
      2. 修改了普通脚本 / systemd 单元 → 看一眼即可，想要新功能就覆盖
      3. 修改了 update.py（更新器自己）→ 建议比对一眼：它决定「装进来的东西
         装到哪、怎么重启」。这类改动本来就该由维护者看懂，出问题也是更新器
         这类文件最先暴露

    ## 还要说清「不同步的代价」（issue #55）

    只讲风险会让管理员一律选择不动手，而**更新器本身**也在 deploy/ 里：不同步
    就意味着「更新器永远是旧的那一份」，它修过的毛病（例如 compose v2 探测）
    不会生效 —— issue #28 修过一次、#55 又报了一次，就是同一批人始终跑着旧
    更新器。所以这里必须给出**代价**与**一条可执行的命令**，而不是只留下
    「需人工确认」四个字。
    """
    anchors = [f for f in modified if Path(f).name in _REVIEW_FILES]
    others = [f for f in modified if Path(f).name not in _REVIEW_FILES]

    if anchors:
        rep.log('   ⚠️ 其中包含更新器自身：' + '、'.join(anchors), 'warn')
        rep.log('      它决定「文件装到哪、服务怎么重启」，建议比对一眼再覆盖。', 'warn')
    elif others:
        rep.log('   这些是普通脚本/配置（不含更新逻辑），看过差异后按需覆盖即可。')
    elif added:
        rep.log('   仅新增文件、未改动现有内容 —— 不影响本次更新，可以不处理。')

    if modified:
        rep.log(f'   比对方法：diff {here.parent}/<文件名> <新包目录>/deploy/<文件名>')
    rep.log(f'   覆盖位置：{here.parent}（本次未改动任何文件）')

    # 「不同步的代价」：更新器就在 deploy/ 里，不换它 = 以后每次都还用旧逻辑。
    updater = [f for f in modified if Path(f).name == 'update.py']
    if updater:
        rep.log('   ⚠️ 本次更新改动了**更新器本身**（deploy/update.py）。'
                '不同步的话，从下一次起仍由旧更新器执行更新，本次修的问题不会生效。',
                'warn')
    rep.log('   想让它随包更新（默认就是）：去掉 WB_SYNC_DEPLOY=0 后重跑本次更新'
            '（覆盖前会备份到数据目录），或按上面的「比对方法」人工比对后手动覆盖。')
    if backup is not None:
        rep.log(f'   本次备份目录：{backup}')


def _deploy_sync_enabled() -> bool:
    """更新管理端时，`deploy/` 是否随包更新。**默认更新**，`WB_SYNC_DEPLOY=0` 关闭。

    见 `update_manager` 里那段说明：包在解压前已核对大小与 sha256 摘要，覆盖 deploy/
    与覆盖 server/ 是同一性质（同一份下载来的内容）；而**不**更新的代价是更新器
    永远是旧的（它本身就在 deploy/ 里）。
    """
    return os.environ.get('WB_SYNC_DEPLOY', '').strip() != '0'


def _sync_deploy(new_deploy: Path, install_dir: Path, backup: Path,
                 rep: Reporter) -> tuple[list[str], list[str]]:
    """把包内的 deploy/ 覆盖到安装目录（仅在与本地不同时动手），返回 (新增, 修改)。

    调用前提：包**已经通过大小与 sha256 校验**（`verify_download` 在解压之前执行）。
    因此这里的覆盖与替换 server/ 是同一性质的 —— 都是同一份下载来的内容。
    仍然先备份，便于回退。

    抽成独立函数是为了可测：它决定「更新器自己能不能被更新」，而这段逻辑
    过去只存在于 `update_manager` 内联代码里，改错了没有测试会红。

    两个刻意的取舍（审核时特意确认过，别当成疏漏）：
      · **只增不删**：本地有、包里没有的文件不会被删掉。发布包不该替用户清理
        目录，留着顶多是多余文件；
      · 用 `copyfile` 而不是 `copy2`：不保留包内权限位，写出来的文件按 umask 取
        默认权限（可预测）。deploy/ 下的脚本都以解释器调用（`bash xxx.sh`、
        `python3 xxx.py`），不依赖可执行位；反过来说，保留包内 mode 反而会把
        一个意外的 0777 一路带进安装目录。
    """
    dest = install_dir / 'deploy'
    added: list[str] = []
    modified: list[str] = []
    for src in sorted(new_deploy.rglob('*')):
        if not src.is_file():
            continue
        rel = src.relative_to(new_deploy)
        dst = dest / rel
        try:
            if not dst.is_file():
                added.append(str(rel))
            elif dst.read_bytes() != src.read_bytes():
                modified.append(str(rel))
        except OSError:
            modified.append(str(rel))
    if not (added or modified):
        return [], []
    shutil.copytree(dest, backup / 'deploy', dirs_exist_ok=True)
    for src in sorted(new_deploy.rglob('*')):
        if src.is_file():
            rel = src.relative_to(new_deploy)
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest / rel)
    rep.log('deploy/ 已同步到包内版本（默认行为；旧文件备份在 '
            f'{backup / "deploy"}，想保持不变可设 WB_SYNC_DEPLOY=0）', 'warn')
    if modified:
        rep.log('   同步（改动）：' + '、'.join(modified[:8])
                + ('…' if len(modified) > 8 else ''), 'warn')
    if added:
        rep.log('   同步（新增）：' + '、'.join(added[:8])
                + ('…' if len(added) > 8 else ''), 'warn')
    rep.log('   注意：若本次同步改了 update.py，**下一次**更新才由新版本执行'
            '（本次仍跑在旧代码上）。', 'warn')
    return added, modified


def _missing_copy_sources() -> list[str]:
    """列出 Dockerfile 里 COPY 引用了、但仓库中并不存在的本地文件。

    上游曾出现「删了脚本却漏改 Dockerfile」导致镜像构建失败。这类问题
    docker 的报错（failed to calculate checksum ... not found）不容易读懂，
    这里提前查出来给出明确结论。

    只判定**确凿**的情况：跳过 --from=（阶段拷贝）、URL、含通配符的源，
    避免误报把正常更新挡下来。
    """
    dockerfile = UPSTREAM_DIR / 'Dockerfile'
    if not dockerfile.is_file():
        return []
    missing: list[str] = []
    try:
        lines = dockerfile.read_text(encoding='utf-8', errors='replace').splitlines()
    except Exception:  # noqa: BLE001
        return []

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        if len(parts) < 2 or parts[0].upper() != 'COPY':
            continue
        tokens = parts[1:]
        if any(t.startswith('--from=') for t in tokens):
            continue  # 从构建阶段拷贝，不是仓库文件
        # 去掉 --chown= 之类的选项
        srcs = [t for t in tokens[:-1] if not t.startswith('--')]
        for src in srcs:
            if any(ch in src for ch in '*?['):
                continue  # 通配符交给 docker 自己解析
            if src.startswith(('http://', 'https://')):
                continue
            if not (UPSTREAM_DIR / src).exists():
                missing.append(src)
    return missing


# 容器里拿不到 docker.sock 权限时该怎么做。抽成常量是因为两处都要用
# （重建前的预检、构建失败后的诊断）—— 措辞必须一致，否则用户会以为是两个问题。
_DOCKER_SOCKET_RECIPE = (
    '容器访问不了宿主 docker（/var/run/docker.sock 权限不足）。\n'
    "  1) 宿主取 GID：stat -c '%g' /var/run/docker.sock\n"
    '  2) 写进 .env：DOCKER_GID=<上面那个 GID>（compose 已用 group_add 引用它）\n'
    '  3) 重建管理端容器：docker compose up -d workbuddy-manager\n'
    '  若你的 compose 里还没有 group_add，就在 workbuddy-manager 服务下加一行：\n'
    '      group_add: ["${DOCKER_GID}"]\n'
    '  （不需要在容器内更新上游的话，把 compose 里挂 docker.sock 那一行注释掉即可，'
    '相关功能会如实降级。）'
)


def _docker_unusable_hint() -> str:
    """docker 用不了时返回可照做的提示，能用则返回空串。

    为什么要预检：socket 权限不足时 `docker compose up` 只会给一句
    「permission denied while trying to connect to the Docker daemon socket」，
    而容器里没有 root 可变 —— 用户拿着这句话无从下手。

    判据是**实际能不能跑通 `docker info`**（与界面层的
    `server/services/updater.py:can_control_docker` 同一口径），不是"在不在容器里"。
    """
    try:
        proc = subprocess.run(['docker', 'info'], capture_output=True, timeout=8)
    except Exception:  # noqa: BLE001
        return _docker_unusable_text()
    if proc.returncode != 0:
        return _docker_unusable_text()
    return ''


def _docker_unusable_text() -> str:
    """区分两种"用不了"：容器里是 socket 权限，宿主上是没装/连不上。"""
    if in_container():
        return _DOCKER_SOCKET_RECIPE
    return ('本机 docker 不可用（没装 docker，或当前用户连不上 daemon）。\n'
            '  自查：docker info\n'
            '  宿主机部署请安装 docker 并确认当前用户在 docker 组；'
            '容器部署请确认挂了 docker.sock 且配了 group_add。')


def _diagnose_build_failure(out: str) -> str:
    """把 docker 构建失败的长日志归纳成一句人话。"""
    low = (out or '').lower()
    if 'not found' in low and ('checksum' in low or 'copy' in low):
        return ('上游 Dockerfile 引用了仓库里不存在的文件——这是上游代码的问题，'
                '不是你配置的问题。可回滚到上一版管理端发布包后重试')
    if 'no space left' in low:
        return '磁盘空间不足，请清理后重试'
    if any(k in low for k in ('dial tcp', 'i/o timeout', 'temporary failure', 'connection refused')):
        return '网络问题（拉取基础镜像或依赖失败），稍后重试'
    # socket 连不上要单独判：它的修法是给容器补组，而不是"换 root 执行"
    # （容器里根本没有 root 可变，那句提示会把人带偏）。
    if 'docker.sock' in low or 'docker daemon' in low:
        return _DOCKER_SOCKET_RECIPE
    if 'permission denied' in low:
        return '权限不足，请确认以 root 或具备 docker 权限的用户执行'
    return ''


def _health_ok(url: str, timeout: int = 3) -> bool:
    try:
        with _probe_opener().open(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def _report_service_state(rep: Reporter) -> None:
    """构建失败后说明当前服务是否还活着。

    构建失败时 compose 不会动已在运行的容器，因此旧版本通常仍在提供服务；
    明确告诉用户这一点，避免误以为「更新失败=服务挂了」而做多余操作。
    """
    if _health_ok(upstream_health_url()):
        rep.log('注意：本次重建失败，但检测到上游仍在响应——旧容器未被影响，服务正常', 'warn')
    else:
        rep.log('警告：上游健康检查未通过，请检查容器状态（docker ps / docker logs）', 'error')


def _has_compose_v2() -> bool:
    """`docker compose`（无连字符）是否可用。

    只是**候选之一**，不要把它当作「能不能用 compose」的判据——见
    `_compose_cmd()`：容器形态下 docker CLI 插件目录里往往没有 compose，
    但用户本机装了旧的 `docker-compose`，两条路都得试。
    """
    try:
        return subprocess.run(['docker', 'compose', 'version'],
                              capture_output=True, timeout=15).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _has_compose_v1() -> bool:
    """旧的 `docker-compose`（连字符）是否可用。"""
    if not shutil.which('docker-compose'):
        return False
    try:
        return subprocess.run(['docker-compose', 'version'],
                              capture_output=True, timeout=15).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _compose_plugin_paths() -> list[str]:
    """已安装的 compose 插件文件（按 docker CLI 的查找顺序，列出存在的那些）。

    只用于诊断输出：compose 用不了时，最需要知道的就是「插件到底装没装、装在哪」
    —— 否则只能对着 `docker-compose: No such file` 猜（issue #55 的报告在这里
    卡住了）。容器镜像按 Dockerfile 会把插件放在第一个路径。

    `Path.home()` 与每个 `is_file()` 都单独兜住：uid 在 /etc/passwd 里没有条目时
    `Path.home()` 会抛 RuntimeError（某些编排器以任意 uid 跑容器就是这种情形），
    权限异常也可能让 `is_file()` 抛。这是**诊断**用的辅助函数，绝不该因为它自己
    出错而把更新流程带崩。
    """
    cands = [
        Path('/usr/local/lib/docker/cli-plugins/docker-compose'),
        Path('/usr/lib/docker/cli-plugins/docker-compose'),
        Path('/usr/local/libexec/docker/cli-plugins/docker-compose'),
    ]
    try:
        cands.append(Path.home() / '.docker' / 'cli-plugins' / 'docker-compose')
    except Exception:  # noqa: BLE001
        pass
    out: list[str] = []
    for p in cands:
        try:
            if p.is_file():
                out.append(str(p))
        except OSError:
            continue
    return out


def _compose_cmd(rep: Reporter | None = None) -> list[str] | None:
    """解析出可用的 compose 命令；都不可用返回 None。

    为什么不能像原先那样「有 v2 就用 v2，否则退回 v1」：容器形态下
    **两个都不可用** —— 镜像里的 docker CLI 来自官方静态包，而那个包里
    只有 `docker` 一个二进制，**不含 compose 插件**
    （实测 `tar -tzf docker-27.3.1.tgz` 只有 docker/dockerd/ctr/containerd*）。
    于是原先的写法在容器里必然走进 `['docker-compose']` 分支，
    报 `FileNotFoundError: 'docker-compose'`（issue #28 的 exit 127）。

    顺序：先用宿主机已有的（v2 优先，它是官方推荐形态；容器镜像自带的就是它），
    再退到旧的 `docker-compose`。两者都没有时返回 None，由调用方**直接报错并给出
    可执行的修法** —— 绝不去执行一个明知不存在的命令（那样用户只会看到一行
    `命令不存在`，完全看不出该做什么，issue #55 的报告正是这个形态）。

    传 `rep` 时打印探测结果（含插件文件路径）。这行日志还有一个用处：它是判断
    **「更新器本身是不是太旧」** 的证据 —— 旧版更新器没有这段探测，日志里根本
    不会出现它（issue #55 的报告里就没有，据此可判定执行的是旧代码）。
    """
    v2 = _has_compose_v2()
    v1 = _has_compose_v1()
    if rep:
        found = _compose_plugin_paths()
        rep.log('compose 探测：docker compose=%s，docker-compose=%s%s'
                % ('可用' if v2 else '不可用', '可用' if v1 else '不可用',
                   ('，插件文件=' + '、'.join(found)) if found else '，未发现插件文件'))
    if v2:
        return ['docker', 'compose']
    if v1:
        return ['docker-compose']
    return None


def wait_health(url: str, tries: int, rep: Reporter) -> bool:
    for _ in range(tries):
        try:
            with _probe_opener().open(url, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
    return False


def wait_upstream_ready(rep: Reporter) -> bool:
    """重建后等上游就绪。

    地址走 `upstream_health_url()`（按部署形态取），不要在这里写死回环——
    容器部署下那句「未在预期时间内就绪」会变成一条与事实相反的假警告。
    """
    rep.log('等待上游就绪…')
    if wait_health(upstream_health_url(), 90, rep):
        rep.log('上游已就绪')
        return True
    rep.log('上游未在预期时间内就绪，请查看容器日志', 'warn')
    return False


# ── 管理端更新 ───────────────────────────────────────────
def fetch_release_via_api() -> dict:
    """经 GitHub API 取最新 Release（认证后 5000 次/小时，未认证 60 次/小时/IP）。"""
    rel = http_json(f'https://api.github.com/repos/{MANAGER_REPO}/releases/latest')
    assets = rel.get('assets') or []
    pkg = next((a for a in assets if str(a.get('name', '')).endswith('.tar.gz')), None)
    if not pkg:
        raise RuntimeError('该 Release 未提供 .tar.gz 产物')
    return {
        'tag': rel.get('tag_name') or '',
        'pkg_url': pkg['browser_download_url'],
        'pkg_name': Path(str(pkg.get('name') or 'pkg.tar.gz')).name,
        # 下载后据此核对（见 verify_download）：GitHub 给每个资产带 size 与
        # sha256 摘要（形如 `sha256:…`，老响应里可能没有 → 那时只能比对大小）。
        'pkg_size': pkg.get('size'),
        'pkg_digest': pkg.get('digest'),
    }


def fetch_release_via_redirect(latest_url: str | None = None,
                               repo: str | None = None) -> dict:
    """不走 GitHub API 的回退：releases/latest 的 302 重定向拿最新 tag。

    API 未认证限额只有 60 次/小时/IP，共享出口（TUN/系统代理）极易被同出口
    用户打满，表现为偶发 HTTP 403；而 github.com 的网页端与
    releases/download 资产下载不受该配额限制。资产名按发布惯例
    workbuddy-manager-<tag>.tar.gz 构造（历届 Release 均如此）。

    **这条路径不走镜像**（issue #216）：gh-proxy 一类加速服务只放行资源下载，
    对网页正文直接返回 403（「Web page content is not allowed」）。镜像模式下的
    主路径是 API（镜像可代理 api.github.com），只有 API 完全不可用时才落到这里，
    那时直连 github.com 本来就是唯一选择。
    """
    repo = repo or MANAGER_REPO
    req = urllib.request.Request(
        latest_url or f'https://github.com/{repo}/releases/latest',
        headers={'User-Agent': 'workbuddy-manager-updater'})

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(req, timeout=30)
    except urllib.error.HTTPError as exc:
        if exc.code not in (301, 302, 303, 307, 308):
            raise RuntimeError(f'releases/latest 回退探测失败：HTTP {exc.code}') from exc
        loc = exc.headers.get('Location') or ''
        tag = urllib.parse.unquote(loc.split('/tag/', 1)[-1].split('?')[0].split('#')[0])
        if not tag or '/' in tag:
            raise RuntimeError(f'releases/latest 重定向未含有效 tag：{loc}') from exc
    else:
        raise RuntimeError('releases/latest 未按预期重定向（网络异常或路径变更）')

    base = f'https://github.com/{repo}/releases/download/{tag}'
    pkg_name = f'workbuddy-manager-{tag}.tar.gz'
    # 这条路径拿不到资产的 sha256（只有重定向里的 tag），下载后只校验完整性、
    # 并把本地摘要打进日志供人工比对 —— 见 verify_download。
    return {
        'tag': tag,
        'pkg_url': f'{base}/{pkg_name}',
        'pkg_name': pkg_name,
        'pkg_size': None,
        'pkg_digest': '',
    }


def _system_site_writable() -> bool:
    """系统 site-packages 能不能直接写。

    判据是**真实可写性**，不是 uid：容器部署里进程是 uid 10001，而镜像里的依赖
    是 root 装的、目录归 root —— 这时只能装到用户站点。宿主部署（root 或 venv）
    则是可写的，不该平白引入 --user 去改变既有安装位置。
    """
    try:
        purelib = sysconfig.get_path('purelib')
    except Exception:  # noqa: BLE001
        return False
    if not purelib:
        return False
    p = Path(purelib)
    # 目录本身与父目录都要能写：pip 要新建 dist-info，也可能要新建目录
    return os.access(p, os.W_OK) and os.access(p.parent, os.W_OK)


# 只在 pip 的**输出**里出现这些字样时才认为"可写性判定可能误判了"
_PERMISSION_HINTS = ('permission denied', 'read-only', 'eacces')


def install_requirements(req: Path, rep: Reporter) -> None:
    """安装 Python 依赖，失败则抛错。

    - 与 install.sh 一致：先带 `--break-system-packages`（PEP 668 的系统 Python
      不带它会拒装），不认这个参数的旧 pip 会失败，再不带它重试一次。
    - 系统 site-packages 不可写时（容器以非 root 运行）改用 `--user`：落到
      `PYTHONUSERBASE`（镜像里钉成 /app/.local，见 Dockerfile）。用户站点排在
      全站 site-packages **之前**，所以能压住旧版本；而 `--user` 不在 PEP 668 的
      豁免名单里，所以两个参数要一起给。

    为什么必须支持"装到别处"：容器形态下唯一能装依赖的就是这条路径，装不上就等于
    这个部署**永远升不上去**（issue #157 的另一半——#160 只做到"失败要响"）。
    """
    py = sys.executable
    use_user = not _system_site_writable()
    target = ['--user'] if use_user else []
    rep.log('检查 / 安装 Python 依赖…')
    if use_user:
        rep.log(f'系统 site-packages 不可写，依赖改装到用户站点（{site.getusersitepackages()}）')

    attempts = [target + ['--break-system-packages'], target]
    code, out = 0, ''
    for extra in attempts:
        code, out = run([py, '-m', 'pip', 'install', '-q', *extra, '-r', str(req)],
                        rep=rep, check=False)
        if code == 0:
            return

    # 可写性判定可能误判（ACL、只读挂载…）：只在输出看起来是权限问题时补一次 --user，
    # 免得给真正的网络/解析失败也套上 --user 掩盖原因。
    if not use_user and any(h in (out or '').lower() for h in _PERMISSION_HINTS):
        code, _ = run([py, '-m', 'pip', 'install', '-q', '--user',
                       '--break-system-packages', '-r', str(req)], rep=rep, check=False)
        if code == 0:
            return

    raise RuntimeError(f'Python 依赖安装失败（exit {code}），已中止更新，当前版本保持不变')


# ── 发布包的一次性取用 ────────────────────────────────────────────
# 上游源码只存在于发布包内（原仓库已不可用），所以**更新上游也得取一次包**。
# `--target both` 时两条路径用的是同一个包 —— 缓存住：别查两遍版本、别下两遍、
# 也别解压两遍。更新器是单线程的，用模块级状态即可。
_release_info_cache: dict | None = None
_release_cache: tuple[Path, dict] | None = None
_release_tmp: tempfile.TemporaryDirectory | None = None


def read_package_version(archive: Path) -> str:
    """从发布包里读出 `.version`（只解那一个成员，不展开整包）。

    两条路径都用到它：离线升级包要靠它拿版本号（不查 GitHub），面板上传时也用
    它回显「包内版本」。读 64 字节足够（里面就是 `v1.0.86` 这种短串）。
    """
    try:
        with tarfile.open(archive, 'r:gz') as tf:
            for member in tf:
                name = member.name.replace('\\', '/')
                if name.count('/') == 1 and name.endswith('/.version'):
                    fh = tf.extractfile(member)
                    return fh.read(64).decode('utf-8', 'replace').strip() if fh else ''
    except (OSError, tarfile.TarError) as exc:
        raise RuntimeError(
            f'升级包读不了（{exc}）—— 确认它是发布页面下载的 .tar.gz，且下载完整') from exc
    return ''


def offline_package_info(rep: Reporter) -> dict:
    """离线升级包的元信息（不联网）。

    版本号取自包内 `.version` 而不是文件名：文件名谁都能改，而 `.version` 是
    打包时写进去的（面板侧也按同一份读，两边口径一致）。
    """
    archive = Path(OFFLINE_PACKAGE)
    if not archive.is_file():
        raise RuntimeError(f'找不到离线升级包：{archive}')
    size = archive.stat().st_size
    if size < 100_000:
        raise RuntimeError('离线升级包过小，不像是完整的发布包（下载可能中断了）')
    version = read_package_version(archive)
    if not version:
        raise RuntimeError('离线升级包里没有 .version —— 不是本项目的发布包，已中止')
    rep.log(f'使用离线升级包：{archive.name}（{size / 1024 / 1024:.1f} MB，包内版本 {version}）')
    if not OFFLINE_PACKAGE_SHA256:
        rep.log('未提供发布页 sha256：本次只校验包能不能解开、内容结构对不对，'
                '包本身的可信度取决于你从哪拿到它', 'warn')
    return {
        'tag': version,
        'pkg_url': '',
        'pkg_name': archive.name,
        'pkg_size': None,          # 没有独立的「期望大小」（文件就在本机）
        'pkg_digest': f'sha256:{OFFLINE_PACKAGE_SHA256}' if OFFLINE_PACKAGE_SHA256 else '',
        'local_package': str(archive),
    }


def _fetch_release_info(rep: Reporter) -> dict:
    """取最新 Release 的元信息（**不下载**）。同一进程内只查一次。

    API 优先（认证后 5000 次/小时；未认证仅 60 次/小时/IP，共享出口极易被同出口
    用户打满而 403）；失败时回退到 releases/latest 的 302 重定向（不走 API、无配额）。

    指定了离线升级包（`WB_UPDATE_PACKAGE`）时不联网：内网机器上这条路本来就是
    为了「完全拿不到 GitHub」才存在的。
    """
    global _release_info_cache
    if _release_info_cache is not None:
        return _release_info_cache

    if OFFLINE_PACKAGE:
        _release_info_cache = offline_package_info(rep)
        return _release_info_cache

    info: dict | None = None
    api_error = ''
    try:
        info = fetch_release_via_api()
    except Exception as exc:  # noqa: BLE001
        api_error = f'{type(exc).__name__}: {exc}'
        rep.log(f'GitHub API 不可用（{api_error}），回退到 releases/latest 重定向…', 'warn')
    if info is None:
        try:
            info = fetch_release_via_redirect()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f'获取 Release 失败：API {api_error}；回退路径也失败：{exc}') from exc
    elif GITHUB_MIRROR:
        # 经 API 拿到的是**原始** GitHub 地址；下载时由 download() 自己决定
        # 先走镜像还是直连，这里不改写（保持 info 与发布页一致，便于人工核对）。
        if _mirror_fallback_error:
            rep.log(f'镜像不可用（{_mirror_fallback_error}），本次版本检测走的是直连', 'warn')
        else:
            rep.log(f'已配置 GitHub 镜像：{GITHUB_MIRROR}（下载失败会自动回退直连）')

    _release_info_cache = info
    return info


def _release_root(rep: Reporter, info: dict | None = None) -> tuple[Path, dict]:
    """下载 + 校验 + 解压最新发布包，返回 `(顶层目录, info)`。进程内只做一次。

    校验必须在下解压之前：先确认「这个包下全了、没被换过」，再谈包里的内容。
    解压目录的生命周期到本次更新结束（`main()` 的 finally 回收）—— 因为
    `update_upstream` 与 `update_manager` 要共用这一份。
    """
    global _release_cache, _release_tmp
    if _release_cache is not None:
        return _release_cache
    if info is None:
        info = _fetch_release_info(rep)

    # pkg_name 已在 fetch_release_via_api / fetch_release_via_redirect 里按同名原则
    # 净化（Path(...).name 丢掉任何目录成分）：下载地址来自网络响应，"用远端数据拼
    # 本地路径"与早前的路径穿越是同一类错误。
    _release_tmp = tempfile.TemporaryDirectory(prefix='wbm-update-')
    tmp = Path(_release_tmp.name)
    archive = tmp / info['pkg_name']
    local = str(info.get('local_package') or '')
    if local:
        # 离线升级包：文件就在本机，复制一份到临时目录只是让后续步骤共用同一条
        # 路径（解压、清理都在 tmp 里，不动用户上传的那份原件）。
        shutil.copyfile(local, archive)
        rep.log('离线升级包已就位（跳过下载）')
    else:
        download(info['pkg_url'], archive, rep)
    verify_download(archive, info, rep)

    rep.log('解压…')
    with tarfile.open(archive, 'r:gz') as tf:
        _safe_extract(tf, tmp)
    roots = [p for p in tmp.iterdir() if p.is_dir()]
    if len(roots) != 1:
        raise RuntimeError('压缩包结构异常（应含单一顶层目录）')

    _release_cache = (roots[0], info)
    return _release_cache


def _cleanup_release_cache() -> None:
    """回收解压出来的临时目录（`main()` 的 finally 里调）。"""
    global _release_info_cache, _release_cache, _release_tmp
    _release_info_cache = None
    _release_cache = None
    if _release_tmp is not None:
        try:
            _release_tmp.cleanup()
        except Exception:  # noqa: BLE001
            pass
        _release_tmp = None


def swap_dir(incoming: Path, target: Path) -> None:
    """用 incoming 目录**原子替换** target（同目录 staging + os.replace）。

    为什么不能 rmtree + copytree：复制中途失败（磁盘满、权限、被杀）会留下不完整的
    server/，服务再也起不来，而备份不会自动恢复。这里先把新内容完整复制到
    `target.new`，成功后再交换；交换失败会把旧目录换回去。

    注意：覆盖粒度是**整个目录**，所以调用方给的 incoming 必须是完整的一份
    （发布包解压出来的那份），不能是增量。
    """
    target = Path(target)
    old = target.with_name(target.name + '.old')
    staging = target.with_name(target.name + '.new')
    # 清理上次中断留下的残留（它们已不可信）
    for stale in (old, staging):
        shutil.rmtree(stale, ignore_errors=True)
    shutil.copytree(incoming, staging)
    had_old = target.exists()
    if had_old:
        try:
            os.replace(target, old)
        except OSError as exc:
            # 容器重建后 /app/server 位于 overlayfs 的 lowerdir，对目录 rename(2)
            # 会返回 EXDEV（issue #225）。退化为「复制成备份 + 删除原目录」：
            # 备份完整，后续换入/回滚逻辑不变。
            if exc.errno != errno.EXDEV:
                raise
            shutil.copytree(target, old)
            shutil.rmtree(target)
    try:
        os.replace(staging, target)
    except Exception:
        if had_old and old.exists():
            os.replace(old, target)
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(old, ignore_errors=True)


def update_manager(rep: Reporter) -> None:
    rep.step('更新管理端')

    # 1) 取最新 Release 的元信息（不下载）。API 优先、失败回退 302 —— 见
    #    _fetch_release_info；上游那条路径也走它，所以回退只修一处。
    info = _fetch_release_info(rep)
    tag = info['tag']
    rep.log(('升级包版本：' if info.get('local_package') else '最新版本：') + f'{tag or "(未知)"}')
    rep.set_target_version(tag)

    current = read_local_version()
    if not info.get('local_package') and current and tag and current == tag.lstrip('v'):
        rep.log(f'当前已是最新版本（{tag}）')
        return
    if info.get('local_package'):
        # 离线包是用户**显式指定**的：即使版本号与当前相同也继续装（重装/修复
        # 正是它最常见的用法之一），所以这里不套用上面的「已是最新就跳过」。
        rep.log('离线升级包：按你指定的包安装，不比较版本新旧')

    pkg_name = info['pkg_name']
    pkg_url = info['pkg_url']

    # 2) 取包（下载 + 校验大小/摘要 + 解压）。与 update_upstream 共用同一次：
    #    `--target both` 时只下一次、只解压一次（上游源码也在包里）。
    new_root, info = _release_root(rep, info)

    # 本次发布带了不同的上游：manager 也不能只换面板，否则「面板升级、上游
    # 还是旧代码」会静默错配（issue #217）。旧部署没有 .upstream-id，按
    # 「未知 = 需要同步」处理；想保留自定义上游可设 WB_SYNC_UPSTREAM=0。
    new_up_id = ''
    try:
        new_up_id = (new_root / 'upstream' / '.upstream-id').read_text(
            encoding='utf-8').strip()
    except OSError:
        pass
    local_up_id = ''
    try:
        local_up_id = (UPSTREAM_DIR / '.upstream-id').read_text(encoding='utf-8').strip()
    except OSError:
        pass
    if new_up_id and new_up_id != local_up_id:
        if os.environ.get('WB_SYNC_UPSTREAM', '').strip() == '0':
            rep.log('本次发布包含上游更新，但 WB_SYNC_UPSTREAM=0：只更新面板，'
                    '上游保持现状。', 'warn')
        else:
            rep.log(f'本次发布包含上游更新（{new_up_id[:12]}），先同步上游并重建容器…', 'warn')
            update_upstream(rep)

    new_server = new_root / 'server'
    new_web_out = new_root / 'web' / 'out'
    if not (new_server / 'main.py').is_file():
        raise RuntimeError('新包缺少 server/main.py，中止更新')
    if not (new_web_out / 'index.html').is_file():
        raise RuntimeError('新包缺少 web/out/index.html，中止更新')

    # 依赖必须在**替换代码之前**装：装失败就中止，安装目录一个字节都不动，
    # 旧版本继续运行。早先是换完代码再 `check=False` 装依赖，pip 失败也照样
    # 标记成功并重启，新代码缺依赖起不来（issue #157）。
    new_req = new_server / 'requirements.txt'
    if new_req.is_file():
        install_requirements(new_req, rep)

    # 3) 备份当前代码后替换
    ts = time.strftime('%Y%m%d-%H%M%S')
    backup = INSTALL_DIR / f'backup-{ts}'
    backup.mkdir(parents=True, exist_ok=True)
    rep.log(f'备份当前版本到 {backup}')
    shutil.copytree(INSTALL_DIR / 'server', backup / 'server', dirs_exist_ok=True)
    if (INSTALL_DIR / 'web' / 'out').is_dir():
        shutil.copytree(INSTALL_DIR / 'web' / 'out', backup / 'web-out', dirs_exist_ok=True)

    rep.log('替换 server/')
    swap_dir(new_server, INSTALL_DIR / 'server')
    # 清掉旧字节码，避免加载到过期模块
    for pc in (INSTALL_DIR / 'server').rglob('__pycache__'):
        shutil.rmtree(pc, ignore_errors=True)

    rep.log('替换 web/out/')
    target_web = INSTALL_DIR / 'web' / 'out'
    target_web.parent.mkdir(parents=True, exist_ok=True)
    swap_dir(new_web_out, target_web)

    # deploy/ 里的脚本**默认随包更新**（`WB_SYNC_DEPLOY=0` 可关掉）。
    #
    # 为什么默认更新（issue #55）：包在**解压之前**已经核对过大小与摘要，包内的
    # deploy/ 与 server/ 属于同一份下载来的内容 —— 覆盖它与覆盖 server/ 是
    # 同一性质。
    #
    # 反过来不更新的代价很实在：**更新器本身就在 deploy/ 里**。不换它意味着
    # 以后每次都还用旧逻辑，它修过的问题永远到不了用户机器上——#28 修过的
    # compose 探测就是这样丢的，#55 又报了一次同一个毛病。
    #
    # 保留关闭开关，是给少数确实手工维护 deploy/ 的部署留退路（例如自己改过
    # systemd 单元、或另有分发流程）。
    #
    # 提示仍要**分清轻重**：早先无论什么差异都只说「已跳过同步」，于是
    # 「新增了一个无害的检查脚本」与「更新器被改」看起来一模一样，管理员
    # 无从判断该紧张还是该忽略（实测：有用户为此专门来问）。现在逐文件标注
    # 新增/修改，并把安全关键文件单独点出来。
    new_deploy = new_root / 'deploy'
    if new_deploy.is_dir():
        here = Path(__file__).resolve()
        # 先算差异（用于提示），再决定是否同步
        added: list[str] = []
        modified: list[str] = []
        for src in sorted(new_deploy.rglob('*')):
            if not src.is_file():
                continue
            rel = src.relative_to(new_deploy)
            dst = INSTALL_DIR / 'deploy' / rel
            try:
                if not dst.is_file():
                    added.append(str(rel))
                elif dst.read_bytes() != src.read_bytes():
                    modified.append(str(rel))
            except OSError:
                modified.append(str(rel))
        if not (added or modified):
            rep.log('deploy/ 与包内一致，无需同步')
        elif _deploy_sync_enabled():
            try:
                _sync_deploy(new_deploy, INSTALL_DIR, backup, rep)
            except OSError as exc:
                rep.log(f'deploy/ 同步失败（继续完成本次更新）：{exc}', 'warn')
        else:
            rep.log('⚠️ 新包内的 deploy/ 与本地不同，已按 WB_SYNC_DEPLOY=0 '
                    '跳过同步', 'warn')
            if modified:
                rep.log('   修改（未覆盖）：' + '、'.join(modified[:8])
                        + ('…' if len(modified) > 8 else ''), 'warn')
            if added:
                rep.log('   新增（未覆盖）：' + '、'.join(added[:8])
                        + ('…' if len(added) > 8 else ''), 'warn')
            _explain_deploy_risk(rep, modified, added, here, backup)

    # 版本标记：界面「当前版本」与更新提醒都以它为准，必须一并替换，
    # 否则更新后仍显示旧版本，并一直提示「发现新版本可用」
    new_marker = new_root / '.version'
    if new_marker.is_file():
        shutil.copyfile(new_marker, INSTALL_DIR / '.version')
        rep.log(f'更新版本标记：{new_marker.read_text(encoding="utf-8").strip()}')

    # 文档同步：界面「更新日志」直接读 CHANGELOG.md，若不同步会一直停在旧版内容
    for name in ('CHANGELOG.md', 'README.md', 'README.en.md'):
        src = new_root / name
        if src.is_file():
            shutil.copyfile(src, INSTALL_DIR / name)
            rep.log(f'同步 {name}')

    rep.log(f'管理端已更新到 {tag}，重启服务以生效')

    # 清掉版本检测缓存：缓存里存的是「更新前」查到的 latest，留着会让界面
    # 拿旧 latest 跟新版本比较，出现「v1.0.5 → v1.0.4」这类把降级当更新的提示，
    # 也会让刚发布的新版本最长 6 小时才被发现。
    _clear_version_cache(rep)

    # 先把终态落盘，再重启：systemd 默认 KillMode=control-group，restart 会连同
    # 本进程一起终止（start_new_session 只脱离终端会话，并未脱离 service 的 cgroup），
    # 若等重启之后再写状态就永远写不到了。容器形态同样会终止本进程（交给编排层拉起）。
    rep.finish(True)
    if in_container():
        # 容器：置退出标记后结束自己，由 compose 的 restart 策略用新代码拉起。
        # 不能只 return——那时更新进程会正常退出，而**管理端主进程仍在跑旧代码**
        # （它是另一个进程），结果是「更新完成」但界面还是旧版。
        _exit_for_restart(rep)
    restart_service(rep)


def in_container() -> bool:
    """是否运行在容器里（Docker/K8s 等）。

    判据用标准做法：`/.dockerenv` 存在，或 cgroup 里出现容器运行时标识。
    这是**探测**而不是配置项，减少用户配错的概率；`WB_RUN_MODE` 可显式覆盖。
    """
    if RUN_MODE in ('docker', 'container'):
        return True
    if RUN_MODE in ('systemd', 'host'):
        return False
    if Path('/.dockerenv').exists():
        return True
    try:
        for line in Path('/proc/1/cgroup').read_text(encoding='utf-8').splitlines():
            if any(k in line for k in ('docker', 'containerd', 'kubepods', 'podman')):
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def restart_service(rep: Reporter) -> None:
    """重启使新代码生效。

    两种运行形态的重启方式**本质不同**，必须分开处理：

      * systemd（宿主机安装）：`systemctl restart`。注意该操作会连同本更新进程
        一起终止（systemd 默认 KillMode=control-group），所以**必须先写好终态**
        （调用方已 rep.finish(True) 在前）。
      * 容器：**无法自我重启**——容器里没有 systemd，也不能重启自己所在的容器
        （除非挂载了 docker.sock，而那等于把宿主 root 权限交给容器，是更糟的
        选择）。容器由 compose 的 `restart: unless-stopped` 策略在进程退出后
        自动拉起，因此这里的做法是**主动结束进程**，让编排层把新代码加载起来。

    所以容器形态下这里不是"失败"，而是"交给外部"——日志要写清楚，否则用户会
    以为更新没生效。
    """
    if in_container():
        rep.log('检测到容器环境：新代码已就位，正在退出进程以便容器重启策略拉起…')
        rep.log('（容器无法自我重启；compose 的 restart 策略会在进程退出后')
        rep.log('  用新代码重新启动。若长时间未恢复，请在宿主机执行：')
        rep.log('  docker compose restart workbuddy-manager）')
        return
    if os.name == 'nt':
        # Windows 原生部署（deploy/windows-native）：没有 systemd，也不该在这里
        # 报一次「systemctl 重启失败」——那时新代码其实已经就位，用户看到的却是一次
        # 失败的更新（还可能去查一个 Windows 上根本不存在的服务）。如实说明怎么做。
        rep.log('Windows 原生部署：新代码已就位。请关闭当前面板窗口，'
                '重新执行启动脚本（start-workbuddy-manager.cmd）。')
        return
    rc, _ = run(['systemctl', 'restart', SERVICE_NAME], rep=rep, check=False)
    if rc != 0:
        # 服务降权为专用账号后，没有 polkit 授权的机器上会走到这里：
        # 新代码已落盘，只差重启这一步，所以提示必须给出可直接照做的命令。
        raise RuntimeError(
            f'重启服务失败（systemctl 返回 {rc}）。新代码已就位，'
            f'请在服务器上手动执行：systemctl restart {SERVICE_NAME}')
    rep.log('服务已重启')


def _exit_for_restart(rep: Reporter) -> None:
    """容器形态下：结束**整个容器**使新代码生效。

    为什么不是「结束更新进程自己就完事」：更新进程是管理端拉起的子进程，它退出
    后管理端主进程仍在跑**旧代码**（新文件已落盘，但 Python 已把旧模块载入内存）。
    容器里没有办法从内部重启 PID 1 之外的主进程，所以正确做法是结束整个容器，
    由 compose 的 `restart: unless-stopped` 用新代码重新拉起。

    实现：给 PID 1 发 SIGTERM（容器里 PID 1 就是主进程，uvicorn）。
    compose 收到容器退出后会按 restart 策略重启它。
    """
    try:
        os.kill(1, signal.SIGTERM)
        rep.log('已向主进程发送终止信号，容器即将在新代码下重启')
    except Exception as exc:  # noqa: BLE001
        rep.log(f'发送终止信号失败（{exc}）—— 请手动重启容器：'
                'docker compose restart workbuddy-manager', 'warn')
        return
    # 给接收方一点时间优雅退出；随后本进程也退出（容器本该随之结束）
    time.sleep(2)


def read_local_version() -> str:
    """读取当前部署版本（优先取部署时留下的标记，否则读代码里的版本号）。"""
    marker = INSTALL_DIR / '.version'
    if marker.is_file():
        return marker.read_text(encoding='utf-8').strip().lstrip('v')
    main_py = INSTALL_DIR / 'server' / 'main.py'
    if main_py.is_file():
        for line in main_py.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line.startswith('version='):
                return line.split('=', 1)[1].strip().strip("'\"")
    return ''


def _safe_extract(tf: tarfile.TarFile, dest: Path) -> None:
    """解压发布包，严格限制在 dest 之内。

    安全（曾有两个可复现的逃逸，属供应链风险）：更新器以 root 运行、包来自
    Release，一旦有人能控制该 Release，包里的路径逃逸就等于 **root 任意文件写入**
    ——实测可覆盖 /root/.ssh/authorized_keys 直接拿到服务器 SSH 登录。
    原实现有两个绕过：

      1. `str(target).startswith(str(base))` 是**字符串前缀**匹配：
         `/tmp/wbm-update-abc` 与 `/tmp/wbm-update-abc-sibling` 是不同目录，
         但前缀相同 → 成员名 `../wbm-update-abc-sibling/x` 可逃逸。
         （本机实测确实写出了 dest 之外的文件。）
      2. **符号链接成员未校验**：检查阶段链接还没创建，`resolve()` 看不穿它；
         解压时先建 `link -> /任意/目录`，再写 `link/x` 就落到了外面。
         另外 Python 3.12 的 `extractall` 默认 `filter=None`（等价 fully_trusted），
         还会按包内 mode 执行 chmod（含 setuid 位）。

    修法（三条同时做，互为纵深）：
      - 拒绝一切链接类成员（symlink/hardlink）——正常发布包不需要它们
      - 用 `Path.is_relative_to` 取代字符串前缀比较
      - 优先用 `filter='data'`（Python 3.12+）由标准库再兜一层；
        老版本没有该参数，则退化为已做的逐成员校验
    """
    base = dest.resolve()
    for member in tf.getmembers():
        # 1) 链接成员一律拒绝：它们是逃逸的载体，发布包没有理由包含
        if member.issym() or member.islnk():
            raise RuntimeError(f'压缩包含链接成员，已拒绝：{member.name}')
        name = member.name
        # 2) 绝对路径与 .. 直接拒绝（比"算出来再比较"更早、更明确）
        if name.startswith('/') or name.startswith('\\') or '..' in Path(name).parts:
            raise RuntimeError(f'压缩包含非法路径：{name}')
        target = (base / name).resolve()
        # 3) 包含性判断用 is_relative_to，不用字符串前缀
        if target != base and not target.is_relative_to(base):
            raise RuntimeError(f'压缩包含非法路径：{name}')

    try:
        tf.extractall(dest, filter='data')  # Python 3.12+：再由标准库校验一遍
    except TypeError:
        # 老版本 Python 无 filter 参数；上面的逐成员校验已是等价防护
        tf.extractall(dest)
    # 兜底：清掉可能被带进来的特殊权限位（filter='data' 已处理，这里防老版本）
    for root, _dirs, files in os.walk(dest):
        for fn in files:
            p = Path(root) / fn
            try:
                mode = p.stat().st_mode
                if mode & (stat.S_ISUID | stat.S_ISGID):
                    os.chmod(p, stat.S_IMODE(mode) & ~(stat.S_ISUID | stat.S_ISGID))
            except OSError:
                pass


# ── 入口 ─────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description='WorkBuddy Manager 更新')
    parser.add_argument('--target', choices=['manager', 'upstream', 'both'], default='both')
    args = parser.parse_args()

    rep = Reporter(args.target)
    rep.log(f'开始更新（target={args.target}，安装目录={INSTALL_DIR}）')
    if GITHUB_MIRROR:
        # 日志里出现它很重要：镜像不是「隐身加速」，用户得知道流量经过了第三方
        rep.log(f'GitHub 镜像已启用：{GITHUB_MIRROR}（凭据不会发给镜像）')
    if PROXY_UNSUPPORTED:
        # 只报协议名，不回显地址（里面通常带用户名与密码）
        scheme = PROXY_UNSUPPORTED.split('://', 1)[0]
        rep.log(f'已忽略下载代理（{scheme}）：更新器只认 http/https。'
                f'需要更新也走代理时，单独设 WB_UPDATE_PROXY=http://…；'
                f'面板自身的代理不受影响。', 'warn')

    ok = True
    try:
        if args.target in ('upstream', 'both'):
            update_upstream(rep)
        if args.target in ('manager', 'both'):
            update_manager(rep)
    except BaseException as exc:  # noqa: BLE001
        # KeyboardInterrupt（控制台 Ctrl+C/关窗）、SystemExit 等 BaseException
        # 也要捕获并留下终态：只捕 Exception 的话中断会直接穿出去，
        # 状态文件永远停在 running=true，前端就永远等不到「更新未完成」的
        # 结束信号，一直显示「正在更新：执行中」。
        if isinstance(exc, KeyboardInterrupt):
            rep.log('更新被中断（收到 Ctrl+C / 控制台关闭信号）', 'error')
        else:
            rep.log(f'更新失败：{exc}', 'error')
        ok = False
    finally:
        # 收尾兜底：任何退出路径都必须把终态落盘。正常成功路径已在
        # update_manager 内 rep.finish(True) 写过（running=false），这里
        # 幂等跳过；其余情形（含中断）在此补写 ok=false，绝不留悬空状态。
        if rep.state.get('running'):
            rep.finish(ok)
        # 解压出来的发布包活到本次更新结束（两条路径共用），在这里回收
        _cleanup_release_cache()

    rep.log('更新完成' if ok else '更新未完成，请检查上方日志')
    rep.finish(ok)
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
