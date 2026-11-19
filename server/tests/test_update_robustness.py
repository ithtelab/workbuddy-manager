"""更新流程的健壮性回归（上游构建失败相关）。

真实事故：上游某提交删掉了一批脚本、却漏改 Dockerfile（仍 COPY 已删除的
`scripts/probe_active.py`），导致 `docker compose up --build` 失败。管理端
当时只把它当普通命令失败上报，用户看到的是难懂的 checksum 报错，
既不知道原因、也没有退路。

这里覆盖两件事：
  1. Dockerfile 预检能提前查出「引用了不存在的文件」，且不误报；
  2. 构建失败诊断能把 docker 报错归纳成人话。

随包上游的同步语义在下面的 BundledUpstreamSyncTest 里覆盖。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

# 复用 worker 里的纯函数（deploy/update.py 是独立脚本，不在包内）
DEPLOY_DIR = pathlib.Path(__file__).resolve().parents[2] / 'deploy'
sys.path.insert(0, str(DEPLOY_DIR))
import update as worker  # noqa: E402


class DockerfilePrecheck(unittest.TestCase):
    """构建前预检：查出 Dockerfile 引用了仓库里不存在的文件。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        self._orig = worker.UPSTREAM_DIR
        worker.UPSTREAM_DIR = self.dir

    def tearDown(self) -> None:
        worker.UPSTREAM_DIR = self._orig
        self._tmp.cleanup()

    def _write(self, dockerfile: str, files: list[str]) -> None:
        (self.dir / 'Dockerfile').write_text(dockerfile, encoding='utf-8')
        for f in files:
            path = self.dir / f
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('', encoding='utf-8')

    def test_detects_missing_copy(self) -> None:
        """复刻真实事故：COPY scripts/probe_active.py 但文件已删除。"""
        self._write(
            'FROM alpine\n'
            'COPY --from=build /out/wb2api /app/wb2api\n'
            'COPY login.sh /app/\n'
            'COPY scripts/probe_active.py /app/scripts/probe_active.py\n',
            ['login.sh'],
        )
        self.assertEqual(worker._missing_copy_sources(), ['scripts/probe_active.py'])

    def test_no_false_positive_when_present(self) -> None:
        self._write('COPY a.py /app/a.py\nCOPY b.py /app/b.py\n', ['a.py', 'b.py'])
        self.assertEqual(worker._missing_copy_sources(), [])

    def test_ignores_stage_copy_wildcard_and_url(self) -> None:
        """阶段拷贝 / 通配符 / URL 不是本地文件，不能误报。"""
        self._write(
            'COPY --from=build /out/x /app/x\n'
            'COPY scripts/*.py /app/scripts/\n'
            'COPY https://example.com/f /app/f\n'
            'RUN echo COPY not.a.copy /tmp/\n',
            [],
        )
        self.assertEqual(worker._missing_copy_sources(), [])

    def test_missing_dockerfile_is_not_an_error(self) -> None:
        self.assertEqual(worker._missing_copy_sources(), [])


class BuildFailureDiagnosis(unittest.TestCase):
    """把 docker 的报错归纳成一句人话。"""

    def test_detects_dockerfile_missing_file(self) -> None:
        out = ('failed to calculate checksum of ref x::y: '
               '"/scripts/probe_active.py": not found')
        hint = worker._diagnose_build_failure(out)
        self.assertIn('不存在的文件', hint)
        self.assertIn('上游', hint)

    def test_detects_disk_and_network(self) -> None:
        self.assertIn('磁盘', worker._diagnose_build_failure('no space left on device'))
        self.assertIn('网络', worker._diagnose_build_failure('dial tcp 1.2.3.4:443: i/o timeout'))
        self.assertIn('权限', worker._diagnose_build_failure('permission denied'))

    def test_unknown_returns_empty(self) -> None:
        self.assertEqual(worker._diagnose_build_failure('some novel error'), '')


