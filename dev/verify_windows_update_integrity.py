"""验一遍 Windows 一键更新脚本（`windows/update.ps1`）的**发布包校验门禁** —— 拿真实产物跑。

为什么值得单独跑：这个门禁失效时**不报错**，只悄悄放行（2026-10-13 之前这一层是
离线私钥验签，那版就踩过「`WaitForExit` 的返回值进输出流 → 判空数组恒假 → 篡改包
照样安装」这种坑）。现在的门禁是 sha256 比对，失效形态更简单也更隐蔽：
`-not` 写反、或者校验结果压根没被用来中止流程。

这个脚本从**工作区**的 `windows/update.ps1` 里截出 `Test-PackageDigest`，在真
PowerShell 里跑三种输入：

    摘要一致（发布页给的那份）        → 期望放行
    摘要不一致（改一个字节的包）      → 期望中止
    没给摘要（拿不到发布页摘要）      → **期望中止**（除非显式 -AllowUnverified）
    没给摘要 + -AllowUnverified       → 期望放行并大声警告

用法：

    python dev/verify_windows_update_integrity.py <放产物的目录>

目录里需要有 `workbuddy-manager-v*.tar.gz`。需要 Windows PowerShell。
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

TEMPLATE = """
$ErrorActionPreference = 'Continue'
function Try-One($label, $a, $d) {
  try {
    $ok = Test-PackageDigest -archivePath $a -expectedDigest $d
    $type = if ($ok -is [array]) { 'ARRAY' } else { 'BOOL' }
    $gate = if (-not $ok) { 'ABORT' } else { 'PROCEED' }   # 与脚本里的 `if (-not (Test-PackageDigest …))` 同义
    Write-Output ("RESULT|{0}|{1}|{2}" -f $label, $type, $gate)
  } catch {
    Write-Output ("RESULT|{0}|EXC|{1}" -f $label, $_.Exception.Message)
  }
}
$AllowUnverified = $false
Try-One 'match'     '@TAR@'      '@DIGEST@'
Try-One 'mismatch'  '@TAMPERED@' '@DIGEST@'
Try-One 'no-digest' '@TAR@'      ''
$AllowUnverified = $true
Try-One 'no-digest-allowed' '@TAR@' ''
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
    digest = hashlib.sha256(tar.read_bytes()).hexdigest()

    tampered = art / '_tampered_probe.tar.gz'
    data = bytearray(tar.read_bytes())
    data[len(data) // 2] ^= 0xFF
    tampered.write_bytes(bytes(data))

    src = (REPO / 'windows' / 'update.ps1').read_text(encoding='utf-8-sig')
    m = re.search(r'^function Test-PackageDigest.*?(?=^})', src, re.S | re.M)
    if not m:
        print('windows/update.ps1 里找不到 Test-PackageDigest', file=sys.stderr)
        return 2

    ps = REPO / 'dev' / '.windows-digest-probe.ps1'
    body = (TEMPLATE.replace('@TAR@', str(tar))
            .replace('@TAMPERED@', str(tampered))
            .replace('@DIGEST@', f'sha256:{digest}'))
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

    want = {'match': ('BOOL', 'PROCEED'),
            'mismatch': ('BOOL', 'ABORT'),
            # 拿不到摘要默认拒绝：本机下载目录里的同名包是最现实的投毒路径，
            # 「只打印本地 sha256 就放行」等于没有门禁。
            'no-digest': ('BOOL', 'ABORT'),
            'no-digest-allowed': ('BOOL', 'PROCEED')}
    bad = []
    for label, expected in want.items():
        actual = got.get(label)
        mark = 'OK  ' if actual == expected else 'FAIL'
        print(f'  {mark} {label}: {actual}（期望 {expected}）')
        if actual != expected:
            bad.append(label)
    if bad:
        print('\n门禁不对：' + '、'.join(bad))
        print('提示：返回 ARRAY 说明函数里有语句把值写进了输出流（判空数组会恒假）；'
              'mismatch 若是 PROCEED，就是「比对结果没被拿来中止」。')
        return 1
    print('\n校验门禁正确：一致放行 / 不一致中止 / 拿不到摘要默认拒绝（可显式放行）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
