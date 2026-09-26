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
import urllib.request
from pathlib import Path

from .. import config

STATUS_FILE = config.DATA_DIR / 'update-status.json'
# docker 可用性缓存（(时间, 布尔)）。每次探测要跑 docker info（~50ms），
# 而状态接口会被前端轮询，加 30 秒缓存避免频繁探测。
_docker_ok_cache: tuple[float, bool] | None = None
LOCK_FILE = config.DATA_DIR / 'update.lock'
LOG_FILE = config.DATA_DIR / 'update.log'
# 上游版本固定：写入提交号/标签后，更新上游时检出该版本而不跟随分支。
# 用于上游某提交自身有问题（如 Dockerfile 引用了已删除的文件）时回退。
UPSTREAM_REF_FILE = config.DATA_DIR / 'upstream-ref.txt'

# 锁有效期：超过此时间视为异常退出遗留，允许再次更新
LOCK_TTL = 3600


def _updater_script() -> Path:
    """定位更新脚本：优先仓库内 deploy/update.py。"""
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
        # 当前固定的上游版本（空 = 跟随分支）
        'upstream_ref': upstream_ref(),
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
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:  # noqa: BLE001
        return False


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


def upstream_ref() -> str:
    """当前固定的上游版本（空 = 跟随分支）。"""
    try:
        return UPSTREAM_REF_FILE.read_text(encoding='utf-8').strip()
    except Exception:  # noqa: BLE001
        return ''


def set_upstream_ref(ref: str) -> str:
    """固定/取消固定上游版本。

    ref 为空即取消固定（恢复跟随分支）。只做格式校验：允许
    十六进制提交号、标签名（v1.2.3 之类），拒绝明显有问题的输入——
    这个值会被拼进 git 命令的参数位，不能让任意字符串进来。
    """
    val = (ref or '').strip()
    if not val:
        UPSTREAM_REF_FILE.unlink(missing_ok=True)
        return ''
    if not re.fullmatch(r'[0-9A-Za-z][0-9A-Za-z._/-]{0,79}', val):
        raise ValueError('版本写法不合法（只允许提交号或标签名）')
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPSTREAM_REF_FILE.write_text(val, encoding='utf-8')
    return val


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


def start_update(target: str) -> tuple[bool, str]:
    """启动更新（后台脱离运行）。返回 (是否已启动, 说明)。"""
    if target not in ('manager', 'upstream', 'both'):
        return False, '参数不合法'
    if os.name == 'nt' and config.WB2API_MODE == 'native':
        return False, (
            'Windows 原生部署暂不支持网页一键更新；'
            '请手动替换代码、重新构建前端，然后运行 service-tools.ps1 restart。'
        )
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
    env.update({
        'WB_INSTALL_DIR': str(config.ROOT),
        'WB_UPSTREAM_DIR': str(_upstream_dir()),
        'WB_MANAGER_PORT': str(config.PORT),
        'WB_DATA_DIR': str(config.DATA_DIR),
        'WB_UPDATE_STATUS': str(STATUS_FILE),
        'WB_SERVICE_NAME': os.environ.get('WB_SERVICE_NAME', 'workbuddy-web'),
        # 上游版本固定（空 = 跟随分支）；worker 据此决定检出哪个版本
        'WB_UPSTREAM_REF': upstream_ref(),
        'WB_UPSTREAM_REF_FILE': str(UPSTREAM_REF_FILE),
        # 子进程 stdio 与默认编码强制 UTF-8：中文 Windows 上重定向 stdout 的
        # 默认编码是 cp936（GBK），print('✓'/'⚠️') 会 UnicodeEncodeError 直接
        # 杀死更新进程（2026-09-26 实测：验签通过后即在 ✓ 日志行中断）。
        'PYTHONIOENCODING': 'utf-8',
        'PYTHONUTF8': '1',
    })

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
        return False, f'启动更新失败：{exc}'
    finally:
        try:
            if logfh not in (subprocess.DEVNULL,):  # type: ignore[comparison-overlap]
                logfh.close()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass

    _write_initial_status(target, proc.pid)

    try:
        LOCK_FILE.write_text(str(proc.pid), encoding='utf-8')
    except Exception:  # noqa: BLE001
        pass

    return True, f'更新已开始（pid={proc.pid}）'


def tail_log(lines: int = 80) -> str:
    if not LOG_FILE.is_file():
        return ''
    try:
        content = LOG_FILE.read_text(encoding='utf-8', errors='replace').splitlines()
        return '\n'.join(content[-lines:])
    except Exception:  # noqa: BLE001
        return ''


