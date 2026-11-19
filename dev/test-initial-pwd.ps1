# 初始管理员密码流程测试：重装 → 启动 → 弹窗显示 → 剪贴板 → 只弹一次
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
. (Join-Path $PSScriptRoot 'ui-helpers.ps1')

$installDir = 'D:\ai-project\_wb_test_install'
$setupExe = 'D:\ai-project\workbuddy-manager\windows\launcher\dist\WorkBuddyManagerSetup.exe'
$testPort = 17864
$settingsFile = Join-Path $env:APPDATA 'WorkBuddyManager\launcher.conf'
# 与客户端相同：首次提示标记按安装目录哈希隔离，不清除用户其他偏好
$installKeyPath = [System.IO.Path]::GetFullPath($installDir).TrimEnd([System.IO.Path]::DirectorySeparatorChar).ToUpperInvariant()
$sha = [System.Security.Cryptography.SHA256]::Create()
try { $installHash = [BitConverter]::ToString($sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($installKeyPath)), 0, 8).Replace('-', '') }
finally { $sha.Dispose() }
$shownKey = 'initial_pwd_shown_' + $installHash
$script:failed = $false

function Check($ok, $label) {
    if ($ok) { Write-Host ('OK: ' + $label) } else { Write-Host ('FAIL: ' + $label); $script:failed = $true }
}

# 1. 重装（新 exe + 干净环境）
$stopScript = Join-Path $installDir 'windows\service-tools.ps1'
if (Test-Path $stopScript) { & $stopScript stop 2>&1 | Out-Null }
Get-Process -Name 'WorkBuddyManager' -ErrorAction SilentlyContinue | Stop-Process -Force
if (Test-Path $installDir) { Remove-Item $installDir -Recurse -Force }
if (Test-Path $settingsFile) { Remove-Item $settingsFile -Force }
Remove-Item 'C:\Users\Lenovo\Desktop\WorkBuddy Manager.lnk' -Force -ErrorAction SilentlyContinue
# 走既有安装测试逻辑（包含核验）
& (Join-Path $PSScriptRoot 'test-installer.ps1') 2>&1 | Select-Object -Last 2
if (-not (Test-Path (Join-Path $installDir 'WorkBuddyManager.exe'))) { Write-Host 'FAIL: 安装未完成'; exit 1 }
Write-Host 'OK: 已重新安装'

# 2. 端口写入 .env（重装后 .env 不存在，先跑 prepare 生成再替换，避开 7864 生产实例）
$envFile = Join-Path $installDir '.env'
if (-not (Test-Path $envFile)) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $installDir 'windows\prepare.ps1') 2>&1 | Out-Null
}
if (Test-Path $envFile) {
    $lines = Get-Content $envFile
    $lines = $lines | ForEach-Object { if ($_ -match '^WB_MANAGER_PORT=') { 'WB_MANAGER_PORT=' + $testPort } else { $_ } }
    Set-Content $envFile $lines -Encoding UTF8
}
Write-Host ('.env 端口已设为 ' + $testPort)

# 3. 启动控制台并点「启动项目」
$proc = Start-Process (Join-Path $installDir 'WorkBuddyManager.exe') -PassThru
$deadline = (Get-Date).AddSeconds(15)
$win = [IntPtr]::Zero
while ((Get-Date) -lt $deadline) {
    $proc.Refresh()
    if ($proc.MainWindowHandle -ne 0) { $win = $proc.MainWindowHandle; break }
    Start-Sleep -Milliseconds 500
}
if ($win -eq [IntPtr]::Zero) { Write-Host 'FAIL: 控制窗口未出现'; $proc.Kill(); exit 1 }
[WbWin]::SetForegroundWindow($win) | Out-Null
$startBtn = [WbWin]::FindChild($win, 'BUTTON', '启动项目')
[WbWin]::Click($startBtn)
Write-Host 'OK: 已点击「启动项目」'

# 4. 等待服务就绪
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$healthy = $false
while ($sw.Elapsed.TotalSeconds -lt 150) {
    try {
        $resp = Invoke-WebRequest -Uri ('http://127.0.0.1:' + $testPort + '/api/healthz') -UseBasicParsing -TimeoutSec 2
        if ($resp.StatusCode -eq 200) { $healthy = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
}
Write-Host ('服务就绪: ' + $healthy)
if (-not $healthy) { $proc.Kill(); exit 1 }

# 5. 等「初始管理员账号」弹窗出现（最多 15 秒）
$mb = [IntPtr]::Zero
$deadline = (Get-Date).AddSeconds(15)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    $mb = [WbTop]::FindDialog($proc.Id)
    if ($mb -ne [IntPtr]::Zero) { break }
}
if ($mb -eq [IntPtr]::Zero) { Write-Host 'FAIL: 初始账号弹窗未出现'; $proc.Kill(); exit 1 }

# 提取弹窗文本
$dialogText = ''
foreach ($item in [WbWin]::Children($mb)) {
    if ($item[2]) { $dialogText += $item[2] + "`n" }
}
Write-Host '--- 弹窗内容 ---'
Write-Host $dialogText
Check ($dialogText -match 'admin') '弹窗包含用户名 admin'
Check ($dialogText -match '密码已复制到剪贴板') '弹窗提示密码已复制'

# 剪贴板内容应与日志中的初始密码一致
Start-Sleep -Milliseconds 300
$clip = [System.Windows.Forms.Clipboard]::GetText()
$logText = Get-Content (Join-Path $installDir 'data\manager.err.log') -Encoding UTF8 -ErrorAction SilentlyContinue
$logPwd = ($logText | Where-Object { $_ -match '密码\s*:\s*(\S+)' } | Select-Object -Last 1)
$logPwd = if ($logPwd -match '密码\s*:\s*(\S+)') { $Matches[1] } else { '' }
Write-Host ('剪贴板长度: ' + $clip.Length + '  日志密码长度: ' + $logPwd.Length)
Check ($clip -eq $logPwd -and $clip.Length -gt 0) '剪贴板密码与日志一致'

# 点确定关闭弹窗
$okBtn = [WbWin]::FindChildByPrefix($mb, 'BUTTON', '确定')
[WbWin]::Click($okBtn)
Start-Sleep -Seconds 1

# 6. 只弹一次：再次触发重启不应再弹（重启会经历 Wait-Healthy，但 initial_pwd_shown 已标记）
if (Test-Path $settingsFile) {
    $conf = @(Get-Content $settingsFile -ErrorAction SilentlyContinue)
    $keyPattern = '^' + [regex]::Escape($shownKey) + '=1$'
    Check (($conf | Where-Object { $_ -match $keyPattern }) -ne $null) '当前安装目录的首次提示标记已写入'
} else { Check $false 'launcher.conf 存在' }

# 再次打开同一安装的控制台：服务本来已运行时也会检查，但不能重复弹密码
if (-not $proc.HasExited) { $proc.Kill() }
Start-Sleep -Seconds 1
$proc2 = Start-Process (Join-Path $installDir 'WorkBuddyManager.exe') -PassThru
Start-Sleep -Seconds 3
$secondDialog = [WbTop]::FindDialog($proc2.Id)
Check ($secondDialog -eq [IntPtr]::Zero) '第二次启动同一安装不重复弹初始密码'
if (-not $proc2.HasExited) { $proc2.Kill() }

# 收尾
& (Join-Path $installDir 'windows\service-tools.ps1') stop 2>&1 | Out-Null
if ($failed) { Write-Host 'RESULT: FAIL'; exit 1 }
Write-Host 'RESULT: PASS'
