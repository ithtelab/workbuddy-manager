"""本机一键导入：把一把**刚创建**的密钥写进本机的 cc-switch / ZCode 配置。

这是 docs/proposal-key-onclick-import.md §4「路径 A」的实现，也是那条路线里
**唯一会写用户数据**的一步，所以本模块的每一条约束都是为了"宁可失败，也不
静默改坏用户的工具配置"。

两条导入路径：先深链，后直写
------------------------------
**① 官方深链（首选，只有 cc-switch 有）**：`ccswitch://v1/import?…` 是 cc-switch
自己公开的接口。把配置塞进链接交给操作系统的协议处理器，**由 cc-switch 自己
校验、入库、并弹确认框**。这条路的三个好处：

  · 不必赌它的表结构，也不必猜 `endpointAutoSelect` 这类语义未公开的开关；
  · 不需要"写盘窗口"，所以**完全不用关掉用户正在用的客户端**；
  · 客户端没开着时会被这条链接**拉起来**——正是"一键唤起再导入"想要的。

**② 直接写配置（回退）**：没有深链的客户端（ZCode）、或旧版没注册协议的
cc-switch，才走这条路。它必须面对下面这条硬约束。
路由侧由 `mode` 选择（`auto` = 能深链就深链），见 routers/keys.py。

为什么是「可选特性、默认关闭」
------------------------------
写盘属外部副作用：写坏了，用户的客户端可能连原本能用的供应商一起丢。因此

  · 面板侧由 `WB_LOCAL_IMPORT` 开关控制（默认关，见 config.LOCAL_IMPORT_ENABLED）；
  · 端点侧只接受**回环来源**（面板必须与客户端同机，见 routers/keys.py）；
  · 本模块自身只做"能不能写"的判断，不做"要不要写"的判断——开关归调用方。

「客户端正在运行」为什么是硬约束，以及怎么不让用户自己动手
----------------------------------------------------------
（这条只约束**直接写配置**那条路；走深链时客户端在不在运行都无所谓。）

两个客户端都是"运行时把自身配置读进内存、保存时整份回写"的形态：

  · ZCode 会重写 `provider_config.json`（用户改任意一项就落盘）；
  · cc-switch 持有 SQLite 连接与内存态。

我们在它运行时写入，结果会被它随后的保存**整体覆盖**——用户看到"导入成功"
然后配置莫名其妙消失，比直接报错难查得多。所以**写入那一刻它必须是退出的**，
这一条不能让步。

但"请你自己先退出、再回来点一次"是很差的体验：用户要来回切两次窗口，还常常
关错窗口（关成了一个同名的另一个实例）。所以本模块提供 `client_closed()`：
调用方显式声明 `close_running` 后，我们**先替用户把它关掉**（优雅关闭优先，
超时才强杀），写完**无论成败都自动拉起来**——客户端回到用户离开时的状态。

前提是能定位到它的可执行文件（`protocol_handler` / `find_executable`：
前者读注册表里"谁响应 ccswitch://"，客户端没开也准；后者读正在跑的进程的
镜像路径。都不依赖 psutil / PowerShell）。**定位不到就不关**：
宁可让用户手动退出一次，也不能把客户端关掉却拉不起来。同理，`close_running`
没有默认打开——终止别人的进程不该因为"默认值更方便"而被隐式触发。

为什么不猜 `endpointAutoSelect`
-------------------------------
cc-switch 的 `providers.meta` 里有 `endpointAutoSelect` 与 `commonConfigEnabled`
两个语义未公开的开关，`provider_endpoints` 表则是它的端点列表。实测本机
`provider_health` 表为空、日志里也没有端点探测记录，**无法验证**打开
`endpointAutoSelect` 后 cc-switch 会不会用端点表里的 URL 去替换我们精心算好的
base url（claude 要根地址、codex 要带 /v1，见 keyexport）。

猜错的后果是"流量被悄悄改到另一个地址上"，属于最难排查的一类故障。所以：
端点行照写（保持界面里「端点」一列不为空），但**显式关掉自动选择**，
明确不使用这个我们无法验证的机制。若 cc-switch 作者能说明语义，可以再开。
"""
from __future__ import annotations

import base64
import collections
import contextlib
import ctypes
import json
import locale
import os
import shutil
import sqlite3
import string
import subprocess
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

CLIENTS = ('ccswitch', 'zcode')

# 判定"客户端是否在运行"的进程名。Windows 侧 `tasklist /FI IMAGENAME eq` 需要
# 带 .exe 的名字；POSIX 侧用不带扩展名的进程名。
PROCESS_NAMES = {
    'ccswitch': ('cc-switch.exe', 'cc-switch'),
    'zcode': ('ZCode.exe', 'zcode'),
}

# 客户端的可执行文件路径覆盖（自动关闭后要靠它把人家的客户端重新拉起来）。
EXE_ENV = {'ccswitch': 'WB_CCSWITCH_EXE', 'zcode': 'WB_ZCODE_EXE'}

# 自动检测扫描哪些根目录（`os.pathsep` 分隔）。留空走内置的常见位置 + 固定盘。
SCAN_ROOTS_ENV = 'WB_CLIENT_SCAN_ROOTS'
# 上次检测到的路径存在哪（缓存：换机器/重装后不用每次重扫）。
CACHE_FILE_ENV = 'WB_CLIENT_PATHS_FILE'

_PROC_TIMEOUT = 15
_BACKUP_SUFFIX = '.wbimport'