class BundledUpstreamSyncTest(unittest.TestCase):
    """面板更新时同步包内自带的上游源码（上游公开地址已不可用，源码随包分发）。

    这里钉的是三条**不能错**的性质：

      · 用户数据一个字节都不能动：`config.json`（api_key）、`auths/`（账号凭据）、
        `data/`（运行数据）——写坏了用户就登不上、账号全丢；
      · 用户在 compose 里加过的东西（网络 / 卷 / 端口之外）要保住 —— issue #28 就
        是这套定制被更新抹掉，容器重建后连不上网络；
      · 内容没变要返回 0：调用方据此跳过容器重建，否则每次更新都白等一次构建、
        还把上游短暂停掉。
    """

    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self.target = root / 'upstream-target'
        self.bundle = root / 'bundle'
        self._orig = worker.UPSTREAM_DIR
        worker.UPSTREAM_DIR = self.target

        # 目标：一个「非 git」的上游目录（装的时候用 UPSTREAM_SRC / 包内 upstream）
        (self.target / 'scripts').mkdir(parents=True)
        (self.target / 'docker-compose.yml').write_text(
            'services:\n  workbuddy2api:\n    ports: ["127.0.0.1:7863:7863"]\n',
            encoding='utf-8')
        (self.target / 'scripts' / 'checkin.py').write_text('OLD\n', encoding='utf-8')
        (self.target / 'scripts' / 'keep.py').write_text('KEEP\n', encoding='utf-8')
        # 用户数据与凭据
        (self.target / 'config.json').write_text('{"api_key": "secret"}\n', encoding='utf-8')
        (self.target / 'auths').mkdir()
        (self.target / 'auths' / 'workbuddy-a.json').write_text('{"uid":"a"}', encoding='utf-8')
        (self.target / 'data').mkdir()
        (self.target / 'data' / 'state.json').write_text('{"x":1}', encoding='utf-8')

        # 包内自带的上游源码
        (self.bundle / 'scripts').mkdir(parents=True)
        (self.bundle / 'docker-compose.yml').write_text(
            'services:\n  workbuddy2api:\n    ports: ["7863:7863"]\n', encoding='utf-8')
        (self.bundle / 'scripts' / 'checkin.py').write_text('NEW\n', encoding='utf-8')
        (self.bundle / 'scripts' / 'brand_new.py').write_text('NEW FILE\n', encoding='utf-8')
        # 包内不该带用户数据，但真带了也不能覆盖（防御）
        (self.bundle / 'config.json').write_text('{"api_key": "from-bundle"}\n', encoding='utf-8')

    def tearDown(self) -> None:
        worker.UPSTREAM_DIR = self._orig
        self._tmp.cleanup()

    def _rep(self):
        class R:
            def __init__(self):
                self.logs = []

            def log(self, msg, level='info'):
                self.logs.append(str(msg))

        return R()

    def test_syncs_code_and_counts_changes(self) -> None:
        n = worker._sync_bundled_upstream(self.bundle, self._rep())
        # 改写 2 个（checkin.py + docker-compose.yml）、新增 1 个。
        # compose 也算改动是对的：包内那份是「上游原样」，同步后由调用方
        # 重新施加端口收敛（见下一条用例）。
        self.assertEqual(n, 3, '应改写 2 个 + 新增 1 个')
        self.assertEqual((self.target / 'scripts' / 'checkin.py').read_text(encoding='utf-8'),
                         'NEW\n')
        self.assertTrue((self.target / 'scripts' / 'brand_new.py').is_file())
        self.assertEqual((self.target / 'scripts' / 'keep.py').read_text(encoding='utf-8'),
                         'KEEP\n', '包里没有的文件保留原地，不删')

    def test_port_convergence_is_reapplied_after_sync(self) -> None:
        """同步会采用包内那份（上游原样、公网可达）的 compose —— 安全基线必须重来一遍。

        这一步在 update_manager 里紧跟同步之后：`enforce_local_bind`。漏掉
        它，上游就会重新监听 0.0.0.0:7863，端口收敛形同虚设。
        """
        worker._sync_bundled_upstream(self.bundle, self._rep())
        worker.enforce_local_bind(self._rep())
        compose = (self.target / 'docker-compose.yml').read_text(encoding='utf-8')
        self.assertIn('127.0.0.1:7863:7863', compose,
                      '同步后没有重新收敛端口 —— 上游会暴露到公网')

    def test_user_data_untouched(self) -> None:
        worker._sync_bundled_upstream(self.bundle, self._rep())
        self.assertEqual((self.target / 'config.json').read_text(encoding='utf-8'),
                         '{"api_key": "secret"}\n', 'config.json 被覆盖 = 用户 api_key 丢了')
        self.assertEqual((self.target / 'auths' / 'workbuddy-a.json').read_text(encoding='utf-8'),
                         '{"uid":"a"}', 'auths 被覆盖 = 账号凭据丢了')
        self.assertEqual((self.target / 'data' / 'state.json').read_text(encoding='utf-8'),
                         '{"x":1}', 'data 被覆盖 = 运行数据丢了')

    def test_customized_compose_is_preserved(self) -> None:
        """除端口收敛之外的定制（加网络等）要保住——issue #28 的原样。"""
        custom = ('services:\n  workbuddy2api:\n'
                  '    ports: ["127.0.0.1:7863:7863"]\n'
                  '    networks: [external-net]\n'
                  'networks:\n  external-net:\n    external: true\n')
        (self.target / 'docker-compose.yml').write_text(custom, encoding='utf-8')
        worker._sync_bundled_upstream(self.bundle, self._rep())
        self.assertIn('external-net',
                      (self.target / 'docker-compose.yml').read_text(encoding='utf-8'),
                      '用户加的 external 网络被抹掉了')

    def test_plain_port_convergence_is_not_treated_as_customization(self) -> None:
        """只有端口收敛（我们自己制造的差异）时，应当**采用包内那份**，不算定制。"""
        worker._sync_bundled_upstream(self.bundle, self._rep())
        self.assertNotIn('127.0.0.1:7863',
                         (self.target / 'docker-compose.yml').read_text(encoding='utf-8'),
                         '端口收敛被误判成用户定制，导致 compose 永远是旧的')

    def test_second_sync_reports_no_change(self) -> None:
        worker._sync_bundled_upstream(self.bundle, self._rep())
        n = worker._sync_bundled_upstream(self.bundle, self._rep())
        self.assertEqual(n, 0, '内容一致时必须返回 0（调用方据此跳过容器重建）')

    def test_git_upstream_is_synced_too(self) -> None:
        """外部上游停更后，git 目录也按发布包内源码同步。"""
        (self.target / '.git').mkdir()
        n = worker._sync_bundled_upstream(self.bundle, self._rep())
        self.assertEqual(n, 3)
        self.assertEqual((self.target / 'scripts' / 'checkin.py').read_text(encoding='utf-8'),
                         'NEW\n', 'git 目录不再是独立更新源，应按包内源码同步')

    def test_missing_bundle_is_noop(self) -> None:
        empty = self.bundle.parent / 'empty'
        empty.mkdir()
        n = worker._sync_bundled_upstream(empty, self._rep())
        self.assertEqual(n, 0)

    def test_update_manager_reapplies_baseline_after_sync(self) -> None:
        """接线：同步之后紧跟端口收敛，且**在重建之前**。

        上面那条只证明了 `enforce_local_bind` 本身有效，没证明它被调用 ——
        这正是我评审时栽过的那类假绿（测了函数、没测调用点与顺序）。
        """
        src = pathlib.Path(worker.__file__).read_text(encoding='utf-8')
        i = src.index('_sync_bundled_upstream(new_root')
        tail = src[i:i + 800]
        self.assertIn('enforce_local_bind(rep)', tail,
                      '同步了包内源码却没重新施加端口收敛 —— 上游会暴露到公网')
        self.assertLess(tail.index('enforce_local_bind(rep)'),
                        tail.index('rebuild_upstream(rep)'),
                        '顺序反了：收敛必须在重建之前，否则重建时用的是公网可达那份 compose')


