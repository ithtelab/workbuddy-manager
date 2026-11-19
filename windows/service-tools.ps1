# WorkBuddy Manager —— 后台服务管理（Windows / PowerShell）
#
#   powershell -ExecutionPolicy Bypass -File windows\service-tools.ps1 start    后台启动（日志写 data\）
#   powershell -ExecutionPolicy Bypass -File windows\service-tools.ps1 stop     停止
#   powershell -ExecutionPolicy Bypass -File windows\service-tools.ps1 status   查看状态
#   powershell -ExecutionPolicy Bypass -File windows\service-tools.ps1 restart  重启
#
# 与同目录 start.ps1 的区别：start.ps1 是前台运行（关窗口即停），本脚本用 Start-Process
# 拉起独立进程，关掉终端也不影响；日志落在 data\manager.out.log 与 data\manager.err.log。
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'stop', 'status', 'restart')]
    [string]$Action = 'status'
)

# 项目根在上一级（server/、web/out、.env 所在处）；兄弟脚本用 $scriptDir 找
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
# Python 解释器：预置运行时（.runtime\python）优先，uv 虚拟环境（.venv）兜底
. (Join-Path $scriptDir 'python-env.ps1')
$python = Get-WbPython -Root $root
$outLog = Join-Path $root 'data\manager.out.log'
$errLog = Join-Path $root 'data\manager.err.log'

# 监听地址与端口从 .env 读（与 start.ps1 同一处配置）
$bindHost = '127.0.0.1'
$bindPort = '7864'
$envPath = Join-Path $root '.env'
if (Test-Path $envPath) {
    foreach ($line in Get-Content $envPath) {
        if ($line -match '^\s*WB_MANAGER_HOST\s*=\s*(\S+)') { $bindHost = $Matches[1] }
        elseif ($line -match '^\s*WB_MANAGER_PORT\s*=\s*(\S+)') { $bindPort = $Matches[1] }
    }
}
$port = [int]$bindPort

# 监听地址 → 可连接地址（与 start.ps1、启动器 C# 侧同一套规则）：
# 通配地址（0.0.0.0 / ::）与 localhost 都归到 127.0.0.1 —— server/serve.py 把 ::
# 绑成双栈，IPv4 回环必然可连；其余 IPv6 一律加方括号。
function Get-WbConnectHost($addr) {
    $h = "$addr".Trim()
    if (-not $h) { return '127.0.0.1' }
    if ($h -match '^\[(.*)\]$') { $h = $Matches[1] }
    if ($h -eq '0.0.0.0' -or $h -eq '*' -or $h -eq '::' -or $h -eq '::0') { return '127.0.0.1' }
    if ($h -ieq 'localhost') { return '127.0.0.1' }
    if ($h -match ':') { return "[$h]" }
    return $h
}

function Get-ManagerProcess {
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if (-not $conns) { return @() }
    return @($conns.OwningProcess | Sort-Object -Unique | ForEach-Object {
        Get-Process -Id $_ -ErrorAction SilentlyContinue
    } | Where-Object { $_ })
}

function Stop-Manager {
    $upstreamStop = Join-Path $root 'upstream\stop-workbuddy2api.cmd'
    if (Test-Path $upstreamStop) {
        & cmd.exe /c $upstreamStop
    }

    $procs = Get-ManagerProcess
    if (-not $procs) { Write-Host "端口 $port 没有监听进程，Manager 服务未运行"; return }
    foreach ($proc in $procs) {
        Write-Host "停止 PID $($proc.Id) ($($proc.ProcessName))"
        Stop-Process -Id $proc.Id -Force
    }
    Start-Sleep -Seconds 2
}

function Start-Manager {
    $upstreamStart = Join-Path $root 'upstream\start-workbuddy2api.cmd'
    if (Test-Path $upstreamStart) {
        Write-Host "正在启动上游 workbuddy2api 服务..."
        & cmd.exe /c $upstreamStart
    }

    if (Get-ManagerProcess) { Write-Host "端口 $port 已被占用，服务似乎已在运行（用 status 确认）"; return }
    if (-not $python) {
        # 只报一次就返回：原来 Write-Error 之后会继续往下跑，紧接着再抛一条
        # 「Start-Process: 系统找不到指定的文件」，用户看到的是两条错、猜不出哪条是真因。
        Write-Error "未找到可用的 Python 环境（.runtime\python 或 .venv），请先运行 windows\prepare.ps1 完成准备"
        return
    }
    New-Item -ItemType Directory -Force (Join-Path $root 'data') | Out-Null
    # 中文 Windows 默认 GBK：不切 UTF-8 模式时，读 docker logs（UTF-8）的线程会抛
    # UnicodeDecodeError，任务记录页拿不到上游日志。子进程继承这里的变量。
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    # 走 server.serve 而不是 `-m uvicorn`：绑 :: 时它会把 v6only 关掉（双栈），
    # 否则 IPv4（本机虚拟网卡 / 回环）都访问不到。
    $uvicornArgs = @(
        '-m', 'server.serve',
        '--host', $bindHost,
        '--port', $bindPort,
        '--env-file', '.env'
    )
    $proc = Start-Process -FilePath $python -ArgumentList $uvicornArgs `
        -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru
    Start-Sleep -Seconds 4
    if ($proc.HasExited) {
        Write-Host "启动失败（退出码 $($proc.ExitCode)），错误日志尾部："
        Get-Content $errLog -Tail 20 -ErrorAction SilentlyContinue
        return
    }
    Write-Host "已启动，PID $($proc.Id)，日志：$outLog"
}

switch ($Action) {
    'start'   { Start-Manager }
    'stop'    { Stop-Manager }
    'restart' { Stop-Manager; Start-Manager }
    'status'  {
        $procs = Get-ManagerProcess
        if ($procs) {
            $names = ($procs | ForEach-Object { "PID $($_.Id) ($($_.ProcessName))" }) -join ', '
            Write-Host "WorkBuddy Manager 运行中：$names"
            try {
                # 监听在 :: 时只能走 [::1]（v6only），写死 127.0.0.1 会一直探测失败
                $resp = Invoke-WebRequest -Uri "http://$(Get-WbConnectHost $bindHost):$port/api/healthz" -UseBasicParsing -TimeoutSec 5
                Write-Host "健康检查：$($resp.StatusCode) $($resp.Content)"
            } catch {
                Write-Host "健康检查失败：$($_.Exception.Message)"
            }
        } else {
            Write-Host 'WorkBuddy Manager 未运行'
        }

        $upstreamStatus = Join-Path $root 'upstream\status-workbuddy2api.cmd'
        if (Test-Path $upstreamStatus) {
            Write-Host "上游状态："
            & cmd.exe /c $upstreamStatus
        }
    }
}