# 扫描预算：用户主动点「自动检测」才走这条路，宁可给一句"没找到"，
# 也不能让面板在一个装满文件的盘上卡几十秒。
_SCAN_BUDGET_DIRS = 4000
_SCAN_BUDGET_SECONDS = 6.0
# 各根目录往下挖几层。用户目录要够深（AppData\Local\Programs\<app>\<exe>），
# 盘符根目录只挖一层——不然一个 D 盘就够把预算耗光。
_SCAN_DEPTH_USER = 3
_SCAN_DEPTH_DRIVE = 1
_SCAN_SKIP_DIRS = {
    'windows', '$recycle.bin', 'system volume information', 'programdata',
    'appdata', 'node_modules', '.git', '.svn', '.venv', '__pycache__',
    'recovery', 'perflogs', 'msocache', 'windows.old',
}
# 从注册表字符串里抠路径时要挡掉的东西：这些都是卸载器 / 安装器 / 更新器，
# 拉起来的是卸载向导而不是客户端（实测 ZCode 的 DisplayIcon 就指向卸载器）。
_EXE_REJECT = ('unins', 'installer', 'setup', 'update', 'crashpad', 'uninst')


class ImportRefused(Exception):
    """拒绝写入（可预期的原因，调用方据此给用户一句能照做的提示）。

    `reason` 是稳定的机器可读码，`message` 直接面向用户。
    """

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


# ── 路径 ────────────────────────────────────────────────────────────────

def ccswitch_dir() -> Path:
    """cc-switch 的数据目录（可用 WB_CCSWITCH_DIR 覆盖，便于测试/非默认安装）。"""
    override = os.environ.get('WB_CCSWITCH_DIR', '').strip()
    return Path(override) if override else Path.home() / '.cc-switch'


def zcode_dir() -> Path:
    """ZCode 的配置目录（可用 WB_ZCODE_DIR 覆盖）。"""
    override = os.environ.get('WB_ZCODE_DIR', '').strip()
    return Path(override) if override else Path.home() / '.zcode' / 'v2'


def target_path(client: str) -> Path:
    """该客户端被写入的文件。找不到就是「未安装」。"""
    if client == 'ccswitch':
        return ccswitch_dir() / 'cc-switch.db'
    if client == 'zcode':
        return zcode_dir() / 'provider_config.json'
    raise ValueError(f'未知客户端 {client!r}，应为 {CLIENTS} 之一')


# ── 进程检测 ────────────────────────────────────────────────────────────

def _decode(raw: bytes) -> str:
    """解码子进程输出。

    `tasklist` 在中文 Windows 上按**系统 ANSI 码页**（936）输出，直接按 UTF-8
    解会抛 UnicodeDecodeError（实测踩过）。逐候选编码尝试，最后兜底不抛。
    """
    candidates = []
    if sys.platform == 'win32':
        candidates.append('mbcs')
    candidates.append(locale.getpreferredencoding(False) or 'utf-8')
    candidates.append('utf-8')
    for enc in candidates:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode('utf-8', 'replace')


def _windows_pids(names: tuple[str, ...]) -> list[int]:
    """该进程名当前的所有 PID。

    `tasklist` 无匹配时输出一行「信息: 没有运行的任务匹配指定标准。」，有匹配
    时是 CSV 行（以引号开头）。用引号判定，不依赖界面语言。
    """
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    pids: list[int] = []
    for name in names:
        proc = subprocess.run(
            ['tasklist', '/FI', f'IMAGENAME eq {name}', '/FO', 'CSV', '/NH'],
            capture_output=True, timeout=_PROC_TIMEOUT, creationflags=flags,
        )
        for line in _decode(proc.stdout).splitlines():
            line = line.strip()
            if not line.startswith('"'):
                continue
            cells = [c.strip('"') for c in line.split('","')]
            if len(cells) >= 2 and cells[1].isdigit():
                pids.append(int(cells[1]))
    return pids


def _running_windows(names: tuple[str, ...]) -> bool:
    return bool(_windows_pids(names))


def _running_posix(names: tuple[str, ...]) -> bool:
    for name in names:
        for args in (['pgrep', '-x', name], ['pgrep', '-f', name]):
            try:
                proc = subprocess.run(args, capture_output=True,
                                      timeout=_PROC_TIMEOUT)
            except OSError:
                continue
            if proc.returncode == 0 and proc.stdout.strip():
                return True
    return False


def is_running(client: str) -> bool | None:
    """客户端是否在运行。**None = 无法判定**（调用方必须按"不能写"处理）。

    宁可误判为"在运行"而让用户手动确认，也不要漏判后写坏配置：两种误判的
    代价不对称。
    """
    names = PROCESS_NAMES.get(client)
    if not names:
        raise ValueError(f'未知客户端 {client!r}')
    try:
        if sys.platform == 'win32':
            return _running_windows(names)
        return _running_posix(names)
    except (OSError, subprocess.SubprocessError):
        return None


# ── 可执行文件定位 / 关闭 / 重新拉起 ──────────────────────────────────
#
# 这一节是整个面板里**唯一会终止并启动别的进程**的地方，所以规则先写在这：
#
#   · 定位不到可执行文件就**不关**——关掉却拉不起来，等于把用户的工具弄没了；
#   · 先优雅关闭（`taskkill` 不带 `/F` = 发关闭请求，让客户端自己收尾），
#     超时后才强杀。SQLite 是崩溃安全的，强杀不会写坏库，丢的只是没落盘的界面状态；
#   · `finally` 里一定要拉起来：不能因为某次导入失败，就把用户的客户端留在关闭状态。

_POLL = 0.25            # 进程状态的轮询间隔（秒）
# 优雅关闭的等待上限。实测 cc-switch 3.x 是托盘应用：它**不理会** WM_CLOSE，
# 只是把窗口藏进托盘，所以这一档在它身上必然是白等（实测 16.3s 里绝大多数
# 是这段等待）。但别的应用（以及 ZCode）可能确实会正常退出，所以不能省掉这一
# 步——只把上限收到 6 秒：正常退出的应用一秒内就退了，6 秒足够，也不会让用户
# 盯着"导入中"发呆。
_STOP_GRACE = 6.0
_KILL_GRACE = 6.0       # 强杀后的等待上限
_BOOT_GRACE = 10.0      # 重新拉起后确认它真的起来的等待上限（实测 1.0s 就起来了）

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_kernel32 = None        # 懒加载：POSIX 上没有 kernel32，模块导入时不能碰它


