# WorkBuddy Manager —— 本机启动脚本（Windows / PowerShell）
$ErrorActionPreference = 'Stop'
# 脚本住在 windows\ 子目录里：项目根（server/、web/out、.env、upstream/ 所在处）
# 是它的上一级。拼项目内的路径一律用 $root；只有找**同一目录里的兄弟脚本**才用 $scriptDir。
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
Set-Location $root

# 1. 环境准备（.env、上游配置、目录、Python 环境、前端产物；幂等，与桌面启动器共用）
& (Join-Path $scriptDir 'prepare.ps1')

# 2. 解析 Python 解释器（预置运行时优先，uv 虚拟环境兜底）
. (Join-Path $scriptDir 'python-env.ps1')
$python = Get-WbPython -Root $root
if (-not $python) {
    Write-Error "未找到可用的 Python 环境（.runtime\python 或 .venv）。请先运行 windows\prepare.ps1 完成准备。"
    exit 1
}

# 3. 编码设置与参数解析
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$bindHost = '127.0.0.1'
$bindPort = '7864'
$envPath = Join-Path $root '.env'
if (Test-Path $envPath) {
    foreach ($line in Get-Content $envPath) {
        if ($line -match '^\s*WB_MANAGER_HOST\s*=\s*(\S+)') { $bindHost = $Matches[1] }
        elseif ($line -match '^\s*WB_MANAGER_PORT\s*=\s*(\S+)') { $bindPort = $Matches[1] }
    }
}

# 监听地址 → 可连接地址：通配地址（0.0.0.0 / ::）与 localhost 都换成 127.0.0.1，
# 裸 IPv6 加方括号。`::` 也归到 127.0.0.1 —— server/serve.py 会把它绑成双栈，
# IPv4 回环必然可连。脚本之间不 dot-source，与 C# 侧 EnvConfig.ToConnectHost 各留一份。
function Get-WbConnectHost($addr) {
    $h = "$addr".Trim()
    if (-not $h) { return '127.0.0.1' }
    if ($h -match '^\[(.*)\]$') { $h = $Matches[1] }
    if ($h -eq '0.0.0.0' -or $h -eq '*' -or $h -eq '::' -or $h -eq '::0') { return '127.0.0.1' }
    if ($h -ieq 'localhost') { return '127.0.0.1' }
    if ($h -match ':') { return "[$h]" }
    return $h
}

# 4. 联动启动上游服务
$upstreamScript = Join-Path $root 'upstream\start-workbuddy2api.cmd'
$upstreamStopScript = Join-Path $root 'upstream\stop-workbuddy2api.cmd'
if (Test-Path $upstreamScript) {
    Write-Host "[INFO] 正在启动上游 workbuddy2api 网关服务..." -ForegroundColor Cyan
    & cmd.exe /c $upstreamScript
}

# 5. 启动主控面板并唤起浏览器
# 显示与浏览器必须用**可连接**地址：监听地址是通配时（0.0.0.0 / ::）拿它拼 URL
# 会得到 http://:::7864 这种非法地址，浏览器打不开还会让人以为服务没起来。
$connectHost = Get-WbConnectHost $bindHost
Write-Host "[INFO] WorkBuddy Manager 已就绪: http://${connectHost}:${bindPort}（按 Ctrl+C 退出）" -ForegroundColor Green
Start-Process "http://${connectHost}:${bindPort}"

try {
    # 走 server/serve.py 而不是 `-m uvicorn`：只有前者能在绑 :: 时关掉 v6only，
    # 让 IPv4（本机虚拟网卡 / 127.0.0.1）也能访问；uvicorn --host 做不到。
    & $python -m server.serve --host $bindHost --port $bindPort --env-file .env
} finally {
    if (Test-Path $upstreamStopScript) {
        & cmd.exe /c $upstreamStopScript
    }
}
