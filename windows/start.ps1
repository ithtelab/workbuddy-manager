# WorkBuddy Manager —— 本机启动脚本（Windows / PowerShell）
$ErrorActionPreference = 'Stop'
# 脚本住在 windows\ 子目录里：项目根（server/、web/out、.env、upstream/ 所在处）
# 是它的上一级。拼项目内的路径一律用 $root；只有找**同一目录里的兄弟脚本**才用 $scriptDir。
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
Set-Location $root

# 启用现代 TLS 协议支持
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12 -bor [System.Net.SecurityProtocolType]::Tls13

# 1. 自动初始化 .env 配置（若首次使用）
$envPath = Join-Path $root '.env'
if (-not (Test-Path $envPath)) {
    $envExample = Join-Path $root '.env.example'
    if (Test-Path $envExample) {
        Write-Host "[INIT] 首次运行，正在自动根据模板生成适配 Windows 的 .env 配置文件..." -ForegroundColor Cyan
        $envContent = Get-Content $envExample -Raw -Encoding UTF8
        $rootSlash = ($root -replace '\\', '/')
        
        # 将 Linux /opt 模板路径自动转换为 Windows 本地路径，并启用 native 模式
        $envContent = $envContent.Replace('/opt/workbuddy2api/auths', "$rootSlash/upstream/auths")
        $envContent = $envContent.Replace('/opt/workbuddy2api/config.json', "$rootSlash/upstream/config.json")
        $envContent = $envContent.Replace('/opt/workbuddy2api/start-workbuddy2api.cmd', "$rootSlash/upstream/start-workbuddy2api.cmd")
        $envContent = $envContent.Replace('/opt/workbuddy2api/stop-workbuddy2api.cmd', "$rootSlash/upstream/stop-workbuddy2api.cmd")
        $envContent = $envContent.Replace('/opt/workbuddy2api/data/server.err.log', "$rootSlash/upstream/data/server.err.log")
        $envContent = $envContent.Replace('/opt/workbuddy2api', "$rootSlash/upstream")
        $envContent = $envContent.Replace('/opt/workbuddy-manager/data/manager.db', "$rootSlash/data/manager.db")
        $envContent = $envContent.Replace('/opt/workbuddy-manager/data/users.json', "$rootSlash/data/users.json")
        $envContent = $envContent.Replace('/opt/workbuddy-manager/data', "$rootSlash/data")
        $envContent = $envContent.Replace('/opt/workbuddy-manager/web/out', "$rootSlash/web/out")
        $envContent = $envContent.Replace('WB2API_MODE=docker', 'WB2API_MODE=native')
        $envContent = $envContent.Replace('WB_MANAGER_HOST=0.0.0.0', 'WB_MANAGER_HOST=127.0.0.1')
        $envContent = $envContent.Replace('WB_SECURE_COOKIE=auto', 'WB_SECURE_COOKIE=false')
        
        # 无 BOM：模板首行虽然目前是注释（BOM 落在注释里无害），但一旦有人调整模板顺序，
        # 第一个变量名就会带 ﻿ 前缀而整个失效 —— 不给自己留这种地雷。
        [System.IO.File]::WriteAllText($envPath, $envContent, (New-Object System.Text.UTF8Encoding($false)))
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

# 确保必要目录存在
$needDirs = @('data', 'upstream\auths', 'upstream\data')
foreach ($nd in $needDirs) {
    $fullNd = Join-Path $root $nd
    if (-not (Test-Path $fullNd)) { New-Item -ItemType Directory -Path $fullNd -Force | Out-Null }
}

# 3. 自动初始化独立 Python 虚拟环境（免装 Python 机制）
$python = Join-Path $root '.venv\Scripts\python.exe'
$uvExe = Join-Path $root '.tools\uv\uv.exe'
if (-not (Test-Path $python)) {
    if (Test-Path $uvExe) {
        Write-Host "[INIT] 首次运行，正在自动构建独立的 Python 3.11 运行环境..." -ForegroundColor Cyan
        & $uvExe venv .venv --python 3.11
        & $uvExe pip install -r (Join-Path $root 'server\requirements.txt') --python $python
    } else {
        Write-Host "[INIT] 正在调用自动更新程序同步依赖与前端产物..." -ForegroundColor Yellow
        & (Join-Path $scriptDir 'update.ps1')
    }
}

# 4. 检查前端静态产物，若缺失自动触发同步
$webIndex = Join-Path $root 'web\out\index.html'
if (-not (Test-Path $webIndex)) {
    Write-Host "[WARN] 检测到尚未下载前端页面包，正在调用更新程序自动拉取..." -ForegroundColor Yellow
    & (Join-Path $scriptDir 'update.ps1')
}

# 5. 编码设置与参数解析
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$bindHost = '127.0.0.1'
$bindPort = '7864'
if (Test-Path $envPath) {
    foreach ($line in Get-Content $envPath) {
        if ($line -match '^\s*WB_MANAGER_HOST\s*=\s*(\S+)') { $bindHost = $Matches[1] }
        elseif ($line -match '^\s*WB_MANAGER_PORT\s*=\s*(\S+)') { $bindPort = $Matches[1] }
    }
}

# 6. 联动启动上游服务
$upstreamScript = Join-Path $root 'upstream\start-workbuddy2api.cmd'
$upstreamStopScript = Join-Path $root 'upstream\stop-workbuddy2api.cmd'
if (Test-Path $upstreamScript) {
    Write-Host "[INFO] 正在启动上游 workbuddy2api 网关服务..." -ForegroundColor Cyan
    & cmd.exe /c $upstreamScript
}

# 7. 启动主控面板并唤起浏览器
Write-Host "[INFO] WorkBuddy Manager 已就绪: http://${bindHost}:${bindPort}（按 Ctrl+C 退出）" -ForegroundColor Green
Start-Process "http://${bindHost}:${bindPort}"

try {
    & $python -m uvicorn server.main:app --env-file .env --host $bindHost --port $bindPort
} finally {
    if (Test-Path $upstreamStopScript) {
        & cmd.exe /c $upstreamStopScript
    }
}