def _kernel() -> "ctypes.WinDLL":
    """kernel32 懒加载（POSIX 上没有它，模块导入时不能碰）。"""
    global _kernel32
    if _kernel32 is None:
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int,
                                         ctypes.c_uint32]
        kernel32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_uint32)]
        kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.GetLogicalDrives.restype = ctypes.c_uint32
        kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetDriveTypeW.restype = ctypes.c_uint32
        _kernel32 = kernel32
    return _kernel32


def _windows_image_path(pid: int) -> str | None:
    """PID → 可执行文件全路径（Win32 `QueryFullProcessImageName`）。

    为什么不用 PowerShell / wmic：面板不该为了问一个路径去起一个几十毫秒的
    shell，也不该在别人的机器上依赖 PowerShell 的执行策略。读不到（比如目标
    以更高权限运行）就返回 None，由调用方按"定位不到"处理。
    """
    handle = _kernel().OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False,
                                   int(pid))
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_uint32(len(buf))
        if not _kernel().QueryFullProcessImageNameW(handle, 0, buf,
                                                    ctypes.byref(size)):
            return None
        return buf.value or None
    finally:
        _kernel().CloseHandle(handle)


# ── 自动检测客户端位置 ──────────────────────────────────────────────────
#
# 为什么需要"检测"而不是写死一个路径：这个面板是给人本机部署的，而客户端
# 的安装位置完全看用户——绿色版可能解压在 `E:\cc swich\`（目录名还拼错了），
# 安装版在 `%LOCALAPPDATA%\Programs\...`，macOS 在 `/Applications`。
# 写死任何一个都会在别人机器上失效，所以按「可信度从高到低」依次尝试：
#
#   1. 显式覆盖  WB_CCSWITCH_EXE / WB_ZCODE_EXE —— 运维明确指定的，最可信；
#   2. 正在运行的进程 —— 读它自己的镜像路径，绝对准（前提是它开着）；
#   3. 上次检测结果 —— 面板自己记下来的，免得每次重扫；
#   4. 常见安装位 —— 几个固定目录的排列组合，零成本；
#   5. 注册表 —— 安装版的卸载信息 / App Paths 里有真实路径；
#   6. 受限扫描 —— 以上都落空才扫盘（只在用户点「自动检测」时做）。
#
# 前 5 步都很便宜，可以每次调用都跑；第 6 步有目录数 / 时间双预算，是显式动作。

def _paths_cache_file() -> Path:
    override = os.environ.get(CACHE_FILE_ENV, '').strip()
    if override:
        return Path(override)
    from .. import config            # 延迟导入：本模块的单元测试不需要 config
    return config.DATA_DIR / 'client_paths.json'


def _read_cache() -> dict:
    try:
        data = json.loads(_paths_cache_file().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _remember(client: str, exe: Path, source: str) -> None:
    """记住这次检测到的路径（失败就算了，这只是个加速用的缓存）。"""
    data = _read_cache()
    data[client] = {'exe': str(exe), 'source': source,
                    'found_at': time.strftime('%Y-%m-%d %H:%M:%S')}
    try:
        path = _paths_cache_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding='utf-8')
    except OSError:
        pass


def _env_override(client: str) -> Path | None:
    override = os.environ.get(EXE_ENV[client], '').strip()
    if not override:
        return None
    cand = Path(override)
    if cand.is_file():
        return cand
    found = shutil.which(override)       # 允许只写个命令名
    return Path(found) if found else None


def _from_process(client: str) -> Path | None:
    """从正在运行的进程读镜像路径（Windows 走 Win32 API，POSIX 用 PATH 兜底）。"""
    names = PROCESS_NAMES[client]
    if sys.platform == 'win32':
        for pid in _windows_pids(names):
            raw = _windows_image_path(pid)
            if raw and Path(raw).is_file():
                return Path(raw)
        return None
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def _from_cache(client: str) -> Path | None:
    """上次检测到的路径。**会校验文件还在**：客户端卸载/移动过就不能再认。"""
    entry = _read_cache().get(client)
    raw = str((entry or {}).get('exe') or '').strip()
    return Path(raw) if raw and Path(raw).is_file() else None


def _known_dirs(client: str) -> list[Path]:
    """常见安装位（零成本，纯路径拼接 + 一次 is_file）。

    目录名也试几种写法：`cc-switch`（官方）之外还有 `ccswitch` / `cc swich`
    这类用户自己起的名字——绿色版解压出来叫什么名字都有可能。
    """
    name = PROCESS_NAMES[client][0]
    stems = {
        'ccswitch': ('cc-switch', 'ccswitch', 'cc_switch', 'cc swich', 'CC-Switch'),
        'zcode': ('zcode', 'ZCode', 'z-code', 'z_code'),
    }[client]
    out: list[Path] = []
    if sys.platform == 'win32':
        bases = [os.environ.get(k, '').strip() for k in
                 ('LOCALAPPDATA', 'APPDATA', 'PROGRAMFILES',
                  'PROGRAMFILES(X86)', 'ProgramW6432')]
        bases = [Path(b) for b in bases if b]
        for base in bases:
            out.append(base / name)
            out.append(base / 'Programs' / name)
            for stem in stems:
                out.append(base / stem / name)
                out.append(base / 'Programs' / stem / name)
    else:
        for stem in stems:
            out.append(Path('/Applications') / f'{stem}.app' / 'Contents' /
                       'MacOS' / stem)
            out.append(Path('/opt') / stem / name)
        out.append(Path.home() / '.local' / 'bin' / stems[0])
    return out


def _reg_values(key, field: str) -> str:
    try:
        value, _ = _winreg().QueryValueEx(key, field)
    except OSError:
        return ''
    return str(value or '')


def _winreg():
    import winreg
    return winreg


