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



class WindowsUpdaterTrustChainTest(unittest.TestCase):
    """Windows 一键更新脚本（`update.ps1`）的信任链守卫。

    这个是真实踩出来的三条，每条都**不会让脚本报错**，只会让它悄悄失守：

      · `Test-PackageSignature` 里 `$proc.WaitForExit()` 的返回值会进输出流，
        函数于是返回 `@($true, $false)`；调用点是 `if (-not $isSigValid)`，
        PowerShell 对**非空数组**取反恒为 `$false` —— **篡改包照样被安装**
        （实测：篡改包 → `[True,False]` → 门禁放行）；
      · `allowed_signers` 用 `[System.Text.Encoding]::UTF8` 写会带 **BOM**，
        而 ssh-keygen 读到 BOM 整份文件解析失败 —— **真包也判「验签不过」**
        （实测 exit=255 vs 无 BOM 的 exit=0）；
      · 内嵌公钥一旦与 `deploy/update.py` 里的签发公钥不一致，所有更新都会被拒，
        而错误看起来像「包坏了」。
    """

    UPDATER = _ROOT / 'windows' / 'update.ps1'

    def test_script_exists(self) -> None:
        self.assertTrue(self.UPDATER.is_file(), f'找不到 {self.UPDATER}')

    def test_process_output_is_suppressed(self) -> None:
        """`WaitForExit` 的返回值必须被吞掉（否则签名门禁恒真）。"""
        src = self.UPDATER.read_text(encoding='utf-8-sig')
        self.assertIn('WaitForExit', src)
        bad = [l.strip() for l in src.splitlines()
               if 'WaitForExit' in l and not l.strip().startswith(('#', '[void]', '$null ='))]
        self.assertEqual(
            bad, [],
            'WaitForExit 的返回值没有被吞掉：它会进函数输出流，把返回值变成非空数组，'
            '`-not $isSigValid` 因此恒为假 —— 验签失败也不会中止。'
            '写成 `$null = $proc.WaitForExit(...)` 或 `[void]$proc.WaitForExit(...)`。',
        )

    def test_allowed_signers_has_no_bom(self) -> None:
        """`allowed_signers` 必须以**无 BOM** 的 UTF-8 写。

        PowerShell 5.1 的 `[System.Text.Encoding]::UTF8` 会写 BOM，ssh-keygen 直接
        解析失败 —— 表现为「官方包也被判验签不过」，用户无法更新。
        """
        src = self.UPDATER.read_text(encoding='utf-8-sig')
        writers = [l.strip() for l in src.splitlines()
                   if 'WriteAllText' in l and 'tempSigners' in l]
        self.assertTrue(writers, '没有找到写 allowed_signers 的那一行')
        for line in writers:
            self.assertNotIn('[System.Text.Encoding]::UTF8', line,
                             f'allowed_signers 用了会写 BOM 的编码：{line}')
            self.assertIn('UTF8Encoding($false)', line,
                          f'allowed_signers 必须显式无 BOM：{line}')

    def test_embedded_pubkey_matches_the_release_key(self) -> None:
        """脚本内嵌的公钥必须与 `deploy/update.py` 的签发公钥一致（换密钥时同步）。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location('wb_upd_pub', _ROOT / 'deploy' / 'update.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        want = ' '.join(str(mod.RELEASE_PUBKEY).split())
        src = self.UPDATER.read_text(encoding='utf-8-sig')
        m = re.search(r"\$pubkey\s*=\s*'([^']+)'", src)
        self.assertIsNotNone(m, '脚本里找不到内嵌公钥')
        self.assertEqual(' '.join(m.group(1).split()), want,
                         '内嵌公钥与 deploy/update.py 不一致 —— 所有真实更新都会被拒绝')
        self.assertEqual(str(mod.RELEASE_SIGNER_ID), 'release')

    def test_no_unverified_source_update(self) -> None:
        """不许在更新流程里 `git reset/fetch`：那是**不经签名**的第二条代码来源。

        会把 origin 指向的任意代码（fork、镜像）铺进用户目录，并丢弃本地改动。
        """
        src = self.UPDATER.read_text(encoding='utf-8-sig')
        offending = [l.strip() for l in src.splitlines()
                     if re.search(r'\bgit\s+(reset|fetch|pull|checkout)\b', l)
                     and not l.strip().startswith('#')]
        self.assertEqual(offending, [],
                         f'更新脚本里出现了不经验签的 git 操作：{offending}')

    def test_confirm_before_destructive_phase(self) -> None:
        """停服务 / 清 web/out / 覆盖文件之前必须有一次确认。"""
        src = self.UPDATER.read_text(encoding='utf-8-sig')
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


if __name__ == '__main__':
    unittest.main()
