"""Windows 批处理脚本（.cmd）的编码与换行符守卫。

为什么需要它：这类问题**在开发机上不一定看得出来**，而在用户机器上表现成
一串吓人的报错。

现场（v1.0.51 实际发出去了）：`deploy/windows-native/*.cmd` 用 UTF-8 存了中文
注释，而 cmd.exe 按**系统 ANSI 代码页**解析批处理文件——中文 Windows 上是
936/GBK。UTF-8 的多字节序列被当成 GBK 解码后，行边界被吃掉、两行粘成一行，
于是 `REM` 不再位于行首（不再是注释），碎片被当作命令执行：

    '有一…' 不是内部或外部命令，也不是可运行的程序
    'E' 不是内部或外部命令，也不是可运行的程序

更隐蔽的是：**脚本整体可能仍然“能用”**（比如 start.cmd 后面那句 powershell
照样执行），只是每次启动都喷一堆看不懂的报错，用户以为装坏了。所以「能跑通」
不足以说明没问题——必须直接检查字节。

三条机械可查的性质：

1. **纯 ASCII**（.cmd 不能靠 BOM 救：cmd.exe 会把 BOM 当命令的一部分）。
   .ps1 不受影响——PowerShell 认 BOM，本仓的 .ps1 都带 BOM，故不在此列。
2. **CRLF 换行**：cmd 对 LF-only 批处理在 if 块 / goto / 标签边界上有边缘
   解析风险（上游 #163 专门处理过），且脚本是给用户直接运行的。
3. **有 .gitattributes 规则锁住**，否则 git 检出会按平台改写换行符——
   在 Linux 上跑 CI 打包时会把 CRLF 变回 LF，用户下到的包又坏了。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ROOT = Path(__file__).resolve().parents[2]


def _batch_files() -> list[Path]:
    """仓库里所有面向用户的批处理脚本（跳过 node_modules 与 .git）。"""
    found: list[Path] = []
    for p in _ROOT.rglob('*.cmd'):
        parts = set(p.parts)
        if 'node_modules' in parts or '.git' in parts:
            continue
        found.append(p)
    for p in _ROOT.rglob('*.bat'):
        parts = set(p.parts)
        if 'node_modules' in parts or '.git' in parts:
            continue
        found.append(p)
    return sorted(found)


class BatchScriptEncodingTest(unittest.TestCase):
    def test_found_scripts(self) -> None:
        """先确认扫到了东西，否则下面几条会在空集合上「通过」。"""
        files = _batch_files()
        self.assertGreaterEqual(
            len(files), 3, f'只扫到 {len(files)} 个批处理脚本，扫描逻辑可能失效：{files}',
        )
        names = {p.name for p in files}
        self.assertIn('start-workbuddy2api.cmd', names)
        self.assertIn('stop-workbuddy2api.cmd', names)
        self.assertIn('start.cmd', names)

    def test_ascii_only(self) -> None:
        """不得含非 ASCII 字节。

        cmd.exe 用系统 ANSI 代码页解析批处理，UTF-8 中文注释会被拆成乱码、
        吃掉行边界，然后碎片被当命令执行（详见模块 docstring）。
        注释一律用英文——上游自带的三个 .cmd 也是纯 ASCII。
        """
        for p in _batch_files():
            with self.subTest(script=p.relative_to(_ROOT).as_posix()):
                data = p.read_bytes()
                try:
                    data.decode('ascii')
                except UnicodeDecodeError as exc:
                    bad = data[max(0, exc.start - 30):exc.start + 30]
                    self.fail(
                        f'{p.relative_to(_ROOT)} 含非 ASCII 字节（{exc.reason}，'
                        f'位置 {exc.start}）：{bad!r}\n'
                        '  非 ASCII 注释会让 cmd.exe 解析错乱并执行乱码碎片——'
                        '请把注释改成英文（.cmd 不能靠 BOM 解决，'
                        'cmd.exe 会把 BOM 当命令的一部分）。'
                    )

    def test_no_utf8_bom(self) -> None:
        """不得带 UTF-8 BOM：cmd.exe 会把 BOM 当成命令的第一个字符而报错。"""
        for p in _batch_files():
            with self.subTest(script=p.relative_to(_ROOT).as_posix()):
                self.assertFalse(
                    p.read_bytes().startswith(b'\xef\xbb\xbf'),
                    f'{p.relative_to(_ROOT)} 带 UTF-8 BOM，cmd.exe 会报错',
                )

    def test_crlf_line_endings(self) -> None:
        """必须 CRLF 换行（上游 #163：cmd 对 LF-only 批处理有边缘解析风险）。"""
        for p in _batch_files():
            with self.subTest(script=p.relative_to(_ROOT).as_posix()):
                data = p.read_bytes()
                crlf = data.count(b'\r\n')
                bare_lf = data.count(b'\n') - crlf
                self.assertEqual(
                    bare_lf, 0,
                    f'{p.relative_to(_ROOT)} 有 {bare_lf} 处裸 LF（应全部为 CRLF）',
                )
                self.assertGreater(crlf, 0, f'{p.relative_to(_ROOT)} 没有任何 CRLF')
                self.assertNotIn(
                    b'\r\r\n', data,
                    f'{p.relative_to(_ROOT)} 出现 CRCRLF（重复转换过）',
                )