def _from_registry(client: str) -> Path | None:
    """安装版会把真实路径写进注册表：App Paths 或卸载信息里。"""
    if sys.platform != 'win32':
        return None
    import winreg

    name = PROCESS_NAMES[client][0]
    want = name.lower().replace('.exe', '')
    # App Paths：有些安装器会在这里登记 exe 的完整路径
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        key = (r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths' + '\\'
               + name)
        try:
            with winreg.OpenKey(hive, key) as k:
                raw = _reg_values(k, '')
        except OSError:
            continue
        cand = Path(str(raw).strip('"'))
        if cand.is_file():
            return cand

    # 卸载信息：DisplayIcon / InstallLocation / UninstallString 里都可能有路径
    subs = [r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
            r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall']
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for sub in subs:
            try:
                root = winreg.OpenKey(hive, sub)
            except OSError:
                continue
            with root:
                try:
                    count = winreg.QueryInfoKey(root)[0]
                except OSError:
                    continue
                for i in range(count):
                    try:
                        with winreg.OpenKey(root, winreg.EnumKey(root, i)) as k:
                            display = _reg_values(k, 'DisplayName').lower()
                            if want not in display and 'cc-switch' not in display:
                                continue
                            for field in ('InstallLocation', 'DisplayIcon',
                                          'UninstallString'):
                                cand = _exe_from_registry_value(
                                    _reg_values(k, field), name)
                                if cand:
                                    return cand
                    except OSError:
                        continue
    return None


def _exe_from_registry_value(raw: str, exe_name: str) -> Path | None:
    """从 `"E:\\dir\\cc-switch.exe",0` 这类字符串里抠出一个真实存在的 exe。

    必须挡掉卸载器：安装版的 `DisplayIcon` 常常指向 `Uninstall <App>.exe`，
    实测 ZCode 就是这样——把卸载器当成客户端"拉起来"，用户看到的是卸载向导。
    """
    text = str(raw or '').strip()
    if not text:
        return None
    if text.startswith('"'):
        text = text[1:].split('"', 1)[0]
    else:
        text = text.split(' /', 1)[0].strip()
    cand = Path(text)
    if cand.suffix.lower() == '.exe':
        if any(tok in cand.stem.lower() for tok in _EXE_REJECT):
            return None
        return cand if cand.is_file() else None
    if cand.is_dir():
        inner = cand / exe_name
        if inner.is_file():
            return inner
    return None


def _fixed_drives() -> list[Path]:
    """只取固定盘（GetDriveType=3）。

    为什么要判类型：`Path('X:\\\\').is_dir()` 碰到断开的网络映射盘会卡好几秒，
    "自动检测"不该因为别人挂了个掉线的网盘就转圈。
    """
    DRIVE_FIXED = 3
    try:
        mask = _kernel().GetLogicalDrives()
    except OSError:
        return []
    out = []
    for i, letter in enumerate(string.ascii_uppercase):
        if not (mask >> i) & 1:
            continue
        root = f'{letter}:\\'
        try:
            if _kernel().GetDriveTypeW(root) == DRIVE_FIXED:
                out.append(Path(root))
        except OSError:
            continue
    return out


def _scan(client: str) -> Path | None:
    """受限的目录扫描（只在其它来源都落空时用，且是用户显式动作）。

    敢扫盘的底气：要找的**文件名是确定的**（`cc-switch.exe`），不是靠猜目录名；
    而且有目录数与时间双预算，超了立刻放弃。
    """
    if sys.platform != 'win32':
        return None
    names = PROCESS_NAMES[client]
    wanted = {n.lower() for n in names}

    override = os.environ.get(SCAN_ROOTS_ENV, '').strip()
    if override:
        queue = collections.deque(
            (Path(p), _SCAN_DEPTH_USER)
            for p in override.split(os.pathsep) if p.strip())
    else:
        queue = collections.deque()
        for key in ('LOCALAPPDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)',
                    'ProgramW6432', 'APPDATA'):
            base = os.environ.get(key, '').strip()
            if base and Path(base).is_dir():
                queue.append((Path(base), _SCAN_DEPTH_USER))
        # 绿色版常解压在某个盘的根下（`E:\\cc swich\\`），所以盘符也要看，
        # 但只挖一层。
        for drive in _fixed_drives():
            queue.append((drive, _SCAN_DEPTH_DRIVE))

    deadline = time.monotonic() + _SCAN_BUDGET_SECONDS
    visited = 0
    while queue:
        if visited >= _SCAN_BUDGET_DIRS or time.monotonic() > deadline:
            return None
        directory, depth = queue.popleft()      # BFS：浅层先扫完，命中更早
        visited += 1
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            low = entry.name.lower()
                            if (depth > 0 and low not in _SCAN_SKIP_DIRS
                                    and not low.startswith('$')):
                                queue.append((Path(entry.path), depth - 1))
                        elif entry.name.lower() in wanted:
                            return Path(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue
    return None


def _locate(client: str, *, cheap: bool = False) -> tuple[Path | None, str]:
    """定位链，返回 (路径, 来源)。来源会显示给用户（"从哪认出来的"）。

    `cheap=True` 跳过"枚举进程"和"翻卸载注册表"这两步重的。为什么值得分档：
    界面每次打开密钥弹窗都要探测一次状态，而那只是为了显示一行字，不值得为它
    枚举本机进程再翻一遍注册表；真要动手关客户端时（`client_closed`）才走全链。
    """
    if client not in PROCESS_NAMES:
        raise ValueError(f'未知客户端 {client!r}')
    order = [('env', _env_override)]
    if not cheap:
        order.append(('process', _from_process))
    # 协议注册（谁响应 ccswitch:// / zcode://）不依赖进程、也不贵，
    # 而且客户端没开时它是唯一的权威来源，所以放在便宜档里。
    order.append(('protocol', _from_protocol))
    order.append(('cache', _from_cache))
    if not cheap:
        order.append(('registry', _from_registry))
    for source, finder in order:
        found = finder(client)
        if found:
            return found, source
    for cand in _known_dirs(client):
        if cand.is_file():
            return cand, 'known-dir'
    return None, ''


def find_executable(client: str) -> Path | None:
    """客户端的可执行文件路径（**只读、不扫盘、不写缓存**）；找不到返回 None。

    只做便宜的那几步。真需要"满盘找一遍"时走 `detect(deep=True)`
    （用户点「自动检测」）。
    """
    return _locate(client)[0]


def detect(client: str, *, deep: bool = True) -> dict:
    """自动检测客户端位置，命中就记下来（面板的「自动检测」按钮调它）。

    `deep=True` 才允许扫盘。检测结果会缓存到面板的数据目录，之后
    `find_executable` 就能直接命中——所以这个动作是"点一次，长期有效"。
    """
    path, source = _locate(client)
    if path is None and deep:
        path = _scan(client)
        source = 'scan' if path else ''
    if path is not None:
        _remember(client, path, source)
    return {'client': client, 'found': path is not None,
            'exe': str(path) if path else None, 'source': source}


def _terminate_windows(names: tuple[str, ...], *, force: bool) -> None:
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    for name in names:
        # 不带 /F 时会向 GUI 应用的窗口发关闭请求（=点右上角的 ×），
        # 客户端能走正常的退出流程；/F 才是直接杀进程。
        args = ['taskkill', *(['/F'] if force else []), '/IM', name]
        try:
            subprocess.run(args, capture_output=True, timeout=_PROC_TIMEOUT,
                           creationflags=flags)
        except (OSError, subprocess.SubprocessError):
            continue


def _terminate_posix(names: tuple[str, ...], *, force: bool) -> None:
    sig = '-KILL' if force else '-TERM'
    for name in names:
        for args in (['pkill', sig, '-x', name], ['pkill', sig, '-f', name]):
            try:
                subprocess.run(args, capture_output=True, timeout=_PROC_TIMEOUT)
            except (OSError, subprocess.SubprocessError):
                continue


def _wait_exit(client: str, timeout: float) -> bool:
    """等客户端退出。`is_running` 返回 None（探测不了）不算退出——别自欺欺人。"""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if is_running(client) is False:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL)


def stop(client: str) -> dict:
    """关闭正在运行的客户端。

    返回 `{'stopped', 'forced'}`；被探测结果不确定、或关不掉时抛 `ImportRefused`。
    调用方**必须先确认 `find_executable` 有结果**再进来，否则可能关掉却拉不起来。
    """
    names = PROCESS_NAMES.get(client)
    if not names:
        raise ValueError(f'未知客户端 {client!r}')

    running = is_running(client)
    if running is None:
        raise ImportRefused(
            'cannot_detect',
            f'无法确认 {client} 是否在运行，不敢贸然关闭它。'
            f'请手动确认后重试，或改用「导出配置」手动导入。')
    if not running:
        return {'stopped': False, 'forced': False}

    terminate = (_terminate_windows if sys.platform == 'win32'
                 else _terminate_posix)
    terminate(names, force=False)
    if _wait_exit(client, _STOP_GRACE):
        return {'stopped': True, 'forced': False}

    # 到这儿说明它没理会关闭请求（常见于"关掉窗口只是最小化到托盘"的应用）。
    forced = True
    terminate(names, force=True)
    if not _wait_exit(client, _KILL_GRACE):
        raise ImportRefused(
            'cannot_stop',
            f'{client} 正在运行且无法关闭（可能以管理员权限启动，面板没有权限）。'
            f'请手动退出它再点导入——面板不会在它运行时写入，'
            f'否则写进去的配置会被它随后保存时整份覆盖。')
    return {'stopped': True, 'forced': forced}


def launch(exe: Path) -> bool:
    """把客户端拉起来（**不等待**它初始化完成）。起不来返回 False。"""
    flags = 0
    if sys.platform == 'win32':
        # DETACHED_PROCESS：面板退出时不会把客户端一起带走（它不依赖面板活着）；
        # CREATE_NEW_PROCESS_GROUP：它自己的 Ctrl-C 也不会打到面板身上。
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen([str(exe)], cwd=str(Path(exe).parent),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags,
                         close_fds=True)
    except OSError:
        return False
    return True


def reopen(client: str, exe: Path) -> bool:
    """拉起并确认它真的起来了。

    为什么要确认：单实例互斥、缺依赖、被杀过之后锁没释放…… 都可能让进程起来
    又立刻退出。不确认的话，我们会告诉用户"已重新打开"，而他的桌面上什么都没有。
    """
    if not launch(exe):
        return False
    deadline = time.monotonic() + _BOOT_GRACE
    while time.monotonic() < deadline:
        if is_running(client) is True:
            return True
        time.sleep(_POLL)
    return False


class ClientLifecycle:
    """一次导入里客户端被动了什么（要如实告诉用户，不能悄悄关了人家的工具）。"""

    def __init__(self, client: str):
        self.client = client
        self.exe: str = ''
        self.stopped = False
        self.forced = False
        self.restarted: bool | None = None   # None = 没关过，谈不上重开

    def as_dict(self) -> dict:
        return {'stopped': self.stopped, 'forced': self.forced,
                'restarted': self.restarted, 'exe': self.exe}


@contextlib.contextmanager
def client_closed(client: str, *, close_running: bool = False):
    """保证"写入的那一刻客户端是退出的"，并在写完后把它放回原样。

    · 没在运行 → 什么都不做；
    · 在运行且 `close_running` → 关掉它，**无论中间成功还是抛错都拉起来**；
    · 在运行但没授权 → 抛 `ImportRefused('client_running')`，老行为不变。

    `close_running` 必须由调用方显式给出（见 routers/keys.py 的 ImportIn）。
    """
    lc = ClientLifecycle(client)
    if is_running(client):
        if not close_running:
            raise ImportRefused(
                'client_running',
                f'{client} 正在运行：它会在保存时整体回写自己的配置，'
                f'把我们的写入覆盖掉。请先完全退出 {client} 再导入。')
        exe, source = _locate(client)
        if exe is None:
            raise ImportRefused(
                'cannot_stop',
                f'{client} 正在运行，但面板没找到它的可执行文件，'
                f'写完没法自动帮你重新打开，所以没有替你关闭它。'
                f'请手动退出 {client} 后再导入；或先点「自动检测」定位它，'
                f'也可以在面板的 .env 里用 {EXE_ENV[client]} 指明完整路径。')
        lc.exe = str(exe)
        _remember(client, exe, source or 'process')   # 顺手把位置记下来
        res = stop(client)
        lc.stopped, lc.forced = res['stopped'], res['forced']
    try:
        yield lc
    finally:
        if lc.stopped and lc.exe:
            lc.restarted = reopen(client, Path(lc.exe))


# ── 状态 ────────────────────────────────────────────────────────────────

def status(client: str) -> dict:
    """该客户端的可导入状态（**只读、无副作用**，供界面提前提示）。

    多给四个字段是给界面用的：`deeplink` 说明这台机器能不能走客户端官方的
    `ccswitch://` 深链（能走就**不用关客户端、也不用逐个猜它的表结构**，
    见本模块开头的「优先走深链」）；`exe` 让用户看到"面板认的是哪个可执行文件"
    （检测没结果时为 null，界面据此提示可点「自动检测」），`exe_source` 说明
    它是从哪认出来的；`needs_close` 让界面能把「点下去会先替你关掉客户端」
    **提前说清**，而不是点完才发现自己的客户端被关了。
    """
    path = target_path(client)
    installed = path.is_file()
    running = is_running(client)
    # 没在运行时用便宜档：只需要知道"面板认得哪个可执行文件"，不必枚举进程。
    exe, exe_source = _locate(client, cheap=not running)
    if not installed:
        ok, reason = False, 'not_installed'
    elif running is None:
        ok, reason = False, 'cannot_detect'
    elif running:
        ok, reason = False, 'client_running'
    else:
        ok, reason = True, ''
    return {'client': client, 'installed': installed, 'running': running,
            'importable': ok, 'reason': reason, 'target': str(path),
            'exe': str(exe) if exe else None,
            'exe_source': exe_source,
            'needs_close': bool(running is True and exe is not None),
            'deeplink': deeplink_supported(client)}


def status_all() -> list[dict]:
    return [status(c) for c in CLIENTS]


def _ensure_writable(client: str) -> Path:
    """把 status() 的结论转成"能写就返回路径，否则抛 ImportRefused"。"""
    st = status(client)
    if not st['installed']:
        raise ImportRefused(
            'not_installed',
            f'本机未检测到 {client} 的配置（{st["target"]}）。'
            f'请先安装并至少启动一次该客户端，或改用「导出配置」手动导入。')
    if st['reason'] == 'client_running':
        raise ImportRefused(
            'client_running',
            f'{client} 正在运行：它会在保存时整体回写自己的配置，'
            f'把我们的写入覆盖掉。请先完全退出 {client} 再导入。')
    if st['reason'] == 'cannot_detect':
        raise ImportRefused(
            'cannot_detect',
            f'无法确认 {client} 是否在运行，为避免改坏配置已中止。'
            f'请手动确认它已退出，或改用「导出配置」手动导入。')
    return Path(st['target'])


# ── 备份与原子写入 ──────────────────────────────────────────────────────

def _backup(path: Path) -> Path:
    """写前备份到同目录（同盘才能快速回滚）。返回备份路径。"""
    stamp = time.strftime('%Y%m%d-%H%M%S')
    dest = path.with_name(f'{path.name}{_BACKUP_SUFFIX}-{stamp}.bak')
    n = 1
    while dest.exists():        # 同一秒内连点两次导入也不会覆盖上一份备份
        n += 1
        dest = path.with_name(f'{path.name}{_BACKUP_SUFFIX}-{stamp}-{n}.bak')
    shutil.copy2(path, dest)
    return dest


def _restore(backup: Path, path: Path) -> None:
    try:
        shutil.copy2(backup, path)
    except OSError:
        pass        # 回滚失败也要保留原始异常，别把真正的原因盖掉


def _atomic_write_text(path: Path, text: str) -> None:
    """先写临时文件再替换：中途崩溃也不会留下半截 JSON。"""
    tmp = path.with_name(path.name + f'{_BACKUP_SUFFIX}.tmp')
    tmp.write_text(text, encoding='utf-8')
    os.replace(tmp, path)


# 客户端的自定义协议名（注册表 `HKCU\Software\Classes\<协议>\shell\open\command`
# 指向它们各自的 exe）。实测本机：ccswitch → cc-switch.exe，zcode → ZCode.exe。
PROTOCOL_NAMES = {'ccswitch': 'ccswitch', 'zcode': 'zcode'}

DEEPLINK_VERSION = 'v1'


def protocol_handler(client: str) -> Path | None:
    """该客户端自定义协议的处理器 exe（注册表里那个"谁响应这个链接"）。

    这是"安装路径因机器而异"的通用答案：路径由客户端自己注册到系统，
    与它在不在运行**无关**——比枚举进程更稳（客户端没开时进程法是瞎的）。
    """
    proto = PROTOCOL_NAMES.get(client)
    if not proto or sys.platform != 'win32':
        return None
    import winreg

    exe_name = PROCESS_NAMES[client][0]
    sub = rf'Software\Classes\{proto}\shell\open\command'
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_CLASSES_ROOT):
        try:
            with winreg.OpenKey(hive, sub) as k:
                cmd = _reg_values(k, '')
        except OSError:
            continue
        found = _exe_from_registry_value(cmd, exe_name)
        if found:
            return found
    return None