if __name__ == '__main__':
    unittest.main()


class BundledUpstreamLineEndingsTest(unittest.TestCase):
    """随包上游的行尾归一（issue #202）。

    历史：包内源码是**在 Windows 上打的包**，资产里本身就带 CRLF（实测 293/298 个
    文件）。同步是字节拷贝 → 宿主源码树里 `login.sh` 首行成了
    `#!/usr/bin/env bash\r`，容器里解释器路径对不上，一键登录 / 签到 / 积分这些
    依赖脚本的功能整体失效（报告方 10-06 就踩到，只能在自己那份更新器里兜底）。

    这里钉四件事：
      1. 包内 CRLF → 落地是 LF（脚本能执行）；
      2. **已经带 CRLF 的旧树要能自愈**（只比字节会把两边都是 CRLF 判成「没变」，
         于是那些机器永远修不回来）；
      3. 二进制一个字节都不动（猜错的代价不对称）；
      4. 只差行尾的文件不再算「有改动」（否则每次更新都白等一次网关重建）。
    """

    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self.target = root / 'upstream-target'
        self.bundle = root / 'bundle'
        self._orig = worker.UPSTREAM_DIR
        worker.UPSTREAM_DIR = self.target
        for d in (self.target, self.bundle):
            d.mkdir(parents=True, exist_ok=True)
            (d / 'docker-compose.yml').write_bytes(b'services: {}\n')

    def tearDown(self) -> None:
        worker.UPSTREAM_DIR = self._orig
        self._tmp.cleanup()

    def _rep(self):
        class R:
            def __init__(self):
                self.logs = []

            def log(self, msg, level='info'):
                self.logs.append(str(msg))

        return R()

    def test_crlf_package_lands_as_lf(self) -> None:
        """包内带 CRLF（历史资产就是这样）→ 落地必须是 LF，否则脚本执行不了。"""
        (self.bundle / 'login.sh').write_bytes(
            b'#!/usr/bin/env bash\r\n# login.sh\r\necho hi\r\n')
        worker._sync_bundled_upstream(self.bundle, self._rep())
        raw = (self.target / 'login.sh').read_bytes()
        self.assertNotIn(b'\r', raw, '首行 shebang 后带 \r 会让容器里的解释器路径对不上')
        self.assertEqual(raw, b'#!/usr/bin/env bash\n# login.sh\necho hi\n')

    def test_existing_crlf_tree_is_healed(self) -> None:
        """本机已经是 CRLF（被历史版本写坏的）→ 即使内容与包内一致也要就地修好。"""
        content_lf = b'#!/usr/bin/env bash\necho hi\n'
        (self.bundle / 'login.sh').write_bytes(content_lf)
        (self.target / 'login.sh').write_bytes(content_lf.replace(b'\n', b'\r\n'))
        n = worker._sync_bundled_upstream(self.bundle, self._rep())
        self.assertGreater(n, 0, '只差行尾也算改动 —— 否则那些机器永远修不回来')
        self.assertEqual((self.target / 'login.sh').read_bytes(), content_lf)

    def test_second_run_reports_no_change(self) -> None:
        """修好之后再跑一次：内容一致（且都是 LF）→ 返回 0，不白等一次网关重建。"""
        (self.bundle / 'login.sh').write_bytes(b'#!/usr/bin/env bash\necho hi\r\n')
        (self.target / 'login.sh').write_bytes(b'#!/usr/bin/env bash\necho hi\r\n')
        first = worker._sync_bundled_upstream(self.bundle, self._rep())
        self.assertGreater(first, 0, '第一次要把本机那份归一')
        second = worker._sync_bundled_upstream(self.bundle, self._rep())
        self.assertEqual(second, 0, '归一之后应判「没变」')

    def test_binary_bytes_untouched(self) -> None:
        """二进制（含 NUL）一个字节都不动：把数据改坏比留一处行尾问题严重得多。"""
        blob = b'\x89PNG\r\n\x1a\n\x00\x00\x00\r\nEND'
        (self.bundle / 'logo.png').write_bytes(blob)
        worker._sync_bundled_upstream(self.bundle, self._rep())
        self.assertEqual((self.target / 'logo.png').read_bytes(), blob)
