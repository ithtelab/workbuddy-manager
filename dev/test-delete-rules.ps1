# 卸载删除规则的验收：编译探针（dev/delete-rules-probe.cs + 启动器源码）并跑场景。
#
# 为什么值得单独跑：卸载**不可逆**，而它失效的样子恰恰是「一切正常」——
# issue #178 就是：程序装进了用户自己的 D:\workspace，卸载把那个目录连同资料删了。
# 规则（只删安装清单登记的 + 程序自有目录，其余保留；非安装目录直接拒绝）必须能
# 脱离图形界面反复验证。
#
# 用法：
#     powershell -ExecutionPolicy Bypass -File dev\test-delete-rules.ps1
#
# 退出码：0 = 全过；非 0 = 有不符合预期的场景（或编译失败）。
$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$srcDir = Join-Path $repoRoot 'windows\launcher\src'
$probe = Join-Path $repoRoot 'dev\delete-rules-probe.cs'
$outExe = Join-Path $env:TEMP ('wb-delete-rules-' + [Guid]::NewGuid().ToString('N') + '.exe')

$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path $csc)) { $csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe' }
if (-not (Test-Path $csc)) { throw '未找到 csc.exe（需要 .NET Framework 4.x）' }

# 启动器源码里 Program.cs 有自己的 Main，这里排除它、用探针的 Main
$sources = Get-ChildItem (Join-Path $srcDir '*.cs') |
    Where-Object { $_.Name -ne 'Program.cs' } |
    ForEach-Object { $_.FullName }
$sources += $probe

$rsp = Join-Path $env:TEMP ('wb-delete-rules-' + [Guid]::NewGuid().ToString('N') + '.rsp')
$lines = @(
    '-nologo',
    '-target:exe',
    '-main:WorkBuddyLauncher.DeleteRulesProbe',
    ('-out:' + $outExe),
    '-r:System.Windows.Forms.dll',
    '-r:System.Drawing.dll',
    '-r:System.IO.Compression.dll',
    '-r:System.IO.Compression.FileSystem.dll'
) + $sources
Set-Content -Path $rsp -Value $lines -Encoding UTF8

try {
    Write-Host '[build] 编译探针…' -ForegroundColor Cyan
    & $csc ('@' + $rsp)
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $outExe)) { throw '编译失败' }

    Write-Host '[run] 跑删除规则场景…' -ForegroundColor Cyan
    & $outExe
    $code = $LASTEXITCODE
} finally {
    Remove-Item $rsp, $outExe -Force -ErrorAction SilentlyContinue
}

if ($code -ne 0) {
    Write-Host '[FAIL] 删除规则不符合预期' -ForegroundColor Red
    exit $code
}
Write-Host '[OK] 删除规则正确：只删自己的，别人的一律保留，非安装目录拒绝' -ForegroundColor Green
exit 0