def _from_protocol(client: str) -> Path | None:
    return protocol_handler(client)


def deeplink_supported(client: str) -> bool:
    """这版客户端能不能走**官方深链**导入。

    只有 cc-switch 公开了协议格式（`ccswitch://v1/import?…`，见官方文档 §5.3），
    所以只认它。ZCode 也注册了 `zcode://`，但格式没公开——**不猜**：猜错会把
    一条带明文密钥的链接交给一个不知道会怎么处理它的程序。
    """
    return client == 'ccswitch' and protocol_handler(client) is not None


def build_ccswitch_deeplink(*, app: str, name: str, settings_config: dict,
                            token: str = '', base_url: str = '',
                            model: str = '', set_current: bool = False) -> str:
    """拼一条 `ccswitch://v1/import` 深链。

    **这条字符串里含明文密钥**：只在内存里拼、交给操作系统的协议处理器，
    不落盘、不进日志、不进审计。

    为什么同时给 `config`（完整配置）和 endpoint/apiKey/model（散字段）：
    `config` 是 v3.8+ 才有的，而散字段从 v1 起就有——两者并存时 cc-switch
    以 URL 参数为准，所以老版本也不会因为它读不懂 `config` 就整个导入失败。
    """
    if app not in ('claude', 'codex'):
        raise ValueError(f'app 必须是 claude / codex 之一，收到 {app!r}')
    inner = settings_config or {}
    if app == 'claude':
        # claude 的 settings_config 就是 settings.json 的内容
        payload = json.dumps(inner, ensure_ascii=False).encode('utf-8')
        fmt = 'json'
    else:
        # codex 的 config.toml 文本藏在 settings_config['config'] 里
        payload = str(inner.get('config') or '').encode('utf-8')
        fmt = 'toml'

    params = {'resource': 'provider', 'app': app, 'name': name,
              'config': base64.b64encode(payload).decode('ascii'),
              'configFormat': fmt}
    if base_url:
        params['endpoint'] = base_url
    if token:
        params['apiKey'] = token
    if model:
        params['model'] = model
    if set_current:
        # 只有要激活时才给这个参数：cc-switch 对 "enabled=false" 的处理
        # 没在文档里写死，能不传就不传。
        params['enabled'] = 'true'
    return (f'{PROTOCOL_NAMES["ccswitch"]}://{DEEPLINK_VERSION}/import?'
            + urllib.parse.urlencode(params))


