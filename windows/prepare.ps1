# WorkBuddy Manager —— 启动前环境准备（Windows / PowerShell）
# 幂等：已就绪的步骤直接跳过。start.ps1 与桌面启动器在启动前都会调用本脚本。
$ErrorActionPreference = 'Stop'
# 脚本住在 windows\ 子目录里：项目根（server/、web/out、.env、upstream/ 所在处）
# 是它的上一级。拼项目内的路径一律用 $root；只有找**同一目录里的兄弟脚本**才用 $scriptDir。
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
Set-Location $root

# 启用现代 TLS 协议支持
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12 -bor [System.Net.SecurityProtocolType]::Tls13

# 1. 自动初始化并迁移 .env
$envPath = Join-Path $root '.env'

function Normalize-EnvPathValue([string]$value) {
    return $value.Trim().Trim('"').Trim("'").Replace('\', '/').TrimStart('/').ToLowerInvariant()
}

function Convert-WindowsEnvContent([string]$content) {
    $rootSlash = ($root -replace '\\', '/')
    $lines = @($content -split "`r?`n")

    # 只把仓库模板里的 Linux 默认值迁成当前安装目录路径；用户自定义值不改。
    # WB_MANAGER_HOST 也属于用户设置：模板默认已是 127.0.0.1，启动器里改成
    # 0.0.0.0（局域网访问）是明确意图，**不能**在每次启动时被重置回环（issue #368：
    # 保存后偶尔生效、重启/开机自启后又变回 127.0.0.1，就是这里无条件改写造成的）。
    # 兼容 /opt/...、opt/...、\\opt\\... 等旧文件实际可能出现的写法。
    $pathDefaults = @{
        'WB_AUTH_DIR' = @('/opt/workbuddy2api/auths', "$rootSlash/upstream/auths")
        'WB_UPSTREAM_CONFIG' = @('/opt/workbuddy2api/config.json', "$rootSlash/upstream/config.json")
        'WB_UPSTREAM_DIR' = @('/opt/workbuddy2api', "$rootSlash/upstream")
        'WB2API_START_SCRIPT' = @('/opt/workbuddy2api/start-workbuddy2api.cmd', "$rootSlash/upstream/start-workbuddy2api.cmd")
        'WB2API_STOP_SCRIPT' = @('/opt/workbuddy2api/stop-workbuddy2api.cmd', "$rootSlash/upstream/stop-workbuddy2api.cmd")
        'WB2API_LOG_FILE' = @('/opt/workbuddy2api/data/server.err.log', "$rootSlash/upstream/data/server.err.log")
        'WB_DATA_DIR' = @('/opt/workbuddy-manager/data', "$rootSlash/data")
        'WB_DB' = @('/opt/workbuddy-manager/data/manager.db', "$rootSlash/data/manager.db")
        'WB_USERS_FILE' = @('/opt/workbuddy-manager/data/users.json', "$rootSlash/data/users.json")
        'WB_STATIC_DIR' = @('/opt/workbuddy-manager/web/out', "$rootSlash/web/out")
    }

    $linuxUpstreamValues = @(
        (Normalize-EnvPathValue '/opt/workbuddy2api'),
        (Normalize-EnvPathValue '/opt/workbuddy2api/config.json')
    )
    $hasLinuxUpstreamDefault = $false
    foreach ($line in $lines) {
        if ($line -match '^\s*WB_UPSTREAM_(?:DIR|CONFIG)\s*=\s*(.*?)\s*$') {
            if ($linuxUpstreamValues -contains (Normalize-EnvPathValue $Matches[1])) {
                $hasLinuxUpstreamDefault = $true
                break
            }
        }
    }

    $changed = $false
    $seenKeys = @{}
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -notmatch '^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$') { continue }
        $key = $Matches[1]
        $seenKeys[$key] = $true
        $value = $Matches[2].Trim().Trim('"').Trim("'")

        if ($pathDefaults.ContainsKey($key) -and
            (Normalize-EnvPathValue $value) -eq (Normalize-EnvPathValue $pathDefaults[$key][0])) {
            $lines[$i] = $key + '=' + $pathDefaults[$key][1]
            $changed = $true
        } elseif ($key -eq 'WB2API_MODE' -and $value.ToLowerInvariant() -eq 'docker' -and $hasLinuxUpstreamDefault) {
            $lines[$i] = 'WB2API_MODE=native'
            $changed = $true
        } elseif ($key -eq 'WB_SECURE_COOKIE' -and $value -eq 'auto') {
            $lines[$i] = 'WB_SECURE_COOKIE=false'
            $changed = $true
        }
    }

    # 打包版应自带原生上游；旧 .env 若缺少关键变量，补齐当前安装目录的安全默认值。
    # 已存在的自定义值不覆盖，避免把用户的外部上游改掉。
    $nativeBundleReady = (Test-Path (Join-Path $root 'upstream\wb2api.exe')) -and
        (Test-Path (Join-Path $root 'upstream\start-workbuddy2api.cmd'))
    if ($nativeBundleReady) {
        $nativeDefaults = [ordered]@{
            'WB2API_MODE' = 'native'
            'WB_AUTH_DIR' = "$rootSlash/upstream/auths"
            'WB_UPSTREAM_CONFIG' = "$rootSlash/upstream/config.json"
            'WB_UPSTREAM_DIR' = "$rootSlash/upstream"
            'WB2API_START_SCRIPT' = "$rootSlash/upstream/start-workbuddy2api.cmd"
            'WB2API_STOP_SCRIPT' = "$rootSlash/upstream/stop-workbuddy2api.cmd"
            'WB2API_LOG_FILE' = "$rootSlash/upstream/data/server.err.log"
            'WB_DATA_DIR' = "$rootSlash/data"
            'WB_DB' = "$rootSlash/data/manager.db"
            'WB_USERS_FILE' = "$rootSlash/data/users.json"
            'WB_STATIC_DIR' = "$rootSlash/web/out"
        }
        foreach ($entry in $nativeDefaults.GetEnumerator()) {
            if (-not $seenKeys.ContainsKey($entry.Key)) {
                $lines += $entry.Key + '=' + $entry.Value
                $changed = $true
            }
        }
    }

    if ($changed) {
        Write-Host '[MIGRATE] 检测到旧版 Linux/Docker 模板默认配置，已迁移为当前 Windows 原生路径；自定义配置保持不变。' -ForegroundColor Yellow
    }
    return ($lines -join [Environment]::NewLine)
}