# ── 版本检测（是否有新版本可更新）────────────────────────
# GitHub 未认证 API 限流为每小时 60 次/IP，因此结果做长缓存，
# 避免每次打开页面都去请求；用户可手动强制刷新。
_VERSION_CACHE_FILE = config.DATA_DIR / 'version-check.json'
VERSION_CACHE_TTL = 6 * 3600          # 6 小时
UPSTREAM_API_REPO = os.environ.get('WB_UPSTREAM_API_REPO') or 'Sliverkiss/workbuddy2api'
# 管理端仓库（owner/name），用于查询最新 Release
MANAGER_REPO = os.environ.get('WB_MANAGER_REPO') or 'ithtelab/workbuddy-manager'
_upstream_api_repo_cache: str | None = None


def _gh_get(url: str, timeout: int = 15) -> object:
    req = urllib.request.Request(url, headers={
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'workbuddy-manager-updater',
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _upstream_api_slug() -> str:
    """把上游仓库地址归一化为 owner/name（用于 GitHub API）。"""
    global _upstream_api_repo_cache
    if _upstream_api_repo_cache:
        return _upstream_api_repo_cache
    slug = UPSTREAM_API_REPO
    raw = (os.environ.get('WB_UPSTREAM_REPO') or '').strip()
    m = re.search(r'github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$', raw)
    if m:
        slug = f'{m.group(1)}/{m.group(2)}'
    _upstream_api_repo_cache = slug
    return slug


def _local_upstream_head(full: bool = False) -> str:
    """本地上游仓库当前的 commit。

    full=True 返回完整 sha（GitHub compare API 用完整 sha 更稳，短 sha 偶发 404）。
    """
    try:
        proc = subprocess.run(['git', 'rev-parse', 'HEAD'],
                              cwd=str(_upstream_dir()), capture_output=True, text=True, timeout=10)
        if proc.returncode == 0:
            sha = proc.stdout.strip()
            return sha if full else sha[:8]
    except Exception:  # noqa: BLE001
        pass
    return ''


# 变更列表最多带回多少条提交说明（界面上展示最近若干条即可）
_CHANGES_LIMIT = 20


def _fetch_upstream_changes(slug: str, base_sha: str, head_sha: str) -> dict:
    """用 compare API 取两者之间的提交数与说明。

    为什么不是只显示「最新一条提交」：上游常一次累积多个提交，
    只显示最新一条会让人以为「就改了这一处」，看不出这批更新到底做了什么
    （功能还是修复、值不值得跟）。

    失败一律返回空结构——版本提示是辅助信息，不能因为拿不到就报错。
    """
    out: dict = {'ahead': 0, 'total': 0, 'changes': [], 'truncated': False}
    if not base_sha or not head_sha or base_sha == head_sha:
        return out
    try:
        data = _gh_get(
            f'https://api.github.com/repos/{slug}/compare/{base_sha}...{head_sha}'
        )
    except Exception:  # noqa: BLE001
        # 本地提交不在远端（如上游 force-push）时会 404，静默降级
        return out
    if not isinstance(data, dict):
        return out

    out['ahead'] = int(data.get('ahead_by') or 0)
    commits = data.get('commits') or []
    if not isinstance(commits, list):
        commits = []
    out['total'] = int(data.get('total_commits') or len(commits))

    items: list[dict] = []
    for c in commits:
        if not isinstance(c, dict):
            continue
        commit = c.get('commit') or {}
        items.append({
            'sha': str(c.get('sha') or '')[:8],
            'subject': str(commit.get('message') or '').split('\n')[0][:120],
            'date': str((commit.get('committer') or {}).get('date') or ''),
        })
    # compare 返回的是时间正序（旧→新）；界面想先看最新的，故倒序
    items.reverse()
    out['changes'] = items[:_CHANGES_LIMIT]
    out['truncated'] = len(items) > _CHANGES_LIMIT
    return out


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


def _fetch_remote_versions() -> dict:
    """向 GitHub 查询管理端与上游的最新版本（不做缓存判断）。"""
    result: dict = {
        'checked_at': int(time.time()),
        'manager': {'latest': '', 'url': '', 'error': ''},
        'upstream': {'latest': '', 'date': '', 'subject': '', 'error': ''},
    }

    # 管理端：最新 Release
    try:
        rel = _gh_get(f'https://api.github.com/repos/{MANAGER_REPO}/releases/latest')
        if isinstance(rel, dict):
            result['manager']['latest'] = str(rel.get('tag_name') or '')
            result['manager']['url'] = str(rel.get('html_url') or '')
    except urllib.error.HTTPError as exc:
        result['manager']['error'] = '未找到 Release' if exc.code == 404 else f'HTTP {exc.code}'
    except Exception as exc:  # noqa: BLE001
        result['manager']['error'] = str(exc)[:120]

    # 上游：默认分支最新提交
    slug = _upstream_api_slug()
    try:
        repo = _gh_get(f'https://api.github.com/repos/{slug}')
        branch = (repo.get('default_branch') if isinstance(repo, dict) else '') or 'master'
        commits = _gh_get(f'https://api.github.com/repos/{slug}/commits?sha={branch}&per_page=1')
        if isinstance(commits, list) and commits:
            c = commits[0]
            head_sha = str(c.get('sha') or '')
            result['upstream']['latest'] = head_sha[:8]
            result['upstream']['date'] = str(((c.get('commit') or {}).get('committer') or {}).get('date') or '')
            result['upstream']['subject'] = str(((c.get('commit') or {}).get('message') or '').split('\n')[0])[:120]
            # 本地已部署的版本与远端不同时，进一步取「领先多少个提交 + 各自说明」
            local_full = _local_upstream_head(full=True)
            if local_full and not head_sha.startswith(local_full) and not local_full.startswith(head_sha):
                result['upstream'].update(_fetch_upstream_changes(slug, local_full, head_sha))
    except urllib.error.HTTPError as exc:
        result['upstream']['error'] = f'HTTP {exc.code}'
    except Exception as exc:  # noqa: BLE001
        result['upstream']['error'] = str(exc)[:120]

    return result


def check_updates(force: bool = False) -> dict:
    """检测是否有新版本。结果缓存 6 小时（GitHub 未认证 API 限流较严）。"""
    cache = _read_cache()
    age = time.time() - float(cache.get('checked_at') or 0)
    if force or not cache or age > VERSION_CACHE_TTL:
        fresh = _fetch_remote_versions()
        # 保留上次成功结果：临时网络故障不应让界面显示「未知」
        for key in ('manager', 'upstream'):
            if fresh[key].get('error') and cache.get(key, {}).get('latest'):
                fresh[key] = {**cache[key], 'error': fresh[key]['error']}
        _write_cache(fresh)
        cache = fresh

    current_manager = current_version()
    local_head = _local_upstream_head()

    m_latest = str(cache.get('manager', {}).get('latest') or '')
    # 只有远端确实更新才提示；不能只判「不相等」，否则当前版本领先于缓存里的
    # 旧 latest 时会冒出降级提示（如 v1.0.5 → v1.0.4）
    manager_has = bool(m_latest) and _version_newer(m_latest, current_manager)

    u_latest = str(cache.get('upstream', {}).get('latest') or '')
    # 有本地 HEAD 时按 commit 比较；否则仅展示远端最新
    upstream_has = bool(u_latest) and bool(local_head) and not u_latest.startswith(local_head) \
        and not local_head.startswith(u_latest)

    return {
        'checked_at': int(cache.get('checked_at') or 0),
        'cached': not force and age <= VERSION_CACHE_TTL,
        'manager': {
            'current': current_manager,
            'latest': m_latest,
            'has_update': manager_has,
            'url': str(cache.get('manager', {}).get('url') or ''),
            'error': str(cache.get('manager', {}).get('error') or ''),
            'repo': MANAGER_REPO,
        },
        'upstream': {
            'current': local_head,
            'latest': u_latest,
            'has_update': upstream_has,
            'date': str(cache.get('upstream', {}).get('date') or ''),
            'subject': str(cache.get('upstream', {}).get('subject') or ''),
            # 领先多少个提交 + 各自说明：上游常一次累积多个提交，
            # 只显示最新一条会让人以为「就改了这一处」
            'ahead': int(cache.get('upstream', {}).get('ahead') or 0),
            'total': int(cache.get('upstream', {}).get('total') or 0),
            'changes': cache.get('upstream', {}).get('changes') or [],
            'truncated': bool(cache.get('upstream', {}).get('truncated')),
            'error': str(cache.get('upstream', {}).get('error') or ''),
            'repo': _upstream_api_slug(),
        },
        'has_any': manager_has or upstream_has,
    }
