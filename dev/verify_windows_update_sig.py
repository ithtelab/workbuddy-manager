"""验一遍 Windows 一键更新脚本（`windows/update.ps1`）的验签门禁 —— 拿**真实发布产物**跑。

为什么值得单独跑：这个门禁是 Windows 原生部署唯一的信任锚点，而它的失效方式全都
**不报错**、只悄悄失守。开发时真踩过两条：

  ① `Test-PackageSignature` 里 `$proc.WaitForExit()` 的返回值进了输出流，函数于是
     返回 `@($true,$false)`；调用点 `if (-not $isSigValid)` 对非空数组取反恒为
     $false —— **篡改包照样放行**；
  ② `allowed_signers` 用 PS 5.1 的 `[Text.Encoding]::UTF8` 写会带 BOM，ssh-keygen
     解析失败 —— **真包也被判验签不过**（exit=255）。

这个脚本从**工作区**的 `windows/update.ps1` 里截出那个函数，在真 PowerShell 里跑三种输入：

    真包（tar.gz + .sig）           → 期望放行
    改一个字节的包 + 原 .sig        → 期望中止
    只有包、没有 .sig               → 期望中止

用法（先准备一份真实发布产物，例如从 Release 下回来的）：

    python dev/verify_windows_update_sig.py <放产物的目录>

目录里需要有 `workbuddy-manager-v*.tar.gz` 与同名 `.sig`。需要 Windows PowerShell
与 `ssh-keygen.exe`（Win10+ 自带）—— 这正是用户侧的执行环境。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

TEMPLATE = """
$ErrorActionPreference = 'Continue'
function Try-One($label, $a, $s) {
  try {
    $ok = Test-PackageSignature -archivePath $a -sigPath $s
    $type = if ($ok -is [array]) { 'ARRAY' } else { 'BOOL' }
    $gate = if (-not $ok) { 'ABORT' } else { 'PROCEED' }   # 与脚本里的 `if (-not $isSigValid)` 同义
    Write-Output ("RESULT|{0}|{1}|{2}" -f $label, $type, $gate)
  } catch {
    Write-Output ("RESULT|{0}|EXC|{1}" -f $label, $_.Exception.Message)
  }
}
Try-One 'valid'    '@TAR@'      '@SIG@'
Try-One 'tampered' '@TAMPERED@' '@SIG@'
Try-One 'nosig'    '@TAR@'      '@MISSING@'
"""


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    art = Path(sys.argv[1])
    tars = sorted(art.glob('workbuddy-manager-v*.tar.gz'))
    if not tars:
        print(f'{art} 里没有 workbuddy-manager-v*.tar.gz', file=sys.stderr)
        return 2
    tar = tars[-1]
    sig = Path(str(tar) + '.sig')
    if not sig.is_file():
        print(f'缺少签名文件 {sig}', file=sys.stderr)
        return 2

    tampered = art / '_tampered_probe.tar.gz'
    data = bytearray(tar.read_bytes())
    data[len(data) // 2] ^= 0xFF
    tampered.write_bytes(bytes(data))

    src = (REPO / 'windows' / 'update.ps1').read_text(encoding='utf-8-sig')
    m = re.search(r'^function Test-PackageSignature.*?(?=^})', src, re.S | re.M)
    if not m:
        print('windows/update.ps1 里找不到 Test-PackageSignature', file=sys.stderr)
        return 2

    ps = REPO / 'dev' / '.windows-sig-probe.ps1'
    body = (TEMPLATE.replace('@TAR@', str(tar)).replace('@SIG@', str(sig))
            .replace('@TAMPERED@', str(tampered))
            .replace('@MISSING@', str(art / 'nope.sig')))
    # **必须带 BOM**：PowerShell 5.1 读无 BOM 的 UTF-8 会按 ANSI 解，中文一进去就乱、
    # 甚至解析失败（这也是仓库里 .ps1 一律带 BOM 的原因）。
    ps.write_text(m.group(0) + '}\n' + body, encoding='utf-8-sig')
    try:
        r = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                            '-File', str(ps)],
                           capture_output=True, text=True, encoding='utf-8', errors='replace')
        out = r.stdout
    finally:
        ps.unlink(missing_ok=True)
        tampered.unlink(missing_ok=True)

    got = {}
    for line in out.splitlines():
        if line.startswith('RESULT|'):
            _, label, kind, gate = line.split('|')
            got[label] = (kind, gate)

    want = {'valid': ('BOOL', 'PROCEED'), 'tampered': ('BOOL', 'ABORT'), 'nosig': ('BOOL', 'ABORT')}
    bad = []
    for label, expected in want.items():
        actual = got.get(label)
        mark = 'OK  ' if actual == expected else 'FAIL'
        print(f'  {mark} {label}: {actual}（期望 {expected}）')
        if actual != expected:
            bad.append(label)
    if bad:
        print('\n门禁不对：' + '、'.join(bad))
        print('提示：返回值若是 ARRAY，说明 WaitForExit 的输出没被吞掉（篡改包也会放行）；'
              'valid 若是 ABORT，多半是 allowed_signers 带了 BOM。')
        return 1
    print('\n验签门禁正确：真包放行 / 篡改中止 / 缺签名中止')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