def open_url(url: str) -> bool:
    """把 URL 交给操作系统的协议处理器（Windows = ShellExecute）。"""
    try:
        if sys.platform == 'win32':
            os.startfile(url)                    # noqa: S606 - 自定义协议
        else:
            opener = 'open' if sys.platform == 'darwin' else 'xdg-open'
            subprocess.Popen([opener, url], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return True


def import_ccswitch_deeplink(*, app: str, name: str, settings_config: dict,
                             token: str = '', base_url: str = '',
                             model: str = '',
                             set_current: bool = False) -> dict:
    """走 cc-switch 官方深链导入。

    客户端会弹一个**自己的确认框**（密钥只显示前 4 位），用户点「导入」才落盘。
    这是刻意的：面板把配置交给客户端，由它校验、入库、并让用户确认，
    比我们绕开它直接改库安全得多（见提案 §10）。
    """
    handler = protocol_handler('ccswitch')
    if handler is None:
        raise ImportRefused(
            'deeplink_unavailable',
            '本机没有注册 ccswitch:// 协议（可能是 cc-switch 版本较旧），'
            '无法用官方深链导入。请改用「直接写入本机配置」。')
    url = build_ccswitch_deeplink(
        app=app, name=name, settings_config=settings_config, token=token,
        base_url=base_url, model=model, set_current=set_current)
    if not open_url(url):
        raise ImportRefused(
            'cannot_open',
            '已生成导入链接，但操作系统没能把它交给 cc-switch（可能是协议注册失效）。'
            '请改用「导出配置」手动导入。')
    return {'action': 'handed-off', 'provider_id': '', 'app_type': app,
            'name': name, 'target': '', 'backup': '',
            'handler': str(handler)}


# ── cc-switch（SQLite）──────────────────────────────────────────────────

def ccswitch_provider_id(prefix: str, app: str) -> str:
    """由密钥前缀 + 应用推导**稳定**的 provider id。

    用 uuid5（确定性）而不是 uuid4：同一把密钥重复导入必须落到同一行
    （upsert），否则列表里会堆出一串同名供应商。前缀本身是公开信息
    （列表页就显示它），不含密钥内容。
    """
    seed = f'workbuddy:{str(prefix or "manual").strip()}:{str(app or "").strip()}'
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def import_ccswitch(*, settings_config: dict, app: str, name: str,
                    provider_id: str, endpoint_url: str = '',
                    set_current: bool = False) -> dict:
    """把一条 provider 写进 cc-switch（新增或更新，同 (id, app_type) 为同一行）。

    `settings_config` 必须是 `keyexport.to_ccswitch` 的产物——和「导出」共用
    同一份 payload，避免两条路各写一份格式而后慢慢漂移。
    """
    from . import keyexport

    if app not in keyexport.CCSWITCH_APPS:
        raise ValueError(f'app 必须是 {keyexport.CCSWITCH_APPS} 之一，收到 {app!r}')
    if not str(provider_id or '').strip():
        raise ValueError('provider_id 不能为空')

    path = _ensure_writable('ccswitch')
    payload = json.dumps(settings_config, ensure_ascii=False)
    now_ms = int(time.time() * 1000)
    # apiFormat 取自真实行：claude 用 anthropic，codex 用 openai_responses。
    # 两个未知开关（endpointAutoSelect / commonConfigEnabled）**刻意不写**，
    # 见本模块开头「为什么不猜 endpointAutoSelect」。
    meta = json.dumps({'apiFormat': 'anthropic' if app == 'claude'
                       else 'openai_responses'}, ensure_ascii=False)

    backup = _backup(path)
    try:
        con = sqlite3.connect(str(path), timeout=10)
        try:
            cur = con.cursor()
            exists = cur.execute(
                'SELECT 1 FROM providers WHERE id = ? AND app_type = ?',
                (provider_id, app)).fetchone()
            if exists:
                cur.execute(
                    'UPDATE providers SET name = ?, settings_config = ?, '
                    'meta = ? WHERE id = ? AND app_type = ?',
                    (name, payload, meta, provider_id, app))
                action = 'updated'
            else:
                cur.execute(
                    'INSERT INTO providers (id, app_type, name, settings_config, '
                    'category, created_at, sort_index, is_current, meta) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    # sort_index 留 NULL：cc-switch 自己新增的供应商就是 NULL，
                    # 不替它编造排序值。
                    (provider_id, app, name, payload, 'custom', now_ms, None,
                     1 if set_current else 0, meta))
                action = 'created'

            # is_current 在同 app_type 内是单选：置位前先清掉其它行，
            # 否则会出现两个"当前"（cc-switch 按第一条取，用户看到的却是另一条）。
            if set_current:
                cur.execute('UPDATE providers SET is_current = 0 WHERE app_type = ?',
                            (app,))
                cur.execute('UPDATE providers SET is_current = 1 '
                            'WHERE id = ? AND app_type = ?', (provider_id, app))

            if endpoint_url.strip():
                cur.execute('DELETE FROM provider_endpoints '
                            'WHERE provider_id = ? AND app_type = ?',
                            (provider_id, app))
                cur.execute(
                    'INSERT INTO provider_endpoints (provider_id, app_type, url, '
                    'added_at) VALUES (?, ?, ?, ?)',
                    (provider_id, app, endpoint_url.strip().rstrip('/'), now_ms))
            con.commit()
        finally:
            con.close()
    except Exception:
        _restore(backup, path)
        raise

    return {'action': action, 'provider_id': provider_id, 'app_type': app,
            'name': name, 'target': str(path), 'backup': str(backup)}


# ── ZCode（JSON）───────────────────────────────────────────────────────

def import_zcode(*, fragment: dict, set_current: bool = False) -> dict:
    """把 `keyexport.to_zcode` 的片段并入 provider_config.json（按 providerId upsert）。

    只动我们自己的那一条：其它供应商、`providerOrder` 里已有的顺序、
    `schemaVersion`、以及所有未知字段都原样保留。用户自己的配置不归我们整理。
    """
    rule = (fragment or {}).get('providerRule') or {}
    provider_id = str(rule.get('providerId') or '').strip()
    if not provider_id:
        raise ValueError('片段缺少 providerId')
    new_models = list((rule.get('config') or {}).get('personalModelIds') or [])

    path = _ensure_writable('zcode')
    try:
        doc = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ImportRefused(
            'bad_format',
            f'无法解析 {path}（{exc}）。为避免覆盖成空配置已中止，'
            f'请先让 ZCode 正常启动一次生成合法配置。') from None
    if not isinstance(doc, dict):
        raise ImportRefused('bad_format', f'{path} 顶层不是对象，已中止。')

    cfg = doc.setdefault('config', {})
    if not isinstance(cfg, dict):
        raise ImportRefused('bad_format', f'{path} 的 config 不是对象，已中止。')

    # ── providerRules：按 providerId 替换或追加
    pcr = cfg.setdefault('providerConfigRules', {})
    rules = pcr.setdefault('providerRules', [])
    if not isinstance(rules, list):
        raise ImportRefused('bad_format', 'providerRules 不是数组，已中止。')
    idx = next((i for i, r in enumerate(rules)
                if isinstance(r, dict) and r.get('providerId') == provider_id), None)
    if idx is None:
        rules.append(rule)
        action = 'created'
    else:
        rules[idx] = rule
        action = 'updated'

    # ── providerOrder：缺了 ZCode 可能不显示该供应商；已在其中就不动顺序
    order = cfg.setdefault('providerOrder', [])
    if not isinstance(order, list):
        raise ImportRefused('bad_format', 'providerOrder 不是数组，已中止。')
    if provider_id not in order:
        if set_current:
            order.insert(0, provider_id)
        else:
            order.append(provider_id)

    # ── modelConfigRules：我方规则**整体替换**为本次清单
    #
    # 为什么是"先全删再写入"而不是"逐条 upsert"：后者对已存在的 modelId 会
    # 「保留旧的 + 又追加新的」，同一 (providerId, modelId) 在文件里出现两条
    # 完全相同的规则。真实数据里踩到过：44 个模型的清单里有 1 个与上次重叠，
    # 于是缩量到 3 个之后总数是 65（应为 64）。
    # 整体替换同时天然处理了"模型变少后旧规则残留"的问题。
    mcr = cfg.setdefault('modelConfigRules', {})
    pmr = mcr.setdefault('providerModelRules', [])
    if not isinstance(pmr, list):
        raise ImportRefused('bad_format', 'providerModelRules 不是数组，已中止。')
    incoming, seen_ids = [], set()
    for r in (fragment.get('providerModelRules') or []):
        mid = (r or {}).get('modelId')
        if mid in seen_ids:      # 同一 modelId 只留一条，别把重复传进来
            continue
        seen_ids.add(mid)
        incoming.append(r)
    mcr['providerModelRules'] = [
        r for r in pmr
        if not (isinstance(r, dict) and r.get('providerId') == provider_id)
    ] + incoming

    backup = _backup(path)
    try:
        _atomic_write_text(path, json.dumps(doc, ensure_ascii=False, indent=2) + '\n')
    except Exception:
        _restore(backup, path)
        raise

    return {'action': action, 'provider_id': provider_id,
            'provider_name': rule.get('providerName') or '',
            'models': len(new_models), 'target': str(path), 'backup': str(backup)}
