"""一键更新的调度与状态读取。

更新会重启管理端自身，因此不能同步等待（响应会被中断）。做法是：
以**脱离父进程**的方式启动 updater，进度写入状态文件，前端轮询读取。

安全：目标固定为 manager / upstream / both 三者之一，路径全部来自服务端
环境变量，**不接受客户端传入的命令或路径**。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import logging

from .. import config, db
from .errtext import err_text

logger = logging.getLogger(__name__)

STATUS_FILE = config.DATA_DIR / 'update-status.json'
# docker 可用性缓存（(时间, 布尔)）。每次探测要跑 docker info（~50ms），
# 而状态接口会被前端轮询，加 30 秒缓存避免频繁探测。
_docker_ok_cache: tuple[float, bool] | None = None
# 上游镜像自报标识的缓存（(时间, 值)）。docker inspect 也是子进程，节奏与
# docker 可用性探测保持一致（30 秒）。
_image_id_cache: tuple[float, str] | None = None
# 发布流水线给上游镜像打的 label，与 Release 正文隐藏标记、包内 .upstream-id 同值。
UPSTREAM_IMAGE_LABEL = 'dev.workbuddy.upstream-id'
LOCK_FILE = config.DATA_DIR / 'update.lock'
LOG_FILE = config.DATA_DIR / 'update.log'

# ── 离线升级包（issue #153）────────────────────────────────
# 内网 / 拿不到 GitHub 的机器：从别处下载发布包，在面板里上传，再走同一套更新流程。
# 文件名固定（客户端不参与命名，只往这一个位置覆盖写），元信息存面板设置。
PACKAGE_FILE = config.DATA_DIR / 'update-package.tar.gz'
PACKAGE_META_KEY = 'update_package'
# 发布包现在约 11 MB，留到 63 MB（= 请求体上限 64 MB 减去 multipart 的边界与
# 表头开销）：上传是往磁盘写，没有上限就等于给了一个能把磁盘填满的入口。
# `server/main.py` 的 MAX_PACKAGE_BODY_BYTES 必须比它大（有测试盯着）。
PACKAGE_MAX_BYTES = 63 * 1024 * 1024


def _read_package_version(path: Path) -> str:
    """从发布包里读出 `.version`（只解那一个成员，不展开整包）。

    更新器（deploy/update.py）里有一份同样的实现 —— 这条刻意不共用：面板不该为了
    读一个字符串去 import 那个脚本。`server/tests/test_update_package.py` 拿同一份
    包断言两边读出来的版本号一致，防漂移。
    """
    import tarfile

    try:
        with tarfile.open(path, 'r:gz') as tf:
            for member in tf:
                name = member.name.replace('\\', '/')
                if name.count('/') == 1 and name.endswith('/.version'):
                    fh = tf.extractfile(member)
                    return fh.read(64).decode('utf-8', 'replace').strip() if fh else ''
    except (OSError, tarfile.TarError) as exc:
        raise ValueError(
            f'升级包读不了（{err_text(exc)}）—— 确认它是发布页面下载的 .tar.gz') from exc
    return ''


def allow_unverified_package() -> bool:
    """是否允许安装**没有发布页摘要**的离线升级包（默认：不允许）。

    为什么默认拒绝：离线包没有签名链，而安装链路本身就能执行任意代码 ——
    包内 `server/requirements.txt` 会在替换代码**之前**被 `pip install -r` 执行
    （可指向任意索引/URL），随后 `server/` 整目录替换并重启进程。也就是说，
    「面板管理员」到「主机代码执行」之间原本只隔着一次摘要核对。
    Windows 更新器对未验证包要求显式 `-AllowUnverified`；这里给同名语义的
    `WB_ALLOW_UNVERIFIED_PACKAGE=1`，只在确实拿不到发布页摘要的离线环境里用。
    """
    value = (os.environ.get('WB_ALLOW_UNVERIFIED_PACKAGE') or '').strip().lower()
    return value in ('1', 'true', 'yes', 'on')


def package_meta() -> dict:
    """已上传升级包的元信息 + 文件是否还在（界面据此决定按钮可不可点）。"""
    raw = db.get_setting(PACKAGE_META_KEY, {}) or {}
    meta = dict(raw) if isinstance(raw, dict) else {}
    meta['present'] = PACKAGE_FILE.is_file()
    if meta['present']:
        try:
            meta['size'] = PACKAGE_FILE.stat().st_size
        except OSError:
            meta['present'] = False
    return meta


def record_package(*, name: str, size: int, sha256: str, version: str,
                   expected_sha256: str = '', uploaded_by: str = '') -> dict:
    meta = {
        'name': name,
        'size': int(size),
        'sha256': sha256,
        # 用户填的「发布页 sha256」：更新时交给更新器核对；空 = 只做了结构校验
        'expected_sha256': expected_sha256,
        'version': version,
        'uploaded_at': int(time.time()),
        'uploaded_by': uploaded_by,
    }
    db.set_setting(PACKAGE_META_KEY, meta)
    return {**meta, 'present': True}


def clear_package() -> None:
    """删掉升级包与记录（更新完成后、或用户手动清掉时调）。"""
    try:
        PACKAGE_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    db.set_setting(PACKAGE_META_KEY, {})


# 锁有效期：超过此时间视为异常退出遗留，允许再次更新
LOCK_TTL = 3600


def _updater_script() -> Path:
    """定位更新脚本。

    **优先 `server/deploy/update.py`**：它随 `server/` 一起被替换，能保证
    「面板代码已经是新版，但根目录 deploy/update.py 还是旧版」时，下一次更新
    用的是新版更新器。旧版更新器没有随包同步逻辑，只靠根目录那份会让
    「随包上游与本机不一致」永远消不掉（issue #240）。
    """
    packaged = config.ROOT / 'server' / 'deploy' / 'update.py'
    if packaged.is_file():
        return packaged
    candidate = config.ROOT / 'deploy' / 'update.py'
    if candidate.is_file():
        return candidate
    # 兼容 install.sh 把 deploy 复制到 APP_DIR 的情况
    return Path(__file__).resolve().parent.parent.parent / 'deploy' / 'update.py'


def in_container() -> bool:
    """是否运行在容器里（与 deploy/update.py 的判定一致）。

    只是一个事实判断；**能力**由 can_control_docker() 决定。
    """
    mode = (os.environ.get('WB_RUN_MODE') or 'auto').strip().lower()
    if mode in ('docker', 'container'):
        return True
    if mode in ('systemd', 'host'):
        return False
    if Path('/.dockerenv').exists():
        return True
    try:
        cg = Path('/proc/1/cgroup').read_text(encoding='utf-8')
        if any(k in cg for k in ('docker', 'containerd', 'kubepods', 'podman')):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def can_control_docker() -> bool:
    """能否操作宿主上的 docker（重启上游容器 / 读上游日志 / 更新上游）。

    判据是**实际能不能跑通 `docker info`**，而不是"在不在容器里"：

      * 宿主部署：装了 docker 且在 docker 组 / root → True
      * 容器挂了 docker.sock：socket 可用 → True（与宿主部署能力对齐）
      * 容器没挂 socket：docker 命令找不到或连不上 daemon → False

    为什么不用「是否容器」来判断（初版就是这么写的，是错的）：容器挂了 socket
    后**完全能**做到这些事，而宿主没装 docker 时反而做不到。按能力判定才不会
    把可用场景误判为不可用（也会在真的不可用时给出准确提示）。
    """
    global _docker_ok_cache
    now = time.time()
    if _docker_ok_cache is not None and now - _docker_ok_cache[0] < 30:
        return _docker_ok_cache[1]
    ok = False
    try:
        proc = subprocess.run(['docker', 'info'], capture_output=True, timeout=8)
        ok = proc.returncode == 0
    except Exception:  # noqa: BLE001
        ok = False
    _docker_ok_cache = (now, ok)
    return ok


# 代理地址里的 userinfo（`http://user:pass@host`）。日志与状态文件会经
# /api/system/update-status 返回给登录用户，凭据不能就这么露出去。
_PROXY_CRED_RE = re.compile(r'(?i)(https?|socks5h?|socks4)://[^/@\s]+@')


def redact_credentials(text: str) -> str:
    """把文本里的 `scheme://user:pass@host` 脱敏成 `scheme://***@host`。

    用于更新日志的出口（读状态、tail 日志）。写入口也在脱敏（update.py 的
    redact_proxy），这里是**存量文件**的兜底 —— 旧版本已经写进去的日志同样会被洗。
    """
    return _PROXY_CRED_RE.sub(lambda m: f'{m.group(1)}://***@', str(text))


def read_status() -> dict:
    """读取进度；附上当前版本与是否正在运行。"""
    container = in_container()
    status: dict = {
        'available': True,
        'running': False,
        'ok': None,
        'step': '',
        'logs': [],
        'version': current_version(),
        'updater_found': _updater_script().is_file(),
        'upstream_dir': str(_upstream_dir()),
        # 运行中上游的稳定标识：镜像 label 优先，回退包内 .upstream-id（空 = 未知）
        'upstream_id': _running_upstream_id(),
        # 运行形态与能力边界
        'in_container': container,
        # 「能否更新上游」按**实际能力**判定（能否操作 docker），不按是否容器：
        # 容器挂了 docker.sock 就能（与宿主部署等价），宿主没装 docker 就不能。
        'can_update_upstream': can_control_docker(),
    }

    if STATUS_FILE.is_file():
        try:
            data = json.loads(STATUS_FILE.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                status.update(data)
                # 进程已消失但状态仍标记运行中 → 需要判断是「真的崩了」还是
                # 「管理端重启把更新进程一起带走了」。systemd 默认
                # KillMode=control-group，`systemctl restart` 会终止整个 cgroup，
                # 更新进程虽已 setsid 也难幸免；此时只要代码已换成目标版本，
                # 就说明更新其实成功了。
                if status.get('running') and not _pid_alive(status.get('pid')):
                    status['running'] = False
                    if _update_landed(data):
                        status['ok'] = True
                        status['step'] = '更新完成（服务已重启）'
                        # 被重启带走的进程来不及写结束时间，用状态文件时间兜底，
                        # 否则「上次更新」会一直显示「从未」
                        if not status.get('finished_at'):
                            try:
                                status['finished_at'] = int(STATUS_FILE.stat().st_mtime)
                            except Exception:  # noqa: BLE001
                                pass
                    else:
                        status['ok'] = False
                        status['step'] = '更新进程异常中断'
        except Exception:  # noqa: BLE001
            pass

    if _lock_active():
        status['running'] = True
    # 出口兜底：旧版本写在 update-status.json 里的日志可能带过凭据，读取时统一洗一遍。
    logs = status.get('logs')
    if isinstance(logs, list):
        # 日志条目是 dict（ts/level/text），不能整体 str()：那会把结构变成
        # Python repr 字符串，前端读 l.text 全部是 undefined，看起来就是
        # 「有行数、正文空白」（issue #224）。只洗 text 字段，结构原样保留。
        cleaned = []
        for item in logs:
            if isinstance(item, dict):
                entry = dict(item)
                entry['text'] = redact_credentials(str(entry.get('text') or ''))
                cleaned.append(entry)
            else:
                cleaned.append(redact_credentials(str(item)))
        status['logs'] = cleaned
    return status


def current_version() -> str:
    """当前部署版本。

    以代码里的版本号为准，`.version` 标记只作部署痕迹：
    两者不一致时说明旧版更新流程没能替换标记（代码已换、标记还是旧的），
    此时采信代码并顺手把标记纠正过来，避免界面一直显示旧版本。
    """
    code = _app_version()
    marker = config.ROOT / '.version'
    marker_ver = ''
    if marker.is_file():
        try:
            marker_ver = marker.read_text(encoding='utf-8').strip()
        except Exception:  # noqa: BLE001
            marker_ver = ''

    def norm(v: str) -> str:
        return str(v or '').strip().lstrip('vV')

    if code and code != '0.0.0' and norm(code) != norm(marker_ver):
        # 自愈：把标记对齐到实际代码版本
        try:
            marker.write_text(f'v{norm(code)}\n', encoding='utf-8')
        except Exception:  # noqa: BLE001
            pass
        return f'v{norm(code)}'
    if marker_ver:
        return marker_ver
    return f'v{code}'


def _app_version() -> str:
    try:
        from ..main import app  # 延迟导入避免循环

        return getattr(app, 'version', '0.0.0')
    except Exception:  # noqa: BLE001
        return '0.0.0'


def _upstream_dir() -> Path:
    """上游目录。**必须与 `config.UPSTREAM_DIR` 同一口径**。

    这里此前是独立推导的（`WB_UPSTREAM_DIR` 优先，否则 `AUTH_DIR.parent`），而
    native 模式又给 `config.UPSTREAM_DIR` 加了另一个回退（`UPSTREAM_CONFIG.parent`）。
    两者在默认配置下巧合一致，但只要用户单独调整 `WB_AUTH_DIR` 或
    `WB_UPSTREAM_CONFIG` 中的一个就会指向**不同目录**，而且没有任何报错：

      · 原生启停脚本按 `config.UPSTREAM_DIR` 找（`WB2API_START_SCRIPT` 的默认值）；
      · 任务脚本与「更新上游」按本函数找。

    结果是一部分功能落在 A 目录、另一部分落在 B 目录，排查时极难定位。
    现在只保留一份推导（config 里那份），本函数退化为引用它。
    """
    return config.UPSTREAM_DIR


def _pid_alive(pid: object) -> bool:
    try:
        pid_int = int(pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    if os.name == 'nt':
        return _pid_alive_windows(pid_int)
    try:
        os.kill(pid_int, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:  # noqa: BLE001
        return False
    # Linux 额外判僵尸：os.kill(pid, 0) 对 zombie 进程**静默成功**（PID 槽位还在，
    # 只是进程已退出、等待父进程回收）。若 update.lock 里记录的 pid 恰好变成
    # zombie（更新进程退出后父进程未及时 wait），_lock_active() 会一直判活，
    # read_status() 把状态强制拉回 running=true，前端永久显示「正在更新」，
    # 而状态文件里其实早就写好了 running=false / step=更新完成。
    # 因此读 /proc/<pid>/stat 的状态字段，'Z' 视为已死。
    try:
        with open(f'/proc/{pid_int}/stat', 'rb') as f:
            raw = f.read()
        # 格式：pid (comm) state ... —— comm 可含空格和右括号，故从最后一个 ')' 后切。
        state = raw.rsplit(b')', 1)[1].split()[0]
        if state == b'Z':
            return False
    except (OSError, IndexError):
        # 读不到 /proc（非 Linux、容器限制等）时退回 os.kill 的结论，保持原行为。
        pass
    return True


def _pid_alive_windows(pid_int: int) -> bool:
    """Windows 探活：OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) + GetExitCodeProcess。

    为什么不用 os.kill(pid, 0)：在 Windows 上 sig=0 等价 CTRL_C_EVENT，走
    GenerateConsoleCtrlEvent 分支——对**已退出但内核对象尚可打开**的 pid
    静默成功（误判存活）。后果是更新进程死后 update.lock 仍被当作活跃，
    read_status 把状态强制拉回 running=true，前端一直显示「正在更新：执行中」。
    而「对象还在但已终止」用 GetExitCodeProcess 就能区分（退出码 != STILL_ACTIVE）。

    OpenProcess 失败按 GetLastError 区分：
      * 87 (ERROR_INVALID_PARAMETER) / 6 (ERROR_INVALID_HANDLE) → pid 不存在；
      * 5  (ERROR_ACCESS_DENIED) → 打不开但进程多半存在（受保护进程），视为
        存活。QUERY_LIMITED 本就是为低权限探测设计的，实际极少被拒——
        宁可保守判活，也不误清锁放进第二个并发更新。
    """
    import ctypes
    import ctypes.wintypes as wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_INVALID_PARAMETER = 87
    ERROR_INVALID_HANDLE = 6
    if pid_int > 0xFFFFFFFF:  # DWORD 上限之外必不存在（也防 ctypes 溢出报错）
        return False

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid_int)
    if not handle:
        err = ctypes.get_last_error()
        if err in (ERROR_INVALID_PARAMETER, ERROR_INVALID_HANDLE):
            return False
        # 其余打不开的情形（含 5）：保守视为存活，避免误判造成并发更新
        return True
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True  # 查询失败：保守视为存活
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _claim_lock() -> bool:
    """原子抢占更新锁：True = 抢到，False = 已有任务 / 无法创建。

    为什么不能「先 _lock_active() 再等 spawn 之后写锁」：两次检查之间的窗口里，
    第二个请求同样能通过检查，于是两个更新进程同时替换 server/（审计
    2026-10-18 的 check-then-spawn 竞态）。O_CREAT|O_EXCL 让「检查 + 占用」
    成为一次原子操作。

    占位先写面板 pid，spawn 成功后改写为 worker pid —— `_lock_active` 按
    「pid 存活 + mtime 未超 TTL」判活，占位只是极短窗口内的安全值。
    """
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # 顺手清掉已失效的旧锁（进程没了 / 超过 TTL）；清理与创建之间仍可能竞争，
    # 所以创建本身必须是原子的。
    _lock_active()
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    except OSError as exc:  # noqa: BLE001
        logger.warning('无法创建更新锁：%s', err_text(exc))
        return False
    try:
        os.write(fd, str(os.getpid()).encode())
    finally:
        os.close(fd)
    return True


def _release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _lock_active() -> bool:
    if not LOCK_FILE.is_file():
        return False
    try:
        if time.time() - LOCK_FILE.stat().st_mtime > LOCK_TTL:
            LOCK_FILE.unlink(missing_ok=True)
            return False
        pid = int(LOCK_FILE.read_text(encoding='utf-8').strip() or 0)
    except Exception:  # noqa: BLE001
        return False
    if _pid_alive(pid):
        return True
    LOCK_FILE.unlink(missing_ok=True)
    return False


def _update_landed(status: dict) -> bool:
    """更新进程消失后，判断这次更新是否其实已经成功。

    管理端更新最后一步是 `systemctl restart`，该操作会终止更新进程本身
    （systemd 默认 KillMode=control-group，会清理整个 cgroup），
    因此「进程不在了」多半是正常收尾，而不是崩溃。用两条证据判断：

    1. 状态里记录了目标版本，且部署出来的版本已等于它；
    2. 日志里出现「管理端已更新到 X，重启服务以生效」——该行只在
       server/、web/out/、deploy/ 全部替换且依赖装完之后才打印，
       看到它就说明只剩重启这一步，而重启正是导致进程消失的动作。
       （旧版更新脚本不写目标版本，此条用于兼容过渡。）
    """
    cur = current_version().lstrip('vV')

    target = str(status.get('target_version') or '').strip().lstrip('vV')
    if target and cur == target:
        return True

    if not cur:
        return False
    for entry in status.get('logs') or []:
        text = str((entry or {}).get('text') or '')
        m = re.search(r'管理端已更新到\s*v?([0-9][0-9.]*)', text)
        if m and m.group(1).strip() == cur:
            return True
    return False


def _write_initial_status(target: str, pid: int) -> None:
    """写入更新刚启动时的占位状态（running=true / ok=null）。

    字段口径与 deploy/update.py 的 Reporter 对齐：read_status 会把它合并进
    返回值；子进程若瞬间死亡，「running 且 pid 已死」分支会用 _update_landed
    收敛出终止态——初始状态没有 target_version、logs 为空，正好落到
    ok=false「更新进程异常中断」，前端就能停下转圈并显示失败。
    """
    now = int(time.time())
    data = {
        'running': True,
        'ok': None,
        'target': target,
        'step': '已启动更新进程',
        'logs': [],
        'started_at': now,
        'finished_at': None,
        'duration': 0,
        'pid': pid,
    }
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        # 与 Reporter.flush 相同的「临时文件 + 原子替换」写法，
        # 避免轮询读到写了一半的 JSON
        tmp = STATUS_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        tmp.replace(STATUS_FILE)
    except Exception:  # noqa: BLE001
        # 状态写不出去也不必拦启动：子进程自己的 Reporter 还会再写
        pass


def unsupported_reason() -> str:
    """当前部署形态不支持网页一键更新时返回原因；支持时返回空串。

    **唯一判据**：`start_update` 的前置拒绝与自动更新的开关/巡检都读这里。
    两处各写一份的话，新增一种部署形态时只改一处，另一处就会拿过期的判断
    去放行或误拒 —— 而这类不一致只会在无人值守时才暴露。
    """
    if os.name == 'nt' and config.WB2API_MODE == 'native':
        return ('Windows 原生部署暂不支持网页一键更新；'
                '请手动替换代码、重新构建前端，然后运行 windows\\service-tools.ps1 restart。')
    return ''


def start_update(target: str, *, source: str = 'release') -> tuple[bool, str]:
    """启动更新（后台脱离运行）。返回 (是否已启动, 说明)。

    `source`：
      · `release` —— 从 GitHub 拉最新发布包（默认，原有的路）；
      · `uploaded` —— 用面板里已上传的离线升级包（issue #153，内网机器用）。
        文件路径由服务端给出（固定文件名），客户端永远不传路径。
    """
    if target not in ('manager', 'upstream', 'both'):
        return False, '参数不合法'
    if source not in ('release', 'uploaded'):
        return False, '参数不合法'
    package_path: Path | None = None
    package_expected = ''
    if source == 'uploaded':
        meta = package_meta()
        if not meta.get('present'):
            return False, '还没有上传升级包（在「离线升级包」里选一个发布包上传后再试）'
        package_path = PACKAGE_FILE
        package_expected = str(meta.get('expected_sha256') or '')
        # 早于「摘要必填」的那版面板可能已经存下了一个无摘要的包：这里再拦一次，
        # 不能只靠上传端点（那层只挡新上传）。判定与上传端点同源，见
        # allow_unverified_package 的说明。
        if not package_expected and not allow_unverified_package():
            return False, (
                '这个离线升级包没有发布页 sha256 可供核对，已拒绝安装。'
                '请重新上传并填写发布页上那串 64 位 sha256；'
                '确实无法核对的离线环境，设 WB_ALLOW_UNVERIFIED_PACKAGE=1 后重试。'
            )
    if unsupported_reason():
        return False, unsupported_reason()
    if target in ('upstream', 'both') and not can_control_docker():
        # 重建上游容器需要操作宿主 docker。判定按**实际能力**（能否跑通
        # docker info），而不是"是否在容器里"：容器挂了 docker.sock 就完全
        # 能做这些事（与宿主部署等价）。
        # 不可用时**前置拒绝并给出替代做法**，而不是让它跑到一半才失败。
        return False, (
            '当前环境无法操作 docker（未安装 docker，或容器没有挂载 '
            '/var/run/docker.sock），因此不能更新上游。'
            '请在宿主机升级上游：cd <上游目录> && docker compose up -d --build；'
            '或使用「仅更新管理端」。'
        )
    if _lock_active():
        return False, '已有更新任务正在执行'

    script = _updater_script()
    if not script.is_file():
        return False, f'未找到更新脚本（{script}）'

    python = sys.executable or shutil.which('python3') or 'python3'
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 清理上次状态，避免前端读到旧进度
    STATUS_FILE.unlink(missing_ok=True)

    env = dict(os.environ)
    # 面板「设置 → 代理」里配的出口代理 / 更新代理交给更新器：它是个独立进程，
    # 只认环境变量（update.py 自己的下载只认 http/https，非 http 的写法那边会
    # 忽略并记一条日志，面板自身的代理不受影响）。
    stored_http_proxy = config.stored_http_proxy()
    stored_update_proxy = db.get_setting('update_proxy', '') or ''
    if stored_http_proxy:
        env['WB_HTTP_PROXY'] = stored_http_proxy
    if stored_update_proxy:
        env['WB_UPDATE_PROXY'] = str(stored_update_proxy)
    # GitHub Token 一并传给更新器（issue #205）：它拉 Release / 校验也走 GitHub API，
    # 未认证同样吃 60/h 的限流。空值不传（避免把空变量覆盖掉部署环境里的那份）。
    _token = github_token()
    if _token:
        env['WB_GITHUB_TOKEN'] = _token
    # GitHub 镜像（issue #216）：面板设置里配了才传，空值不覆盖部署环境里的那份。
    # 走镜像时更新器**不会**把 Token 发出去（见 deploy/update.py 的 _github_headers）。
    _mirror = github_mirror()
    if _mirror:
        env['WB_GITHUB_MIRROR'] = _mirror
    env.update({
        'WB_INSTALL_DIR': str(config.ROOT),
        'WB_UPSTREAM_DIR': str(_upstream_dir()),
        'WB_MANAGER_PORT': str(config.PORT),
        'WB_DATA_DIR': str(config.DATA_DIR),
        'WB_UPDATE_STATUS': str(STATUS_FILE),
        'WB_SERVICE_NAME': os.environ.get('WB_SERVICE_NAME', 'workbuddy-web'),
        # 子进程 stdio 与默认编码强制 UTF-8：中文 Windows 上重定向 stdout 的
        # 默认编码是 cp936（GBK），print('✓'/'⚠️') 会 UnicodeEncodeError 直接
        # 杀死更新进程（2026-09-26 实测：验签通过后即在 ✓ 日志行中断）。
        'PYTHONIOENCODING': 'utf-8',
        'PYTHONUTF8': '1',
    })
    if package_path is not None:
        # 离线升级包：更新器不查 GitHub，版本号从包里读；用户填过发布页 sha256
        # 的话一并传下去让它核对（没填就只做结构校验，更新器会在日志里说明）。
        env['WB_UPDATE_PACKAGE'] = str(package_path)
        env.pop('WB_UPDATE_PACKAGE_SHA256', None)
        if package_expected:
            env['WB_UPDATE_PACKAGE_SHA256'] = package_expected

    # 原子抢占：到这里所有前置校验都过了，下一行就是 spawn。抢不到说明另一个
    # 请求刚好抢先（上面的 _lock_active 只是快速失败，两个请求可以同时通过）。
    if not _claim_lock():
        return False, '已有更新任务正在执行'

    try:
        logfh = open(LOG_FILE, 'ab')
    except Exception:  # noqa: BLE001
        logfh = subprocess.DEVNULL  # type: ignore[assignment]

    try:
        proc = subprocess.Popen(
            [python, str(script), '--target', target],
            cwd=str(config.ROOT),
            env=env,
            stdout=logfh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            **({'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
   if os.name == 'nt' else {'start_new_session': True}),
        )
    except Exception as exc:  # noqa: BLE001
        _release_lock()   # 抢到锁但没起来：必须放掉，否则界面永远显示「已有任务」
        return False, f'启动更新失败：{err_text(exc)}'
    finally:
        try:
            if logfh not in (subprocess.DEVNULL,):  # type: ignore[comparison-overlap]
                logfh.close()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass

    _write_initial_status(target, proc.pid)

    try:
        # 锁里换成 worker 的 pid：_lock_active 靠它判断「更新进程还在不在」。
        LOCK_FILE.write_text(str(proc.pid), encoding='utf-8')
    except Exception:  # noqa: BLE001
        pass

    return True, f'更新已开始（pid={proc.pid}）'


def clear_status() -> tuple[bool, str]:
    """清除上次更新的结果与日志。返回 (是否成功, 说明)。

    为什么需要它（issue #105）：一次失败的更新会**长期驻留** —— 状态文件只在下次
    `start_update` 时被删，`update.log` 只追加从不截断，于是界面上那条「更新未完成」
    与日志永远擦不掉，用户只能进容器手删文件。而失败记录恰恰是最想清掉的东西。
    """
    if _lock_active():
        # 运行中清状态会让前端把「正在更新」看丢；等它结束（或锁过期）再清。
        return False, '更新正在进行中，等它结束后再清除'
    for f in (STATUS_FILE, LOG_FILE):
        try:
            f.unlink(missing_ok=True)
        except OSError as exc:
            return False, f'清除失败：{err_text(exc)}'
    return True, '已清除上次更新的结果与日志'


def tail_log(lines: int = 80) -> str:
    if not LOG_FILE.is_file():
        return ''
    try:
        content = LOG_FILE.read_text(encoding='utf-8', errors='replace').splitlines()
        tail = '\n'.join(content[-lines:])
        return redact_credentials(tail)
    except Exception:  # noqa: BLE001
        return ''


# ── 版本检测（是否有新版本可更新）────────────────────────
# GitHub 未认证 API 限流为每小时 60 次/IP，因此结果做长缓存，
# 避免每次打开页面都去请求；用户可手动强制刷新。
_VERSION_CACHE_FILE = config.DATA_DIR / 'version-check.json'
VERSION_CACHE_TTL = 6 * 3600          # 6 小时
# 「新版本已发布但还没有签名」时的短 TTL：这时结论随时会变（维护者签完就好），
# 用 5 分钟换来界面及时恢复。
_ASSETS_PENDING_TTL = 5 * 60
# 管理端仓库（owner/name），用于查询最新 Release
MANAGER_REPO = os.environ.get('WB_MANAGER_REPO') or 'ithtelab/workbuddy-manager'


def github_token() -> str:
    """生效的 GitHub Token：环境变量优先，其次「设置 → 系统」里填的那份。

    为什么要有面板这一份（issue #205）：未认证的 API 配额只有 60 次/小时/IP，
    容器/系统代理共享出口时极易被同出口的其他人打满，表现为「检测更新」403；
    而代码一直支持认证（限额 5000 次/小时），用户却没地方知道、也没地方填 ——
    只能翻源码或更新日志。面板填的这份与部署时注入的环境变量等价。
    """
    for key in ('WB_GITHUB_TOKEN', 'GITHUB_TOKEN'):
        v = (os.environ.get(key) or '').strip()
        if v:
            return v
    try:
        return str(db.get_setting('github_token', '') or '').strip()
    except Exception:  # noqa: BLE001
        return ''


def mask_token(token: str) -> str:
    """只露尾 4 位，供界面回显（**绝不回明文**）。"""
    t = (token or '').strip()
    if not t:
        return ''
    return ('*' * max(0, len(t) - 4)) + t[-4:]


# ── GitHub 镜像（issue #216）──────────────────────────────────
# 国内机器直连 github.com / api.github.com 常超时。gh-proxy 这类服务的用法是
# 「镜像基址 + 完整原始地址」：
#   https://gh-proxy.com/https://api.github.com/repos/<owner>/<repo>/releases/latest
# 只在部署者显式配置时启用 —— 镜像会把请求转给第三方。
GITHUB_PREFIXES = (
    'https://github.com/',
    'https://api.github.com/',
    'https://codeload.github.com/',
    'https://raw.githubusercontent.com/',
    'https://objects.githubusercontent.com/',
)


def normalize_mirror(value: object) -> str:
    """把镜像基址规范成 `https://host/`；非法值返回空串（= 不启用）。

    带查询串 / 片段的地址直接拒绝：拼出来的必须是「基址 + 完整原始地址」，
    多一个 `?` 或 `#` 会让后半截被当成镜像自己的参数。
    """
    raw = str(value or '').strip()
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


def stored_github_mirror() -> str:
    """面板设置里存的镜像；空串 = 没设过。"""
    try:
        return str(db.get_setting('github_mirror', '') or '').strip()
    except Exception:  # noqa: BLE001
        return ''


def github_mirror() -> str:
    """生效的镜像基址：面板设置优先，其次环境变量 WB_GITHUB_MIRROR。"""
    return normalize_mirror(stored_github_mirror()
                            or os.environ.get('WB_GITHUB_MIRROR') or '')


def _mirror_url(url: str, mirror: str) -> str:
    if not mirror or not url.startswith(GITHUB_PREFIXES):
        return url
    return f'{mirror}{url}'


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不让 urllib 自动跟随 302：我们要读 Location 里的 tag（issue #226）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def _release_latest_via_redirect(url: str) -> dict:
    """API 不可用时的回退：读 github.com/.../releases/latest 的 302 Location。

    很多企业网关会 TLS 拦截 api.github.com 并返回 HTML 提示页，但放行
    github.com 的网页与资产下载。这条路径不消耗 API 配额，只需要一个 tag。
    """
    m = re.match(r'https://api\.github\.com/repos/([^/]+)/([^/]+)/releases/latest$', url)
    if not m:
        raise ValueError('not a releases/latest API URL')
    repo = f'{m.group(1)}/{m.group(2)}'
    page = f'https://github.com/{repo}/releases/latest'
    req = urllib.request.Request(page, headers={'User-Agent': 'workbuddy-manager-updater'})
    try:
        with _NO_REDIRECT_OPENER.open(req, timeout=15):
            raise RuntimeError('releases/latest 没有返回重定向')
    except urllib.error.HTTPError as exc:
        if exc.code not in (301, 302, 303, 307, 308):
            raise
        location = str(exc.headers.get('Location') or '')
        tag = ''
        marker = '/tag/'
        if marker in location:
            tag = location.split(marker, 1)[1].split('?', 1)[0].strip('/')
        if not tag:
            raise RuntimeError('releases/latest 的 Location 里没有 tag')
        return {
            'tag_name': tag,
            'html_url': f'https://github.com/{repo}/releases/tag/{tag}',
            'assets': [],
            '_fallback': True,   # 没有 assets 信息：assets_ready 保持未知
        }


def _gh_get(url: str, timeout: int = 15) -> object:
    """取一个 GitHub JSON；配了镜像就先走镜像，失败再直连（issue #216）。

    **走镜像时不带 Authorization**：镜像会把请求转给第三方，Token 不该为了
    提速离开本机；镜像按自己的出口访问 GitHub，也不需要我们的配额。

    **404 不回退**：镜像明确回答「仓库/Release 不存在」时，直连只会给出同一个
    结论（或更糟 —— 被限流的 403 把真实原因盖掉）。实测踩到过：上游仓库已删除，
    镜像返回 404，回退直连拿到 403，界面于是显示「HTTP 403」，看起来像限流，
    与事实（仓库没了）完全无关。403/429 这类「可能换条路就好」的才值得回退。
    """
    mirror = github_mirror()
    candidates = [url]
    mirrored = _mirror_url(url, mirror)
    if mirrored != url:
        candidates.insert(0, mirrored)

    last: BaseException | None = None
    for candidate in candidates:
        headers = {
            'Accept': 'application/vnd.github+json',
            'User-Agent': 'workbuddy-manager-updater',
        }
        # 认证后 5000 次/小时；未认证仅 60 次/小时/IP——共享出口（TUN/系统代理）
        # 极易被同出口的其他用户打满，表现为设置页"检测更新"偶发 403
        if candidate == url:
            token = github_token()
            if token:
                headers['Authorization'] = f'Bearer {token}'
        req = urllib.request.Request(candidate, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise
            last = exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # ValueError 涵盖「镜像返回了非 JSON 错误页」；两类都该换下一条候选
            last = exc
    assert last is not None
    # 最后一层：API 完全不可用时，用 github.com 的 302 回退拿 tag（issue #226）。
    if '/releases/latest' in url:
        try:
            return _release_latest_via_redirect(url)
        except Exception:  # noqa: BLE001 —— 回退也失败时保留原始错误
            pass
    raise last


# Release 正文最多回传多少字符（界面折叠展示，不做 Markdown 渲染）
_RELEASE_NOTES_LIMIT = 4000
_UPSTREAM_ID_RE = re.compile(r'<!--\s*upstream-id:\s*([0-9a-f]{64})\s*-->')


def _parse_version(v: str) -> tuple[int, ...] | None:
    """把 v1.2.3 / 1.2 解析成可比较的数字元组；含非数字段则返回 None。"""
    s = re.split(r'[-+]', str(v or '').strip().lstrip('vV'), 1)[0]
    parts = [p for p in s.split('.') if p != '']
    if not parts:
        return None
    out: list[int] = []
    for p in parts:
        if not p.isdigit():
            return None
        out.append(int(p))
    return tuple(out)


def _version_newer(remote: str, current: str) -> bool:
    """remote 是否**严格新于** current。

    这里必须是「大于」而不是「不相等」：低于或等于当前版本都不能提示更新，
    否则回滚/降级场景会冒出「v1.0.5 → v1.0.4」这种把降级当更新的提示。
    两端有一个解析不了时返回 False——宁可不提示，也不误报。
    """
    r = _parse_version(remote)
    c = _parse_version(current)
    if r is None or c is None:
        return False
    n = max(len(r), len(c))
    return r + (0,) * (n - len(r)) > c + (0,) * (n - len(c))


def _read_cache() -> dict:
    if not _VERSION_CACHE_FILE.is_file():
        return {}
    try:
        data = json.loads(_VERSION_CACHE_FILE.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_cache(data: dict) -> None:
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _VERSION_CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    except Exception:  # noqa: BLE001
        pass


def _local_upstream_id() -> str:
    """本机随包上游的稳定标识（打包时写入 upstream/.upstream-id）。

    老部署没有这个文件时返回空串 = 未知，不能据此判断「上游有变化」。
    """
    try:
        path = _upstream_dir() / '.upstream-id'
        return path.read_text(encoding='utf-8').strip() if path.is_file() else ''
    except Exception:  # noqa: BLE001
        return ''


def _image_upstream_id() -> str:
    """运行中上游容器镜像自报的 upstream-id（镜像部署形态的权威标识）。

    发布流水线给上游镜像打同一个 `dev.workbuddy.upstream-id` label，值与
    Release 正文隐藏标记、包内 `.upstream-id` 同源。镜像部署的机器磁盘上
    **没有源码目录**（只挂 config.json / auths / data），只有这个 label 能
    回答「跑的是哪一版」；没有该 label 的镜像或源码部署返回空串，由调用方
    回退到 `.upstream-id`，判定语义与之前一致。
    """
    global _image_id_cache
    if config.WB2API_MODE != 'docker':
        return ''
    now = time.time()
    if _image_id_cache is not None and now - _image_id_cache[0] < 30:
        return _image_id_cache[1]
    value = ''
    if can_control_docker():
        try:
            proc = subprocess.run(
                ['docker', 'inspect', '--format',
                 f'{{{{index .Config.Labels "{UPSTREAM_IMAGE_LABEL}"}}}}',
                 config.WB2API_CONTAINER],
                capture_output=True, text=True, timeout=8)
            raw = (proc.stdout or '').strip()
            # 镜像没有这个 label 时，模板渲染成 "<no value>"
            if proc.returncode == 0 and raw and raw != '<no value>':
                value = raw
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning('读取上游镜像 %s 失败：%s', UPSTREAM_IMAGE_LABEL, err_text(exc))
    _image_id_cache = (now, value)
    return value


def _running_upstream_id() -> str:
    """运行中上游的稳定标识：镜像 label 优先，回退部署目录标记。"""
    return _image_upstream_id() or _local_upstream_id()


def upstream_changed(release_upstream_id: str, running_upstream_id: str,
                     manager_stale: bool = False) -> bool:
    """Release 包内上游与**运行中**上游是否不同源。

    运行侧标识为空按「未知 = 需要同步」处理（老部署两者都没有）；本次检测
    失败（stale）时不下结论，避免拿上一个 Release 的缓存误报（issue #240）。
    """
    if not release_upstream_id or manager_stale:
        return False
    return not running_upstream_id or release_upstream_id != running_upstream_id


def _fetch_remote_versions() -> dict:
    """向 GitHub 查询管理端最新 Release（不做缓存判断）。

    上游源码随管理端发布包分发，没有独立的远端版本可查；这里只查管理端。
    """
    result: dict = {
        'checked_at': int(time.time()),
        'manager': {'latest': '', 'url': '', 'assets_ready': None, 'error': '',
                    'release_notes': '', 'published_at': '', 'upstream_id': ''},
    }

    try:
        rel = _gh_get(f'https://api.github.com/repos/{MANAGER_REPO}/releases/latest')
        if isinstance(rel, dict):
            result['manager']['latest'] = str(rel.get('tag_name') or '')
            result['manager']['url'] = str(rel.get('html_url') or '')
            body = str(rel.get('body') or '')
            # Release 正文：更新前就能看到「本版本需要什么」，不必等装完再翻
            # 本地 CHANGELOG（issue #217）。只回传纯文本，界面不渲染 Markdown。
            # 隐藏的 upstream-id 标记不能出现在界面（它是给程序读的）。
            result['manager']['release_notes'] = _UPSTREAM_ID_RE.sub('', body)[:_RELEASE_NOTES_LIMIT]
            result['manager']['published_at'] = str(rel.get('published_at') or '')
            m_id = _UPSTREAM_ID_RE.search(body)
            result['manager']['upstream_id'] = m_id.group(1) if m_id else ''
            # 产物是否就位：Release 一般由 CI 连产物一起创建，但「手工建了 Release
            # 还没上传产物」也会被 releases/latest 看到（issue #127/#129 是同一类
            # 窗口期问题）—— 在版本检查里就说清，别让用户点下去才拿到 404。
            assets = rel.get('assets') or []
            # 302 回退拿不到资产清单：标成未知，不能误报「产物未就位」。
            result['manager']['assets_ready'] = None if rel.get('_fallback') else any(
                str((a or {}).get('name') or '').endswith('.tar.gz')
                for a in assets if isinstance(a, dict))
    except urllib.error.HTTPError as exc:
        result['manager']['error'] = '未找到 Release' if exc.code == 404 else f'HTTP {exc.code}'
    except Exception as exc:  # noqa: BLE001
        result['manager']['error'] = str(exc)[:120]

    return result


def check_updates(force: bool = False) -> dict:
    """检测是否有新版本。结果缓存 6 小时（GitHub 未认证 API 限流较严）。"""
    cache = _read_cache()
    age = time.time() - float(cache.get('checked_at') or 0)
    # 已知「该版本产物还没就位」时用短 TTL：产物一上传（通常几分钟）界面就该恢复
    # 正常，而不是继续拿旧结论劝退用户。
    ttl = (_ASSETS_PENDING_TTL if cache.get('manager', {}).get('assets_ready') is False
           else VERSION_CACHE_TTL)
    if force or not cache or age > ttl:
        fresh = _fetch_remote_versions()
        # 保留上次成功结果：临时网络故障不应让界面显示「未知」
        #
        # 但**必须标记这是旧结果**（issue #204）：把上一次的 latest 当成这次检测的
        # 结论，界面就会在 403 限流时照样报「已是最新版本」—— 把一次失败说成了
        # 结论，用户会以为确实没有新版本。stale/stale_at 让界面能如实说
        # 「这次没拿到，下面是 X 时刻的旧结果」。
        if fresh['manager'].get('error') and cache.get('manager', {}).get('latest'):
            old_at = int(cache.get('checked_at') or 0)
            fresh['manager'] = {**cache['manager'], 'error': fresh['manager']['error'],
                                'stale': True, 'stale_at': old_at}
        _write_cache(fresh)
        cache = fresh

    current_manager = current_version()
    m_latest = str(cache.get('manager', {}).get('latest') or '')
    # 只有远端确实更新才提示；不能只判「不相等」，否则当前版本领先于缓存里的
    # 旧 latest 时会冒出降级提示（如 v1.0.5 → v1.0.4）
    manager_has = bool(m_latest) and _version_newer(m_latest, current_manager)
    # 这次检查失败、current 值是上次缓存时，不能拿它断言「上游不一致」：
    # 缓存可能还是上一个 Release 的 upstream-id，会把已经同步好的机器一直
    # 显示成「需要同步」。下次检查成功后再判断。
    manager_stale = bool(cache.get('manager', {}).get('stale'))
    running_upstream_id = _running_upstream_id()
    release_upstream_id = str(cache.get('manager', {}).get('upstream_id') or '')

    return {
        'checked_at': int(cache.get('checked_at') or 0),
        'cached': not force and age <= VERSION_CACHE_TTL,
        'manager': {
            'current': current_manager,
            'latest': m_latest,
            'has_update': manager_has,
            'url': str(cache.get('manager', {}).get('url') or ''),
            'assets_ready': cache.get('manager', {}).get('assets_ready'),
            'release_notes': str(cache.get('manager', {}).get('release_notes') or ''),
            'published_at': str(cache.get('manager', {}).get('published_at') or ''),
            # 远端包内上游 id 与运行中上游不一致（镜像 label / 包内标记）：
            # 界面把主按钮改成「全部更新」，避免面板升了、上游还是旧代码。
            'upstream_changed': upstream_changed(
                release_upstream_id, running_upstream_id, manager_stale),
            'error': str(cache.get('manager', {}).get('error') or ''),
            # stale=True：这一节的值来自**上一次**成功检测（本次失败了），
            # 界面不能据此下「已是最新」的结论（issue #204）。
            'stale': bool(cache.get('manager', {}).get('stale')),
            'stale_at': int(cache.get('manager', {}).get('stale_at') or 0),
            'repo': MANAGER_REPO,
        },
        # 上游不再有独立远端版本：current 是随包上游的稳定标识，has_update
        # 恒为 false；是否需要同步由 manager.upstream_changed 表达。
        'upstream': {
            'current': running_upstream_id[:8],
            'latest': '',
            'has_update': False,
            'date': '',
            'subject': '',
            'ahead': 0,
            'total': 0,
            'changes': [],
            'truncated': False,
            'error': '',
            'stale': False,
            'stale_at': 0,
            'repo': '',
        },
        'has_any': manager_has,
    }