if (-not (Test-Path $envPath)) {
    $envExample = Join-Path $root '.env.example'
    if (Test-Path $envExample) {
        Write-Host "[INIT] 首次运行，正在根据模板生成 Windows 配置文件..." -ForegroundColor Cyan
        $envContent = Get-Content $envExample -Raw -Encoding UTF8
        $envContent = Convert-WindowsEnvContent $envContent
        [System.IO.File]::WriteAllText($envPath, $envContent, (New-Object System.Text.UTF8Encoding($false)))
    }
} else {
    # 覆盖安装会保留 .env：只迁移仍等于模板默认值的 Linux 路径/模式，不覆盖自定义值。
    $existingEnv = Get-Content $envPath -Raw -Encoding UTF8
    $migratedEnv = Convert-WindowsEnvContent $existingEnv
    if ($migratedEnv -cne $existingEnv) {
        [System.IO.File]::WriteAllText($envPath, $migratedEnv, (New-Object System.Text.UTF8Encoding($false)))
    }
}

# 2. 自动初始化上游 config.json（若缺少）
$upstreamCfg = Join-Path $root 'upstream\config.json'
if (-not (Test-Path $upstreamCfg)) {
    $exampleCfg = Join-Path $root 'upstream\config.example.json'
    if (Test-Path $exampleCfg) {
        Write-Host "[INIT] 首次运行，正在初始化上游网关 config.json 与随机 API Key..." -ForegroundColor Cyan
        $cfgJson = Get-Content $exampleCfg -Raw | ConvertFrom-Json
        $cfgJson.api_key = 'wbk_' + [System.Guid]::NewGuid().ToString('N')
        $cfgText = $cfgJson | ConvertTo-Json -Depth 10
        [System.IO.File]::WriteAllText($upstreamCfg, $cfgText, (New-Object System.Text.UTF8Encoding($false)))
    }
}

# 3. 确保必要目录存在
$needDirs = @('data', 'upstream\auths', 'upstream\data')
foreach ($nd in $needDirs) {
    $fullNd = Join-Path $root $nd
    if (-not (Test-Path $fullNd)) { New-Item -ItemType Directory -Path $fullNd -Force | Out-Null }
}

# 4. 准备 Python 环境：预置运行时优先；其次 uv 建独立虚拟环境；最后回退自动更新流程
. (Join-Path $scriptDir 'python-env.ps1')
$python = Get-WbPython -Root $root
if (-not $python) {
    $uvExe = Join-Path $root '.tools\uv\uv.exe'
    if (Test-Path $uvExe) {
        Write-Host "[INIT] 首次运行，正在自动构建独立的 Python 3.11 运行环境..." -ForegroundColor Cyan
        & $uvExe venv .venv --python 3.11
        $python = Join-Path $root '.venv\Scripts\python.exe'
        if (-not (Test-Path $python)) { throw "[ERROR] 虚拟环境创建失败，请检查网络后重试（windows\prepare.ps1）" }
        & $uvExe pip install -r (Join-Path $root 'server\requirements.txt') --python $python
    } else {
        Write-Host "[INIT] 正在调用自动更新程序同步依赖与前端产物..." -ForegroundColor Yellow
        & (Join-Path $scriptDir 'update.ps1')
        $python = Get-WbPython -Root $root
    }
}

# 5. 依赖健全性检查：关键包缺了就补装（预置运行时被破坏、依赖未装全等）
if ($python) {
    & $python -c "import fastapi, uvicorn" 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[INIT] 检测到 Python 依赖不完整，正在安装 requirements.txt ..." -ForegroundColor Yellow
        $reqFile = Join-Path $root 'server\requirements.txt'
        $uvExe = Join-Path $root '.tools\uv\uv.exe'
        if (Test-Path $uvExe) { & $uvExe pip install -r $reqFile --python $python }
        else { & $python -m pip install -r $reqFile }
    }
}

# 6. 检查前端静态产物，若缺失自动触发同步
$webIndex = Join-Path $root 'web\out\index.html'
if (-not (Test-Path $webIndex)) {
    Write-Host "[WARN] 检测到尚未下载前端页面包，正在调用更新程序自动拉取..." -ForegroundColor Yellow
    & (Join-Path $scriptDir 'update.ps1')
}

Write-Host "[PREP] 环境准备完成。" -ForegroundColor Green
