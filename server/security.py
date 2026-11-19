"""管理端鉴权：PBKDF2 密码、HMAC 签名 Cookie 会话、登录防爆破。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from fastapi import Depends, HTTPException, Request

from . import config, tokensvc
from .iputil import client_ip
from .iputil import _is_trusted_proxy  # noqa: SLF001 —— 与 client_ip 同一份可信代理判据

_fail: dict[str, list] = {}
MAX_FAILS = 5
LOCK_SECONDS = 600


# ── 密码哈希 ─────────────────────────────────────────────
def make_hash(pwd: str, iterations: int = 260000) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', pwd.encode(), bytes.fromhex(salt), iterations)
    return f'pbkdf2_sha256${iterations}${salt}${digest.hex()}'


def verify_pwd(pwd: str, stored: str) -> bool:
    try:
        algo, iters, salt, expect = stored.split('$')
        if algo != 'pbkdf2_sha256':
            return False
        digest = hashlib.pbkdf2_hmac('sha256', pwd.encode(), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(digest.hex(), expect)
    except Exception:
        return False


# ── 管理端密码策略 ─────────────────────────────────────
#
# 生产事故复盘：公网面板的 admin 密码被设成了 `admin123`，攻击者直接登录，
# 导出了全部账号凭据。登录失败锁定拦不住「已知弱口令」——必须在设置密码时
# 就拒绝，而不是等到被撞开以后。这里只做最小、确定的规则：
#   · 至少 12 位；
#   · 常见弱口令（含 admin/password/123456 等变体）直接拒绝；
#   · 不允许「用户名 + 纯数字」；
#   · 不允许纯数字或单一重复字符。
# 不做复杂度打分/字典工程：规则越复杂越容易误伤，真正兜底的是长度与黑名单。
MIN_PASSWORD_LENGTH = 12

_WEAK_PASSWORDS = frozenset({
    'admin', 'admin123', 'admin1234', 'admin12345', 'admin123456',
    'admin1234567', 'admin12345678', 'admin123456789',
    'password', 'password123', 'password1234', 'password12345',
    '123456789012', '1234567890123', '12345678901234',
    'qwertyuiop', 'qwerty123456', 'qwertyuiop123',
    'workbuddy', 'workbuddy123', 'workbuddy1234', 'workbuddy2024',
    'letmein12345', 'iloveyou1234', '111111111111', '000000000000',
})


def password_problem(pwd: str, username: str = '') -> str | None:
    """返回密码不合规的原因；合规返回 None。

    给 bootstrap（WB_ADMIN_PASSWORD）、新建用户、重置密码共用同一套判据，
    避免「启动时能设弱口令、界面改密码时却被拦」这种不一致。
    """
    value = str(pwd or '')
    if len(value) < MIN_PASSWORD_LENGTH:
        return f'密码至少 {MIN_PASSWORD_LENGTH} 位'
    low = value.lower()
    if low in _WEAK_PASSWORDS:
        return '密码过于常见，请换一个'
    if username:
        user = str(username).strip().lower()
        if user and low.startswith(user) and low[len(user):].isdigit():
            return '密码不能是用户名加数字'
    if value.isdigit():
        return '密码不能全是数字'
    if len(set(value)) == 1:
        return '密码不能是重复字符'
    return None


def weak_admin_passwords() -> list[str]:
    """启动时检测**存量**管理员是否仍在用已知弱口令。

    新密码在设置入口就被拦了，但升级上来的 users.json 里可能已经有
    `admin123` 这类值（本次生产事故就是如此）。这里只拿小的静态黑名单去比对
    哈希，命中就返回用户名，让启动日志能明确点出来；不直接拒绝启动——
    否则用户会被锁在面板外，连改密码的入口都没有。
    """
    try:
        cfg = load_users()
    except Exception:  # noqa: BLE001
        return []
    found: list[str] = []
    for item in cfg.get('users', []):
        if item.get('role') != 'admin':
            continue
        stored = str(item.get('pwd_hash') or '')
        for candidate in _WEAK_PASSWORDS:
            if verify_pwd(candidate, stored):
                found.append(str(item.get('username') or ''))
                break
    return found

_DUMMY_HASH: str | None = None


def dummy_verify(pwd: str) -> None:
    """对一份固定的哑哈希完整跑一遍 PBKDF2，丢弃结果。

    登录时用户不存在会短路跳过 PBKDF2（260000 轮，毫秒级时间差），响应快慢
    因此暴露"用户名是否存在"——管理端默认必有 admin，价值有限，但公网服务
    不该留这个侧信道。用户不存在的分支先跑一次本函数，把耗时对齐。
    """
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = make_hash(secrets.token_urlsafe(24))
    verify_pwd(pwd, _DUMMY_HASH)


# ── 用户存储 ─────────────────────────────────────────────
def load_users() -> dict:
    """读取 users.json。

    **绝不因解析失败而自动重建**：原实现在文件损坏（半写、磁盘满、进程被杀、
    手工改坏）时直接 `bootstrap_users()`，那会**生成新 secret + 新随机密码的
    admin 并原地覆盖写回**——结果是全部账号（含其他管理员）被静默清空、
    所有人的会话失效，而新密码只打印在 stderr 里。一次瞬时故障就足以让
    管理面「重启」，且现场被覆盖、无法复盘。实测复现过。

    现在：文件不存在 → 首次启动，正常 bootstrap；
          文件存在但读不了/解析不了 → 抛错，让请求失败并保留现场。
    """
    config.ensure_dirs()
    if not config.USERS_FILE.exists():
        return bootstrap_users()
    try:
        data = json.loads(config.USERS_FILE.read_text(encoding='utf-8'))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f'用户文件损坏，拒绝自动重建以保护现场：{config.USERS_FILE}（{exc}）。'
            '请先备份该文件，再用备份恢复；若确需重置，手动删除该文件后重启。'
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get('users'), list):
        raise RuntimeError(f'用户文件结构异常：{config.USERS_FILE}')
    if not data.get('secret'):
        raise RuntimeError(f'用户文件缺少 secret：{config.USERS_FILE}')
    return data


def _restrict_permissions(path) -> None:
    """把敏感文件权限收紧到仅属主可读写（0600）。

    users.json 里存着**签发会话 Cookie 的 secret**——拿到它就能伪造任意角色
    （含 admin）的登录态。因此它属于最高敏感级，不应让同主机的其他用户读到。
    Windows 上 os.chmod 只影响只读位，无实际意义，故静默忽略失败。
    """
    import os
    import stat
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def save_users(data: dict) -> None:
    """原子写入 users.json。

    为什么必须原子：原实现直接 `O_TRUNC` 覆盖，**写窗口内被杀进程/断电/磁盘满
    就会留下半截文件**，进而触发（修复前的）自动重建 → 全部账号被清空。
    现在改为「写临时文件 → fsync → os.replace」：替换是原子的，读者要么看到
    旧内容、要么看到新内容，永远不会看到半截。

    临时文件同样以 0600 创建，避免"先写后改权限"之间出现可被他人读取的窗口。

    **失败时绝不退化成非原子直写**：那样等于把刚堵上的损坏路径又打开一次。
    替换失败就报错，让调用方看到（旧文件此时仍完好，可安全重试）。
    """
    config.ensure_dirs()
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    target = config.USERS_FILE
    tmp = target.with_name(f'.{target.name}.tmp')

    # 支持不支持 O_CREAT 指定 mode 的平台：退化为默认权限创建后再 chmod
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    except OSError:
        fd = None

    try:
        if fd is not None:
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())  # 落盘后再替换，避免断电留下空/半截文件
        else:
            # 先写临时文件（非目标文件），仍然保证目标只在原子替换时改变
            tmp.write_text(payload, encoding='utf-8')
        _restrict_permissions(tmp)
        os.replace(tmp, target)  # 原子替换：要么旧、要么新，不会半截
        _restrict_permissions(target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        # 旧文件未被触碰，保持完好；把失败暴露出去让上层处理
        raise


def bootstrap_users() -> dict:
    """首次启动：创建 admin 用户，密码取 WB_ADMIN_PASSWORD 或随机生成。"""
    import os
    import sys

    env_pwd = os.environ.get('WB_ADMIN_PASSWORD', '').strip()
    if env_pwd:
        problem = password_problem(env_pwd, 'admin')
        if problem:
            raise RuntimeError(
                f'WB_ADMIN_PASSWORD 不符合管理端密码策略：{problem}。'
                f'请使用至少 {MIN_PASSWORD_LENGTH} 位、非常见弱口令的密码。'
            )
    # 随机密码也留足长度；token_urlsafe(12) 约 16 字符。
    pwd = env_pwd or secrets.token_urlsafe(12)
    data = {
        'secret': config.new_secret(),
        'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': make_hash(pwd)}],
        'api_keys': [],
    }
    save_users(data)
    if not env_pwd:
        _write_initial_password_file(pwd)
        print('=' * 60, file=sys.stderr)
        print('[WorkBuddy Manager] 已生成初始管理员账号', file=sys.stderr)
        print(f'  用户名: admin', file=sys.stderr)
        print(f'  密码  : {pwd}', file=sys.stderr)
        print('  请登录后立即在「设置 → 管理用户」中修改密码。', file=sys.stderr)
        print('=' * 60, file=sys.stderr)
    return data


def initial_password_path() -> Path:
    """初始密码落盘文件的位置（与 users.json 同级）。"""
    return config.USERS_FILE.with_name('initial-admin-password.txt')


def _write_initial_password_file(pwd: str) -> None:
    """把随机生成的初始密码落盘，0600。

    为什么必须留一份：随机密码**只在 stderr 打一次**（issue #179）。journal 会被
    轮转、安装脚本可能因为竞态没抓到、用户在几屏日志里也可能翻过去 —— 一旦错过，
    库里只有 pbkdf2 哈希，`users.json` 里也读不出明文，于是**没有任何办法登录**。
    落一份明文 + 登录页指路，是唯一能把这个死结解开的办法。

    权限与 users.json 同级（仅属主可读写）：那已经是能伪造任意管理员会话的
    secret 所在，再多一个同样只属主可读的明文密码不改变暴露面。

    改过密码后由 clear_initial_password_file() 抹掉。
    """
    target = initial_password_path()
    try:
        # 与 save_users 同样的降级写法：不支持 O_CREAT mode 的平台上退化为
        # 写完再 chmod，并继续收紧权限。
        try:
            fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        except OSError:
            fd = None
        if fd is not None:
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                fh.write(f'{pwd}\n')
        else:
            target.write_text(f'{pwd}\n', encoding='utf-8')
        _restrict_permissions(target)
    except OSError:
        # 写不进去也不能影响启动：日志里那条记录仍然是唯一副本。
        pass


def clear_initial_password_file() -> None:
    """密码被改掉（或显式重置）后，抹掉明文副本。

    留着旧密码文件等于给了一份永远有效的凭据 —— 那正是 issue #179 想要的
    「找回来」能力用过期的方式继续生效，安全上不能接受。
    """
    try:
        initial_password_path().unlink(missing_ok=True)
    except OSError:
        pass


# ── 会话签名 ─────────────────────────────────────────────
def _sign(payload: str, secret: str) -> str:
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f'{payload}|{sig}'.encode()).decode()


def _unsign(token: str, secret: str) -> dict | None:
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        payload, sig = raw.rsplit('|', 1)
        expect = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expect, sig):
            return None
        obj = json.loads(payload)
        if obj.get('exp', 0) < time.time():
            return None
        return obj
    except Exception:
        return None


def issue_token(username: str, role: str, *, orig: int | None = None) -> str:
    """签发会话 cookie（登录与**续期**共用）。

    载荷里带 `sv`（session version）：该用户当前的会话版本号。改密码 / 改角色 /
    「吊销会话」都会递增它，于是**已签发的 cookie 立即失效**——否则被盗会话
    即使改了密码也还能继续用。role 仍写进载荷仅作展示参考，
    鉴权一律以用户表为准（见 current_user）。

    两个时间基准，别混：
      · `iat`  本次签发/续期时刻 —— **滑动的基准**（多久没活动就失效）；
      · `orig` 首次登录时刻 —— **绝对上限的基准**，续期**不延长**它。
    分开的理由：只有 `iat` 的话，被盗会话只要定期发一个请求就能无限续命；
    只有 `orig` 的话，天天用的人也要按点重登（惩罚常用者、放过闲置者，正好反了）。
    两者叠加才是「常用不打扰、闲置会过期、且总寿命有硬上限」。
    """
    now = int(time.time())
    started = int(orig or now)
    cfg = load_users()
    sv = session_version(cfg, username)
    payload = json.dumps(
        {'username': username, 'role': role, 'sv': sv,
         'iat': now, 'orig': started,
         'exp': started + config.SESSION_DAYS * 86400}
    )
    return _sign(payload, cfg['secret'])


def idle_expired(obj: dict) -> bool:
    """会话是否因**闲置**超时而失效。

    关闭空闲判定时（`SESSION_IDLE_HOURS` = 0）**不看 `iat`**：此时会话寿命完全
    由载荷里的 `exp`（总时长）决定，缺 `iat` 不构成风险，再拒绝一次就是白踢用户
    ——而且升级前签发的旧 cookie 本来就都没有 `iat`，那种部署一升级会被立刻
    全部登出，与「我特意关掉了空闲判定」的意图相反。

    开启时空缺 `iat` 一律判失效：那时确实无法判断它「多久没活动」，不能假设刚活动过
    （fail-open）。代价是一次性重新登录，已在 CHANGELOG 说明。
    """
    if config.SESSION_IDLE_HOURS <= 0:
        return False
    iat = obj.get('iat')
    if not isinstance(iat, int) or iat <= 0:
        return True
    return (time.time() - iat) > config.SESSION_IDLE_HOURS * 3600


def needs_renewal(obj: dict) -> bool:
    """是否该给这个会话**续期**（滑动窗口）。

    只在走过 1/3 空闲窗口时才续：每次请求都重签会让 cookie 频繁变化，
    也没有额外收益（真正的约束是「多久没活动」，不是「签了几次」）。
    空闲判定关闭时（0）自然也不续期。
    """
    iat = obj.get('iat')
    if not isinstance(iat, int) or iat <= 0:
        return False
    hours = config.SESSION_IDLE_HOURS
    if hours <= 0:
        return False
    return (time.time() - iat) > (hours * 3600) / 3


def cookie_secure(request: Request) -> bool:
    """会话 cookie 是否带 `Secure` 标志。`WB_SECURE_COOKIE` = auto | true | false。

    `auto` 的判据是 `X-Forwarded-Proto`（反代终止 TLS 时由它告知原始协议）。
    该头与 X-Real-IP 一样是普通请求头，只在 **TCP 对端是可信代理**时才采信
    （与 `iputil.client_ip` 同一份判据）：直连的攻击者发
    `X-Forwarded-Proto: http` 不能让 HTTPS 部署漏发 `Secure`。

    **这里有个隐蔽的失效形态**：反代没传该头时（1Panel / nginx 默认配置就未必传），
    后端只看到明文 HTTP，于是 `auto` 静默地不加 `Secure` —— 而用户访问的确实是
    HTTPS，他以为自己受保护。这类「以为安全其实没有」比明确的报错更危险，
    所以下面加了 `insecure_cookie_warning()`，在日志里明确点出来。
    """
    setting = config.SECURE_COOKIE.lower()
    if setting in ('true', '1'):
        return True
    if setting in ('false', '0'):
        return False
    proto = request.scope.get('scheme') or request.url.scheme
    peer = request.client.host if request.client else ''
    if config.TRUST_PROXY and _is_trusted_proxy(peer):
        proto = request.headers.get('x-forwarded-proto', proto)
    return proto == 'https'


def insecure_cookie_warning(request: Request) -> str:
    """返回「cookie 没带 Secure 但很可能该带」的告警文案；否则空串。

    判定用的信号都是**反代存在**的旁证，任一成立就提示：
      · `X-Forwarded-For` / `X-Real-IP` 存在 —— 请求经过了反代；
      · `X-Forwarded-Proto` 缺失或非 https —— 正是「少了这一句」的场景。
    两个都没有（直连本机 HTTP）时不提示：那是本地调试的正常形态。
    """
    if cookie_secure(request):
        return ''
    setting = config.SECURE_COOKIE.lower()
    if setting in ('false', '0'):
        return ''          # 用户显式关掉了，尊重其选择，不再唠叨
    behind_proxy = bool(request.headers.get('x-forwarded-for')
                        or request.headers.get('x-real-ip')
                        or request.headers.get('x-forwarded-host'))
    if not behind_proxy:
        return ''
    return (
        '会话 Cookie 未带 Secure 标志，但请求经过了反向代理。'
        '若外部访问是 HTTPS，说明反代没有透传 X-Forwarded-Proto —— '
        '请在反代配置里加 `proxy_set_header X-Forwarded-Proto $scheme;`，'
        '或直接设 WB_SECURE_COOKIE=true 固定开启。'
        '（未带 Secure 时，浏览器可能在明文 HTTP 下也发送该 Cookie。）'
    )


# ── 登录防爆破 ───────────────────────────────────────────
# 按 IP 与按用户名双维度计数：
#   - 按 IP：防单机爆破（配合修正后的真实 IP 解析才有效）
#   - 按用户名：防「换 IP 打同一账号」的分布式爆破
# 注意：两个字典都设有容量上限，避免被大量不同 IP/用户名撑爆内存。
_fail: dict[str, list] = {}
_user_fail: dict[str, list] = {}
MAX_FAILS = 5
LOCK_SECONDS = 600
MAX_TRACKED = 5000


def _prune(store: dict[str, list]) -> None:
    """超限时淘汰**已经过了锁定窗口**的条目，绝不整体清空。

    为什么不能清空：这些字典承载登录失败计数。原实现超限即 `store.clear()`，
    于是攻击者只要用大量不存在的用户名（或伪造来源 IP）把字典灌满，就能
    **顺手把 admin 的锁定计数一起抹掉**，之后可以无限猜密码。
    「宁可放宽也不被撑爆」这个取舍对内存成立，但对**安全计数器**不成立。

    淘汰规则按安全性排序：
      1. 只淘汰 `first` 已超过 LOCK_SECONDS 的条目（它们本来就不再锁定谁）
      2. 仍在锁定窗口内的条目**一条都不动**——哪怕因此略微超出上限
    这样上限不再是硬保证，但超出的部分有界（窗口内最多被灌这么多条），
    而「锁定不可被抹掉」这个安全属性是硬的。上限的意义是防内存爆炸，
    LOCK_SECONDS 只有 10 分钟，窗口内的条目数受请求速率与时间共同约束。
    """
    if len(store) <= MAX_TRACKED:
        return
    now = time.time()
    expired = [k for k, v in store.items() if (now - v[1]) >= LOCK_SECONDS]
    if not expired:
        # 全都在锁定窗口内：不清空、不淘汰。宁可暂时超出上限，也不能放宽锁定。
        return
    # 优先淘汰最旧的过期条目，留一点余量避免每次请求都触发
    for k in sorted(expired, key=lambda k: store[k][1])[: len(expired)]:
        store.pop(k, None)


def login_blocked(ip: str, username: str | None = None) -> bool:
    now = time.time()
    cnt, first = _fail.get(ip, [0, 0.0])
    if cnt >= MAX_FAILS and (now - first) < LOCK_SECONDS:
        return True
    if username:
        key = username.lower()
        ucnt, ufirst = _user_fail.get(key, [0, 0.0])
        if ucnt >= MAX_FAILS and (now - ufirst) < LOCK_SECONDS:
            # 按用户名的锁定是为了防「换 IP 爆破同一账号」，但它同时给了匿名者
            # 一件武器：对 admin 提交 5 次错误密码就能把**从未输错**的管理员
            # 也锁在门外（远程锁定 DoS）。所以这里只拦**自己也失败过**的来源
            # IP：爆破者每个 IP 都有失败记录，照样被拦；干净的管理员 IP 不受
            # 牵连（它自己的输错次数仍由上面的按 IP 计数管着）。
            icnt, ifirst = _fail.get(ip, [0, 0.0])
            if icnt > 0 and (now - ifirst) < LOCK_SECONDS:
                return True
    return False


def record_fail(ip: str, username: str | None = None) -> None:
    now = time.time()
    cnt, first = _fail.get(ip, [0, now])
    if (now - first) >= LOCK_SECONDS:
        cnt, first = 0, now
    _fail[ip] = [cnt + 1, first]

    if username:
        key = username.lower()
        ucnt, ufirst = _user_fail.get(key, [0, now])
        if (now - ufirst) >= LOCK_SECONDS:
            ucnt, ufirst = 0, now
        _user_fail[key] = [ucnt + 1, ufirst]

    _prune(_fail)
    _prune(_user_fail)


def clear_fail(ip: str, username: str | None = None) -> None:
    _fail.pop(ip, None)
    if username:
        _user_fail.pop(username.lower(), None)


# ── 会话吊销 ─────────────────────────────────────────────
# 每个用户带一个会话版本号 sv（存在 users.json 的该用户条目上，缺省 0）。
# 递增它 = 立即让该用户已签发的所有 cookie 失效。用于：
#   - 改密码后（被盗会话不能继续用）
#   - 改角色后（旧权限快照作废）
#   - 管理员手动「吊销会话」
# 为什么不用全局 epoch：那会连带把其他用户的登录踢掉，属于过度杀伤。
def session_version(cfg: dict, username: str) -> int:
    for u in cfg.get('users', []):
        if u.get('username') == username:
            try:
                return int(u.get('sv') or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def revoke_sessions(username: str) -> int:
    """递增该用户的会话版本，返回新值。其全部既有 cookie 立即失效。"""
    cfg = load_users()
    new_sv = 0
    for u in cfg.get('users', []):
        if u.get('username') == username:
            try:
                new_sv = int(u.get('sv') or 0) + 1
            except (TypeError, ValueError):
                new_sv = 1
            u['sv'] = new_sv
            break
    save_users(cfg)
    return new_sv


def audit(actor: dict | None, action: str, target: str = '', detail: str = '',
          ip: str = '') -> None:
    """记录一条管理端审计日志。

    为什么必须做：本次事故中，攻击者改掉管理员密码、建了自己的账号，而**服务端
    没有留下任何痕迹**——只能靠反代 access log 去猜。管理端是公网入口，
    敏感操作（登录、改密码、增删用户、改安全配置）必须可追溯。

    `ip` 是可选的结构化来源地址。历史调用把来源写进 detail（`来源 x.x.x.x`），
    这里保留兼容；新调用应同时传 `ip`，方便按列筛选/告警，不必再解析文本。
    审计属于旁路，任何异常都不能影响请求本身（与请求日志同样的处理原则）。
    """
    try:
        from . import db
        db.add_audit_log(
            (actor or {}).get('username') or 'anonymous',
            str(action),
            str(target or ''),
            str(detail or '')[:500],
            str(ip or '')[:64],
        )
    except Exception:  # noqa: BLE001
        pass


# ── FastAPI 依赖 ─────────────────────────────────────────
def _cookie_identity(request: Request, cfg: dict) -> dict:
    """会话 Cookie 的身份解析（**既有逻辑，逐字保留**）。

      - role 以表里的为准，不用 cookie 里的快照（否则降权后旧 cookie 仍是管理员）
      - 用户已被删除 → 拒绝（否则删号后其 cookie 在有效期内仍然通行）
      - 会话版本不匹配 → 拒绝（改密码 / 吊销后旧 cookie 立即失效）
    """
    token = request.cookies.get(config.COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail='未登录')
    obj = _unsign(token, cfg['secret'])
    if not obj:
        raise HTTPException(status_code=401, detail='未登录')

    username = str(obj.get('username') or '')
    if not username:
        raise HTTPException(status_code=401, detail='未登录')
    row = next((u for u in cfg.get('users', []) if u.get('username') == username), None)
    if not row:
        # 用户已被删除：其 token 不应继续有效
        raise HTTPException(status_code=401, detail='未登录')
    if int(obj.get('sv') or 0) != session_version(cfg, username):
        # 改密码 / 改角色 / 手动吊销之后，旧 token 作废
        raise HTTPException(status_code=401, detail='登录状态已失效，请重新登录')
    if idle_expired(obj):
        # 闲置超时（滑动窗口）：常用的人会不断续期，放着不用的到点失效。
        # 与上面那条分开报，是为了让用户知道「不是我密码/权限变了，是太久没用」。
        raise HTTPException(status_code=401, detail='登录已超时（长时间未操作），请重新登录')
    user = {'username': username, 'role': row.get('role', 'viewer')}
    # 续期请求交给中间件写 cookie（这里**不产生副作用**：current_user 只解析身份，
    # 被 FastAPI 依赖注入在任意接口上调用，若在这里写响应会耦合不必要的层）。
    # 记录在 request.state 上，由 main.py 的中间件统一处理。
    if needs_renewal(obj):
        request.state.session_renew = True
    return user


def _bearer_token(request: Request) -> str:
    """取管理面 API Token（`Authorization: Bearer wbt_...`，也接受 `X-API-Key`）。

    只认 `wbt_` 前缀的形态，其余一律当「没有」——避免把网关密钥（`wbk_`）或
    别的东西误当管理凭据。
    """
    auth = request.headers.get('authorization', '') or ''
    tok = auth[7:].strip() if auth.lower().startswith('bearer ') else ''
    if not tok:
        tok = (request.headers.get('x-api-key') or '').strip()
    return tok if tok.startswith(tokensvc.TOKEN_PREFIX) else ''


def _token_identity(request: Request, token: str) -> dict:
    """作用域化 API Token 的身份解析（见 docs/api-tokens.md）。

    角色由 token 的 **scope** 决定（不查用户表，避免与用户表产生暧昧耦合）；
    失败时**不区分**「不存在 / 已停用 / 已过期」，只回一个笼统的 401，不给探测者
    信号。失败同样计入既有的按 IP 失败计数（与登录共用），成功则清除。
    """
    ip = client_ip(request)
    row = tokensvc.resolve(token)
    if not row or not tokensvc.usable(row):
        record_fail(ip)
        audit(None, 'token_rejected', '', f'来源 {ip}')
        raise HTTPException(status_code=401, detail='未登录')
    clear_fail(ip)
    tokensvc.touch(int(row['id']), ip)
    # 标记来源，供 require_session_admin 识别并拒绝高危接口。
    request.state.token_id = int(row['id'])
    return {
        'username': f"token:{row['name']}",
        'role': tokensvc.scope_to_role(row['scope']),
    }


def current_user(request: Request) -> dict:
    """解析请求身份。**这是管理端唯一的身份入口**。

    两条凭据路径，**会话优先**：

      1. **签名 Cookie**（浏览器）——既有行为逐字保留，含改密码 / 吊销 / 闲置
         超时的专门提示；role 以用户表为准，用户被删或会话版本不匹配即拒绝。
      2. **`Authorization: Bearer wbt_...`**（作用域化 API Token，见
         docs/api-tokens.md）——角色由 token 的 scope 决定，并在 `request.state`
         上标记来源，供 `require_session_admin` 拒绝高危接口。

    安全事件记录（2026-09-14）：这里**曾经**接受 `users.json` 里 `api_keys`
    数组中的 `X-API-Key`，只要命中就直接授予 admin。那是一条提权后门，已彻底
    移除。现在的 token 走 SQLite `api_tokens` 表：**只存哈希**、有 scope、
    可吊销、有审计——与那次事故的形态刻意不同（见设计文档的「必须避免重蹈的
    覆辙」一节）。下游调用请用网关的 `/v1/*`（那套密钥走 `api_keys` 表、
    只授权模型调用，与后台权限无关）。
    """
    cfg = load_users()
    session_err: HTTPException | None = None
    if request.cookies.get(config.COOKIE_NAME):
        try:
            return _cookie_identity(request, cfg)
        except HTTPException as exc:
            session_err = exc
    token = _bearer_token(request)
    if token:
        return _token_identity(request, token)
    raise session_err or HTTPException(status_code=401, detail='未登录')


def require_admin(user: dict = Depends(current_user)) -> dict:
    if user.get('role') != 'admin':
        raise HTTPException(status_code=403, detail='需要管理员权限')
    return user


def require_session_admin(request: Request,
                          user: dict = Depends(require_admin)) -> dict:
    """要求**会话**管理员：拒绝用 API Token 调用。

    用于高危 / 不可逆接口（一键更新、删账号、清日志、改安全策略，以及管理
    token 自身的接口）。原则是「能改代码、能扩大权限、或能抹掉痕迹的入口只对
    真人开放」——这样即便 token 泄露，攻击面也不包含这些。（见 docs/api-tokens.md）
    """
    if getattr(request.state, 'token_id', None) is not None:
        raise HTTPException(status_code=403,
                            detail='该接口不接受 API Token，请用会话登录后操作')
    return user