class GitAttributesTest(unittest.TestCase):
    """.gitattributes 必须锁住 .cmd 的换行符，否则发布包里的脚本会变回 LF。"""

    def test_cmd_locked_to_crlf(self) -> None:
        ga = _ROOT / '.gitattributes'
        self.assertTrue(ga.is_file(), '缺少 .gitattributes——.cmd 的 CRLF 无人保障')
        text = ga.read_text(encoding='utf-8')
        # 形如：*.cmd text eol=crlf
        self.assertRegex(
            text, r'(?m)^\s*\*\.cmd\s+.*\beol=crlf\b',
            '.gitattributes 里没有「*.cmd ... eol=crlf」规则：'
            '在 Linux 上检出/打包时 .cmd 会变成 LF，用户下到的发布包里脚本是坏的',
        )

    def test_ps1_files_have_utf8_bom(self) -> None:
        """`.ps1` 必须带 UTF-8 BOM。

        为什么（dev/test-delete-rules.ps1 实测踩到）：没有 BOM 时 Windows PowerShell 5.1
        按**系统 ANSI 代码页**读脚本，中文变成乱码 —— 而且某个汉字的第二字节可能正好是
        `'` 或 `*`，于是引号被吃掉、代码直接解析失败（报「必须在 * 运算符后面提供一个
        值表达式」这种与真实原因毫不相干的错）。`.cmd` 不能带 BOM（cmd.exe 会把 BOM 当
        命令），`.ps1` 相反：PowerShell 认 BOM，本仓的 .ps1 一律带。
        """
        offenders = []
        for path in _ROOT.rglob('*.ps1'):
            parts = set(path.parts)
            if parts & {'.git', 'node_modules', '.next', '.tools', '.runtime', 'dist'}:
                continue
            if not path.read_bytes().startswith(bytes([0xEF, 0xBB, 0xBF])):
                offenders.append(str(path.relative_to(_ROOT)))
        self.assertEqual(
            offenders, [],
            '这些 .ps1 缺 UTF-8 BOM —— PowerShell 5.1 会按 ANSI 读，中文会乱码甚至解析失败：'
            + '、'.join(offenders),
        )

    def test_rule_is_not_vacuous(self) -> None:
        """反证：确认这条规则真的会作用于我们的脚本路径（而不是没匹配上）。"""
        import subprocess

        r = subprocess.run(
            ['git', 'check-attr', 'eol', '--', 'deploy/windows-native/start-workbuddy2api.cmd'],
            cwd=str(_ROOT), capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            self.skipTest('本环境没有 git，跳过（CI 上会跑）')
        self.assertIn('eol: crlf', r.stdout, f'git 没把该文件判成 crlf：{r.stdout!r}')



class WindowsUpdaterIntegrityTest(unittest.TestCase):
    """Windows 一键更新脚本（`update.ps1`）的完整性守卫。

    2026-10-13 起这里不再有「离线私钥验签」（原因见 docs/release-signing.md），
    改为核对发布包 sha256。这条门禁的特点是**失效时不报错**：一句 `-not` 写反、
    或校验结果没被用来中止流程，就变成「装什么都放行」，而界面看起来一切正常。
    所以按文本机械地钉住四件事。
    """

    UPDATER = _ROOT / 'windows' / 'update.ps1'

    def _src(self) -> str:
        return self.UPDATER.read_text(encoding='utf-8-sig')

    def test_script_exists(self) -> None:
        self.assertTrue(self.UPDATER.is_file(), f'找不到 {self.UPDATER}')

    def test_hashes_with_sha256(self) -> None:
        src = self._src()
        self.assertIn('Test-PackageDigest', src, '没有摘要校验函数')
        self.assertIn('Get-FileHash', src, '没有真的算哈希')
        self.assertIn('-Algorithm SHA256', src, '算的不是 sha256 —— 发布页给的是 sha256')

    def test_digest_mismatch_is_fatal(self) -> None:
        """摘要不符必须中止：函数返回 $false，且调用点据此 exit。"""
        src = self._src()
        body = src.split('function Test-PackageDigest', 1)[1].split('\n# ──', 1)[0]
        self.assertIn('return $false', body, '摘要不符的分支没有报失败')
        self.assertIn('已拒绝安装', body, '摘要不符时没有给出明确拒绝语')
        call = [l.strip() for l in src.splitlines() if 'Test-PackageDigest -archivePath' in l]
        self.assertTrue(call, '没有任何地方调用摘要校验')
        self.assertTrue(any(l.startswith('if (-not (Test-PackageDigest') for l in call),
                            f'调用点没有按「失败即中止」写：{call}')
        after = src.split('Test-PackageDigest -archivePath', 1)[1]
        self.assertIn('exit 1', after.split('# ──', 1)[0],
                      '校验失败后没有中止更新流程')

    def test_signature_machinery_is_gone(self) -> None:
        """旧的公钥验签要不留残渣：留着会让人以为还有这道门禁。"""
        src = self._src()
        for token in ('Test-PackageSignature', 'ssh-ed25519', 'allowed_signers',
                      'tar.gz.sig'):
            self.assertNotIn(token, src, f'{token} 仍在脚本里 —— 旧的签名门禁没清干净')

    def test_no_unverified_source_update(self) -> None:
        """不许在更新流程里 `git reset/fetch`：那是**第二条代码来源**。

        会把 origin 指向的任意代码（fork、镜像）铺进用户目录，并丢弃本地改动。
        """
        src = self._src()
        offending = [l.strip() for l in src.splitlines()
                     if re.search(r'\bgit\s+(reset|fetch|pull|checkout)\b', l)
                     and not l.strip().startswith('#')]
        self.assertEqual(offending, [], f'更新流程里出现了 git 代码来源：{offending}')

    def test_confirm_before_destructive_phase(self) -> None:
        """停服务 / 清 web/out / 覆盖文件之前必须有一次确认。"""
        src = self._src()
        self.assertRegex(src, r'Read-Host\s+"[^"]*确认', '更新前没有确认提示')
        # 且确认必须在「停服务」之前
        self.assertLess(src.index('Read-Host'), src.index('stop.ps1'),
                        '确认排在了停服务之后 —— 用户点「不要」时服务已经被停了')



class WindowsScriptLayoutTest(unittest.TestCase):
    """脚本住在 `windows/`：项目根 = 脚本目录的**上一级**。

    这条布局不是审美问题，而是脚本能跑的前提：

      · `start.ps1` / `service-tools.ps1` / `update.ps1` 按「项目根」找 `server/`、
        `web/out`、`.env`、`upstream/`、`.venv/`；
      · `stop.ps1` 调同目录的 `service-tools.ps1`，`start.ps1` 调同目录的 `update.ps1`；
      · `update.ps1` 更新前把这一套脚本备份到 `.tools/scripts_backup`，更新后按名字
        **还原回脚本目录** —— 基准用错的话，更新完脚本会散落到仓库根，下次就找不到了。

    写错的形态都是「脚本语法没错、也能跑，只是在错的地方找文件」：启动时报
    「未找到 .venv」、更新后根目录多出一套脚本。所以这里按文本机械地钉住三条。
    """

    PS1 = ('start.ps1', 'stop.ps1', 'service-tools.ps1', 'update.ps1')
    ENTRY_CMD = (('start.cmd', 'start.ps1'), ('stop.cmd', 'stop.ps1'), ('update.cmd', 'update.ps1'))

    def _scripts(self) -> dict:
        out = {}
        names = list(self.PS1) + [c for c, _ in self.ENTRY_CMD]
        for name in names:
            p = _ROOT / 'windows' / name
            self.assertTrue(p.is_file(), f'windows/{name} 不见了（被挪回根目录了？）')
            out[name] = p.read_text(encoding='utf-8-sig')
        return out

    def test_root_is_the_parent_directory(self) -> None:
        """项目根必须由**脚本目录的上一级**算出，而不是脚本自己所在目录。"""
        for name, src in self._scripts().items():
            if not name.endswith('.ps1'):
                continue
            with self.subTest(script=name):
                self.assertIn('$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path', src)
                self.assertIn('$root = Split-Path -Parent $scriptDir', src,
                              f'{name} 没把项目根当成上一级 —— 它会在脚本目录里找 server/')

    def test_sibling_scripts_use_script_dir(self) -> None:
        """找兄弟脚本（同一目录里的那几个）必须走 `$scriptDir`，不许走 `$root`。"""
        siblings = list(self.PS1) + [c for c, _ in self.ENTRY_CMD]
        for name, src in self._scripts().items():
            if not name.endswith('.ps1'):
                continue
            for line in src.splitlines():
                if line.strip().startswith('#'):
                    continue
                with self.subTest(script=name, line=line.strip()[:60]):
                    self.assertNotIn('Join-Path $root $s', line,
                                     f'{name} 的批量备份/还原又用回 $root 了：{line.strip()[:80]}')
                    for sib in siblings:
                        for quoted in (f"Join-Path $root '{sib}'", f'Join-Path $root "{sib}"'):
                            self.assertNotIn(quoted, line,
                                             f'{name} 用 $root 找兄弟脚本了：{line.strip()[:80]}')

    def test_entry_cmd_stays_sibling(self) -> None:
        """三个双击入口要按 `%~dp0`（自己所在目录）调同目录的 .ps1 —— 它们总是一起挪动。"""
        scripts = self._scripts()
        for cmd, ps1 in self.ENTRY_CMD:
            with self.subTest(script=cmd):
                self.assertIn(f'%~dp0{ps1}', scripts[cmd],
                              f'{cmd} 没有按 %~dp0 调同目录的 {ps1}')


class WindowsPythonIsolationTest(unittest.TestCase):
    """issue #268：预置运行时不能被用户级 site-packages 覆盖。"""

    def test_python_env_disables_user_site(self) -> None:
        src = (_ROOT / 'windows' / 'python-env.ps1').read_text(encoding='utf-8-sig')
        self.assertIn("$env:PYTHONNOUSERSITE = '1'", src,
                      'python-env.ps1 没有禁用用户级 site-packages；'
                      '用户目录里的旧 typing_extensions 会盖住内置运行时')

    def test_runtime_scripts_source_the_common_env(self) -> None:
        """启动/准备/更新/服务脚本都必须 dot-source 这个公共设置。"""
        for name in ('prepare.ps1', 'start.ps1', 'update.ps1', 'service-tools.ps1'):
            with self.subTest(script=name):
                src = (_ROOT / 'windows' / name).read_text(encoding='utf-8-sig')
                self.assertIn('python-env.ps1', src,
                              f'{name} 没加载 python-env.ps1，无法继承 PYTHONNOUSERSITE')


if __name__ == '__main__':
    unittest.main()
