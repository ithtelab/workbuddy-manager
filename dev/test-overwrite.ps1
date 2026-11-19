# 覆盖安装场景测试（薄封装）：
#   1) test-installer.ps1 首装          2) 启动服务（产生运行状态与数据）
#   3) test-installer.ps1 -SkipCleanup 同目录覆盖安装（自动停服 + 数据保留）
#   4) 覆盖后的服务重启验证
$ErrorActionPreference = 'Stop'
$installDir = 'D:\ai-project\_wb_test_install'
$testPort = 17864
$envFile = Join-Path $installDir '.env'
$script:failed = $false

function Check($ok, $label) {
    if ($ok) { Write-Host ('OK: ' + $label) } else { Write-Host ('FAIL: ' + $label); $script:failed = $true }
}

function Wait-Healthy($seconds) {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $seconds) {
        try {
            $resp = Invoke-WebRequest -Uri ('http://127.0.0.1:' + $testPort + '/api/healthz') -UseBasicParsing -TimeoutSec 2
            if ($resp.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Seconds 2
    }
    return $false
}

# ── 轮 A：首装 ──────────────────────────────────────────
& (Join-Path $PSScriptRoot 'test-installer.ps1') 2>&1 | Select-Object -Last 2
if (-not (Test-Path (Join-Path $installDir 'WorkBuddyManager.exe'))) { Write-Host 'FAIL: 首装未完成'; exit 1 }

# 端口设为测试端口并启动服务
if (Test-Path $envFile) {
    $lines = Get-Content $envFile
    $lines = $lines | ForEach-Object { if ($_ -match '^WB_MANAGER_PORT=') { 'WB_MANAGER_PORT=' + $testPort } else { $_ } }
    Set-Content $envFile $lines -Encoding UTF8
}
& (Join-Path $installDir 'windows\service-tools.ps1') start 2>&1 | Out-Null
Start-Sleep -Seconds 3
$runningBefore = Wait-Healthy 60
Check $runningBefore '覆盖前服务运行中'
if (-not $runningBefore) { & (Join-Path $installDir 'windows\service-tools.ps1') stop 2>&1 | Out-Null; exit 1 }

# ── 轮 B：覆盖安装 ──────────────────────────────────────
& (Join-Path $PSScriptRoot 'test-installer.ps1') -SkipCleanup 2>&1 | Select-Object -Last 4

# ── 覆盖后的服务重启验证 ────────────────────────────────
& (Join-Path $installDir 'windows\service-tools.ps1') start 2>&1 | Out-Null
Start-Sleep -Seconds 3
$healthyAfter = Wait-Healthy 60
Check $healthyAfter '覆盖安装后服务可正常启动'

# 收尾
& (Join-Path $installDir 'windows\service-tools.ps1') stop 2>&1 | Out-Null
if (Test-Path $installDir) { Remove-Item $installDir -Recurse -Force }
if ($failed) { Write-Host 'RESULT: FAIL'; exit 1 }
Write-Host 'RESULT: PASS'
