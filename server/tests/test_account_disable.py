"""临时禁用账号（issue #21）：改文件名实现，可逆、不碰凭证。

上游**没有**禁用/启用的 HTTP 接口——它内部有 `Disable`/`ReviveDisabled`，但只被
自身的错误处理调用，没有对外暴露；`state.json` 又每 5 秒被上位机覆盖，改它没有
意义（改了立刻被内存状态刷回去）。

可行路径是**改文件名**：上游用 glob `workbuddy*.json` 收集账号文件
（`internal/auth/auth.go` 的 `AuthFileGlob`），所以把文件改名成
`workbuddy-xxx.json.disabled` 之后它就不再被加载、从池里消失；启用就是改回原名。

新上游（2026-09-18 起）额外要求：那边加了 **auths 目录热加载**——每 5 秒轮询目录
指纹并全量重扫。于是改名**不再需要重启容器**，5 秒内自动生效（我们仍会触发一次
重载：对新版上游是让它立即生效而不等轮询，对旧版上游则是唯一生效途径）。

这里钉住的几条，每条都对应一个真实风险：

  1. **可逆**：凭证一个字节都不能动 —— 删除会丢 token（只能重新扫码），改名不会。
  2. **面板仍能看到**：禁用的账号必须还在列表里（否则「禁用」体验上等同于「删除」，
     用户没法再启用它）。
  3. **显示成「已停用」而不是「未加载」**：禁用后账号必然不在池里，若不单独分档，
     界面会按「上游没加载它」报成「账号文件可能有问题」——把用户自己的操作说成故障。
  4. **幂等**：重复禁用/启用不报错（界面可能重试、连点）。
  5. **文件名校验不放水**：新增的 `.disabled` 形态不能成为目录穿越的新入口。
  6. **写入是原子的**（见 `AtomicAuthWriteTest`）：热加载会让「写了一半的账号文件」
     真的被读到。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.services import tencent  # noqa: E402
from server.services import wb2api  # noqa: E402

UID = '99a07e71deadbeef'


def _auth_doc(uid: str, nickname: str = '测试号') -> dict:
    return {
        'account': {'uid': uid, 'nickname': nickname, 'enterpriseId': 'e'},
        'auth': {'accessToken': 'tok-' + uid, 'refreshToken': 'ref-' + uid,
                 'expiresAt': 4102444800, 'domain': 'copilot.tencent.com'},
    }


class DisableToggleTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._orig = config.AUTH_DIR
        config.AUTH_DIR = self.dir
        self.fname = f'workbuddy-{UID}.json'
        (self.dir / self.fname).write_text(
            json.dumps(_auth_doc(UID), ensure_ascii=False), encoding='utf-8')

    def tearDown(self) -> None:
        config.AUTH_DIR = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _names(self) -> list[str]:
        return sorted(p.name for p in self.dir.iterdir())

    def test_disable_renames_and_keeps_bytes(self) -> None:
        before = (self.dir / self.fname).read_bytes()
        r = wb2api.set_account_disabled(self.fname, True)
        self.assertTrue(r['changed'])
        self.assertTrue(r['disabled'])
        self.assertEqual(r['file'], self.fname + '.disabled')
        # 原文件不在了、改名后的文件在，且**内容逐字节一致**（凭证没动）
        self.assertEqual(self._names(), [self.fname + '.disabled'])
        self.assertEqual((self.dir / (self.fname + '.disabled')).read_bytes(), before)

    def test_enable_restores_original_name(self) -> None:
        wb2api.set_account_disabled(self.fname, True)
        r = wb2api.set_account_disabled(self.fname + '.disabled', False)
        self.assertTrue(r['changed'])
        self.assertFalse(r['disabled'])
        self.assertEqual(r['file'], self.fname)
        self.assertEqual(self._names(), [self.fname])

    def test_idempotent(self) -> None:
        """重复调用不报错、不重复改名（界面可能重试/连点）。"""
        first = wb2api.set_account_disabled(self.fname, True)
        second = wb2api.set_account_disabled(self.fname, True)
        self.assertTrue(first['changed'])
        self.assertFalse(second['changed'], '重复禁用不该再改一次名')
        # 用禁用后的名字再禁用一次也要幂等
        third = wb2api.set_account_disabled(self.fname + '.disabled', True)
        self.assertFalse(third['changed'])
        # 启用同理
        wb2api.set_account_disabled(self.fname, False)
        fourth = wb2api.set_account_disabled(self.fname, False)
        self.assertFalse(fourth['changed'])

    def test_missing_file_raises(self) -> None:
        """文件不存在要报错，不能给出「禁用成功」的假象。"""
        with self.assertRaises(ValueError):
            wb2api.set_account_disabled('workbuddy-nosuchuid.json', True)
        with self.assertRaises(ValueError):
            wb2api.set_account_disabled('workbuddy-nosuchuid.json.disabled', False)

    def test_illegal_filenames_still_rejected(self) -> None:
        """`.disabled` 形态不能成为目录穿越的新入口。"""
        for bad in ('../workbuddy-x.json', 'workbuddy-x.json/../y',
                    '/etc/passwd', 'workbuddy-x.json.disabled/../z',
                    'other.json', 'workbuddy-x.txt', 'workbuddy-x.json.disabledx'):
            with self.assertRaises(ValueError, msg=repr(bad)):
                wb2api.set_account_disabled(bad, True)

    def test_disabled_file_still_listed(self) -> None:
        """禁用后必须仍在面板列表里，否则用户没法再启用它。"""
        wb2api.set_account_disabled(self.fname, True)
        rows = {a['uid']: a for a in wb2api.list_auth_accounts()}
        self.assertIn(UID, rows, '禁用后账号从列表里消失了（等同删除）')
        self.assertTrue(rows[UID]['disabled_by_panel'])
        self.assertEqual(rows[UID]['file'], self.fname + '.disabled')

    def test_enabled_file_not_flagged(self) -> None:
        rows = {a['uid']: a for a in wb2api.list_auth_accounts()}
        self.assertFalse(rows[UID]['disabled_by_panel'])

    def test_glob_no_longer_matches_upstream_pattern(self) -> None:
        """改名后的文件**不匹配**上游的 `workbuddy*.json` —— 这是本方案的全部依据。

        一旦这个假设不成立（上游改了 glob），禁用就会失效（账号仍被加载），
        所以钉住它：用与上游相同的 glob 去匹配，必须匹配不到。
        """
        wb2api.set_account_disabled(self.fname, True)
        matched = [p.name for p in self.dir.glob('workbuddy*.json')]
        self.assertEqual(matched, [], f'改名后仍被上游 glob 匹配到：{matched}')


class PoolMergeReasonTest(unittest.TestCase):
    """禁用账号必然不在池里：合并状态时要写清原因，不能报成「文件有问题」。"""

    def test_reason_mentions_panel_disable(self) -> None:
        accounts = [{'uid': UID, 'file': 'workbuddy-x.json.disabled',
                     'disabled_by_panel': True, 'invalid_reason': ''}]
        # 上游返回空池（禁用后它确实不加载）
        wb2api.merge_pool_status(accounts, {'accounts': []})
        self.assertFalse(accounts[0]['in_pool'])
        self.assertIn('禁用', accounts[0]['invalid_reason'],
                      '禁用导致的「不在池里」没写清原因，界面会误报成文件损坏')

    def test_existing_reason_not_overwritten(self) -> None:
        """已有具体原因（如缺 accessToken）时不要被通用文案覆盖。"""
        accounts = [{'uid': UID, 'file': 'workbuddy-x.json.disabled',
                     'disabled_by_panel': True, 'invalid_reason': '缺少 accessToken'}]
        wb2api.merge_pool_status(accounts, {'accounts': []})
        self.assertEqual(accounts[0]['invalid_reason'], '缺少 accessToken')


class AtomicAuthWriteTest(unittest.TestCase):
    """账号文件必须**原子替换**写入（上游热加载依赖这个前提）。

    背景：上游 2026-09-18 起对 auths 目录做 5 秒一次的轮询热加载，一有变化就全量
    重扫。而「先截断再写」的写入方式存在**长度为 0 的窗口**——轮询若正落在那里，
    读到空文件 → 解析失败 → 账号被判为「已删除」而从池里剔除，随后才被写回。
    表现是账号偶发短暂掉线，日志里却看不出原因。

    上游自己也依赖同一约定：其 `watch.go` 注释写明「半写入的临时文件
    （login.sh 用 tempfile + os.replace 原子替换）不会造成误判」。我们的写入路径
    必须符合同一个前提。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.AUTH_DIR
        config.AUTH_DIR = Path(self._tmp.name)

    def tearDown(self) -> None:
        config.AUTH_DIR = self._orig
        self._tmp.cleanup()

    def _acct(self, **kw) -> dict:
        return {
            'uid': kw.get('uid', 'u1'), 'access_token': kw.get('token', 'AT'),
            'refresh_token': 'RT', 'expires_at': 4102444800, 'domain': '',
            'realm': 'cn', 'nickname': 'N', 'enterprise_id': '',
        }

    def test_unique_temp_names_allow_concurrent_writes(self) -> None:
        """并发写同一账号不能互相踩（固定临时名会让彼此删掉对方的内容）。

        发版前自审发现：起初用固定的 `.{name}.tmp` 作临时文件名，两个线程同时写
        同一账号时共用该文件 —— 先完成者 replace 成功后临时文件已不在，后完成者
        replace 失败并触发清理，把对方刚写好的内容一并删掉。实测 4 个并发线程
        全部报错、且原账号文件消失。

        注：Windows 上「替换被打开的文件」本身受限（POSIX rename 无此限制），
        所以这里只断言**临时文件名彼此不同**这一必要条件，不依赖平台替换语义。
        """
        acct = self._acct()
        names: list[str] = []
        real_replace = os.replace

        def spy(src, dst, *a, **kw):
            names.append(Path(src).name)
            return real_replace(src, dst, *a, **kw)

        with unittest.mock.patch('os.replace', side_effect=spy):
            tencent.write_auth_file(acct)
            tencent.write_auth_file(dict(acct, access_token='AT2'))
        self.assertEqual(len(names), 2)
        self.assertNotEqual(names[0], names[1],
                            '两次写用了同一个临时文件名 —— 并发时会互相覆盖/删除')

    def test_file_mode_is_group_and_other_readable(self) -> None:
        """写出来的账号文件必须**可被其他 uid 读**（宿主部署的关键前提）。

        自审发现：改成 mkstemp 后权限从 umask 的 0644 变成固定的 0600 ——
        宿主部署下本面板以 root 写、上游容器以 uid 10001 读，0600 会让上游读不到
        该账号（表现为账号加进去了但池里没有，且没有任何报错）。

        上游自己的 SaveAtomic 用 0o600，但那是「同进程既写又读」；我们跨 uid，
        前提不同。Windows 权限位语义不完整，故在 POSIX 上才有意义。
        """
        tencent.write_auth_file(self._acct())
        mode = (config.AUTH_DIR / 'workbuddy-u1.json').stat().st_mode
        self.assertTrue(mode & 0o044,
                        f'文件权限 {oct(mode)} —— 其他 uid 读不到，宿主部署会失败')

    def test_explicitly_chmods_to_world_readable(self) -> None:
        """必须**显式** chmod 到可被其他 uid 读 —— 不能依赖 mkstemp 的默认权限。

        上一条断言的是「结果」，但它在 Windows 上会空转（那边 mkstemp 给 0666，
        权限位语义不完整，去掉 chmod 也照样通过）。所以这里直接钉住**行为**：
        写入过程里必须对临时文件调过 chmod 且目标模式含 group/other 读位。

        POSIX 上 mkstemp 固定 0600，不显式 chmod 就会破坏跨 uid 读取（宿主部署
        下本面板以 root 写、上游容器以 10001 读）。
        """
        calls: list[tuple] = []

        with unittest.mock.patch('os.chmod', side_effect=lambda *a, **k: calls.append(a)):
            tencent.write_auth_file(self._acct())

        self.assertTrue(calls, '写入过程没有 chmod —— POSIX 上会留下 0600')
        mode = calls[0][1]
        self.assertTrue(mode & 0o044,
                        f'chmod 到 {oct(mode)}：其他 uid 仍读不到（宿主部署会失败）')

    def test_group_chown_denied_does_not_block_write(self) -> None:
        """属组改不动不该阻断写入 —— 属主对齐就够了。

        实测现场（2026-10-26）：面板以 uid 10001 / gid 987 在宿主机运行，上游容器
        写回的账号文件属组是容器内的 10001。重登时把原文件的属主属组照抄到临时
        文件，`chown` 到 10001 这个组必然 EPERM（非 root 只能改到自己所属的组），
        用户看到「账号已授权成功，但写入账号文件失败：Operation not permitted」，
        并被引去检查一个权限完全正常的目录。

        属主相同就够上游读取（0600 同 uid 可读），属组只影响附加的组可读性，
        所以这类失败要忽略；**属主**改不动才是真故障（见下一条）。
        """
        real_chown = getattr(os, 'chown', None)
        if real_chown is None:
            self.skipTest('该平台没有 os.chown')

        tencent.write_auth_file(self._acct(token='OLD'))
        target = config.AUTH_DIR / 'workbuddy-u1.json'
        mode_before = target.stat().st_mode & 0o777

        def _deny_group(path, uid, gid):
            if gid != -1:
                raise PermissionError(1, 'Operation not permitted', str(path))
            return real_chown(path, uid, gid)

        with mock.patch.object(tencent.os, 'chown', _deny_group, create=True):
            tencent.write_auth_file(self._acct(token='NEW'))

        data = json.loads(target.read_text(encoding='utf-8'))
        self.assertEqual(data['auth']['accessToken'], 'NEW', '新 token 没写进去')
        self.assertEqual(target.stat().st_mode & 0o777, mode_before, '权限模式没保住')
        leftovers = [p.name for p in config.AUTH_DIR.iterdir()
                     if p.name != 'workbuddy-u1.json']
        self.assertEqual(leftovers, [], f'留下了临时文件：{leftovers}')

    def test_owner_chown_denied_fails_write(self) -> None:
        """属主改不动必须报错：跨 uid 部署时上游正是靠属主读文件的。

        静默忽略属主失败会写出一个上游读不到的账号文件——表现为「账号加进去了
        但池里没有」，比一条明确的报错难查得多。
        """
        if getattr(os, 'chown', None) is None:
            self.skipTest('该平台没有 os.chown')

        tencent.write_auth_file(self._acct(token='OLD'))
        target = config.AUTH_DIR / 'workbuddy-u1.json'
        before = target.read_bytes()

        def _deny(*_a, **_k):
            raise PermissionError(1, 'Operation not permitted')

        with mock.patch.object(tencent.os, 'chown', _deny, create=True):
            with self.assertRaises(PermissionError):
                tencent.write_auth_file(self._acct(token='NEW'))

        self.assertEqual(target.read_bytes(), before, '写失败却动了原账号文件')
        leftovers = [p.name for p in config.AUTH_DIR.iterdir()
                     if p.name != 'workbuddy-u1.json']
        self.assertEqual(leftovers, [], f'留下了临时文件：{leftovers}')

    def test_rejects_uid_that_would_escape_auth_dir(self) -> None:
        """uid 来自腾讯响应，是外部输入；异常形态必须拒绝，不能拼进文件名。

        自审发现：uid 未经校验就拼成 `workbuddy-{uid}.json`。`../x` 这类值会让
        路径拐出 auths 目录（`workbuddy-` 前缀只挡住了一部分形态）。真实 uid 是
        uuid，但把它当可信输入是错的 —— 读路径（`wb2api._safe_file`）早有同样的
        白名单，写路径此前漏了。
        """
        bad_uids = ['../evil', 'a/b', 'x.json', '', 'a b', 'a' * 81, 'a\\x']
        for bad in bad_uids:
            with self.subTest(uid=bad):
                with self.assertRaises(ValueError):
                    tencent.write_auth_file(dict(self._acct(), uid=bad))

    def test_accepts_real_uuid_shape(self) -> None:
        """真实 uid（带连字符的 uuid）必须照常接受 —— 校验不能误伤正常账号。"""
        uid = '9b212d8c-f5f7-4ad6-aa20-1d576508c8c1'
        name, _ = tencent.write_auth_file(dict(self._acct(), uid=uid))
        self.assertEqual(name, f'workbuddy-{uid}.json')

    def test_write_leaves_no_temp_file(self) -> None:
        """写完不能留下临时文件（它在 auths 目录里会让人以为是垃圾或半个账号）。"""
        tencent.write_auth_file(self._acct())
        leftovers = [p.name for p in config.AUTH_DIR.iterdir()
                     if p.name != 'workbuddy-u1.json']
        self.assertEqual(leftovers, [], f'留下了多余文件：{leftovers}')

    def test_temp_file_is_not_matched_by_upstream_glob(self) -> None:
        """临时文件名既不能匹配上游的 `workbuddy*.json`，也不能进目录指纹。

        上游热加载的指纹只统计 `.json` 结尾的文件；若临时文件叫
        `workbuddy-u1.json.tmp` 之类，虽然 glob 不收，但可能被指纹计入而产生
        无意义的重扫。以 `.` 开头 + 非 `.json` 结尾两头都避开。
        """
        written: list[str] = []
        real_replace = os.replace

        def spy(src, dst, *a, **kw):
            written.append(Path(src).name)
            return real_replace(src, dst, *a, **kw)

        with unittest.mock.patch('os.replace', side_effect=spy):
            tencent.write_auth_file(self._acct())
        self.assertTrue(written, '没有走 os.replace（那就不是原子替换）')
        for name in written:
            self.assertFalse(name.startswith('workbuddy') and name.endswith('.json'),
                             f'临时文件名会被上游当成账号文件：{name}')

    def test_target_never_observed_empty(self) -> None:
        """目标文件在读到的每一刻都必须是完整可解析的 JSON。

        这是热加载场景的核心不变量：模拟「写入过程中被读取」，读取点只能在
        os.replace 之后发生（替换前目标文件还是旧内容）。
        """
        tencent.write_auth_file(self._acct(token='OLD'))
        seen: list[object] = []
        real_replace = os.replace

        def spy(src, dst, *a, **kw):
            # 替换**之前**读一次目标（此时应还是旧内容，不能是空）
            seen.append(json.loads(Path(dst).read_text(encoding='utf-8')))
            out = real_replace(src, dst, *a, **kw)
            # 替换之后读（应是新内容）
            seen.append(json.loads(Path(dst).read_text(encoding='utf-8')))
            return out

        with unittest.mock.patch('os.replace', side_effect=spy):
            tencent.write_auth_file(self._acct(token='NEW'))

        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0]['auth']['accessToken'], 'OLD', '替换前被读到半截内容')
        self.assertEqual(seen[1]['auth']['accessToken'], 'NEW')

    def test_failure_cleans_temp_and_keeps_old_file(self) -> None:
        """写失败时：不留临时文件，且**原文件保持完好**（凭证不能丢）。

        让 `os.replace` 失败（而不是让写入失败）：写入现在走 `os.fdopen`，
        替换才是「生效那一刻」——在那里失败最能代表真实故障（磁盘满、权限变化），
        也验证了「失败时原文件没被碰过」这一关键性质。
        """
        tencent.write_auth_file(self._acct(token='GOOD'))
        target = config.AUTH_DIR / 'workbuddy-u1.json'

        with unittest.mock.patch('os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                tencent.write_auth_file(self._acct(token='BROKEN'))

        self.assertEqual(json.loads(target.read_text(encoding='utf-8'))['auth']['accessToken'],
                         'GOOD', '写失败把原凭证弄坏了')
        leftovers = [p.name for p in config.AUTH_DIR.iterdir()
                     if p.name != 'workbuddy-u1.json']
        self.assertEqual(leftovers, [], f'失败后留下临时文件：{leftovers}')


if __name__ == '__main__':
    unittest.main()
