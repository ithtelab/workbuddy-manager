# WorkBuddy Manager 安装包构建脚本
#
# 产物：
#   desktop\setup\dist\WorkBuddyManagerSetup.exe        （单文件安装包，内嵌全部运行时）
#   desktop\setup\dist\WorkBuddyManagerSetup.exe.sha256
#
# 用法（任意目录）：
#   powershell -ExecutionPolicy Bypass -File desktop\setup\build-setup.ps1
#   powershell -ExecutionPolicy Bypass -File desktop\setup\build-setup.ps1 -PythonRuntime "D:\path\to\cpython-3.11"
#
# 说明：
# - 目标机需要 .NET 8 Desktop Runtime（与主程序相同）。
# - Python 运行时默认取本机 uv 管理的 CPython 3.11，并把主目录 .venv 的依赖一起打进
#   .runtime\python\Lib\site-packages；如果项目里已有 .runtime\python，优先用它。

param(
    [string]$PythonRuntime = ""
)

Add-Type -AssemblyName System.IO.Compression.FileSystem

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktopDir = Split-Path -Parent $scriptDir
$root = Split-Path -Parent $desktopDir
$srcDir = Join-Path $scriptDir 'src'
$appOut = Join-Path $scriptDir 'app'
$dist = Join-Path $scriptDir 'dist'

$stage = Join-Path $env:TEMP ('wb-setup-' + [Guid]::NewGuid().ToString('N'))
$payloadZip = Join-Path $env:TEMP ('wb-payload-' + [Guid]::NewGuid().ToString('N') + '.zip')

function Copy-Tree([string]$from, [string]$to, [string[]]$excludeDirs = @()) {
    if (-not (Test-Path $from)) { return }
    New-Item -ItemType Directory -Path $to -Force | Out-Null
    $args = @($from, $to, '/E', '/R:1', '/W:1', '/NFL', '/NDL', '/NJH', '/NJS')
    if ($excludeDirs.Count -gt 0) { $args += '/XD'; $args += $excludeDirs }
    & robocopy @args | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "复制失败：$from -> $to（robocopy $LASTEXITCODE）" }
}

Write-Host "[SETUP] 项目根: $root"
Write-Host "[SETUP] 暂存目录: $stage"

if (-not (Test-Path (Join-Path $root 'WorkBuddyManager.exe'))) {
    throw "缺少主程序 WorkBuddyManager.exe，请先运行 desktop\build.ps1"
}

# ── 1. 组装 payload ────────────────────────────────────────
New-Item -ItemType Directory -Path $stage -Force | Out-Null

Copy-Item (Join-Path $root 'WorkBuddyManager.exe') $stage -Force
Copy-Item (Join-Path $root '.version') $stage -Force

Write-Host "[SETUP] 复制 server / web / upstream"
Copy-Tree (Join-Path $root 'server') (Join-Path $stage 'server') @('__pycache__', 'tests')
Copy-Tree (Join-Path $root 'web\out') (Join-Path $stage 'web\out')
New-Item -ItemType Directory -Path (Join-Path $stage 'upstream') -Force | Out-Null
foreach ($file in @('wb2api.exe', 'config.example.json', 'LICENSE', 'start-workbuddy2api.cmd', 'stop-workbuddy2api.cmd', 'status-workbuddy2api.cmd')) {
    $path = Join-Path $root "upstream\$file"
    if (Test-Path $path) { Copy-Item $path (Join-Path $stage 'upstream') -Force }
}

Write-Host "[SETUP] 复制 uv"
Copy-Tree (Join-Path $root '.tools\uv') (Join-Path $stage '.tools\uv')

# Python 运行时：优先项目里的 .runtime\python，否则取 uv 管理的 CPython 3.11
$runtimeSource = $null
if (Test-Path (Join-Path $root '.runtime\python\python.exe')) {
    $runtimeSource = (Join-Path $root '.runtime\python')
} elseif ($PythonRuntime -and (Test-Path (Join-Path $PythonRuntime 'python.exe'))) {
    $runtimeSource = $PythonRuntime
} else {
    $candidate = Get-ChildItem (Join-Path $env:APPDATA 'uv\python') -Directory -Filter 'cpython-3.11-*' -ErrorAction SilentlyContinue |
        Where-Object { Test-Path (Join-Path $_.FullName 'python.exe') } |
        Select-Object -First 1
    if ($candidate) { $runtimeSource = $candidate.FullName }
}
if (-not $runtimeSource) {
    throw "找不到 Python 3.11 运行时。请用 -PythonRuntime 指定，或先运行 prepare.ps1。"
}

Write-Host "[SETUP] 复制 Python 运行时: $runtimeSource"
Copy-Tree $runtimeSource (Join-Path $stage '.runtime\python')

$siteSource = Join-Path $root '.venv\Lib\site-packages'
if (Test-Path $siteSource) {
    Write-Host "[SETUP] 合并依赖到运行时"
    Copy-Tree $siteSource (Join-Path $stage '.runtime\python\Lib\site-packages') @('__pycache__')
} else {
    Write-Host "[SETUP] 警告：没有找到 .venv\Lib\site-packages"
}

$stagePython = Join-Path $stage '.runtime\python\python.exe'
& $stagePython -c "import fastapi, uvicorn; print('runtime ok')"
if ($LASTEXITCODE -ne 0) { throw "运行时自检失败（import fastapi/uvicorn）" }

# ── 1.5 构建卸载程序并放进 payload ────────────────────────
Write-Host "[SETUP] 构建卸载程序"
$uninstallOut = Join-Path $scriptDir 'uninstall-app'
if (Test-Path $uninstallOut) { Remove-Item $uninstallOut -Recurse -Force }
& dotnet publish (Join-Path $scriptDir 'uninstall\src\WorkBuddyUninstall.csproj') -c Release -r win-x64 `
    --self-contained false -o $uninstallOut `
    -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true
if ($LASTEXITCODE -ne 0) { throw "卸载程序构建失败" }
$uninstallExe = Join-Path $uninstallOut 'WorkBuddyUninstall.exe'
if (-not (Test-Path $uninstallExe)) { throw "没有找到 WorkBuddyUninstall.exe" }
Copy-Item $uninstallExe (Join-Path $stage 'Uninstall.exe') -Force

# ── 2. 打包 ────────────────────────────────────────────────
Write-Host "[SETUP] 压缩 payload…"
if (Test-Path $payloadZip) { Remove-Item $payloadZip -Force }
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $stage, $payloadZip,
    [System.IO.Compression.CompressionLevel]::Optimal,
    $false)
$payloadSize = (Get-Item $payloadZip).Length
Write-Host ("[SETUP] payload: {0:N1} MB" -f ($payloadSize / 1MB))

# ── 3. 构建 setup.exe ─────────────────────────────────────
Write-Host "[SETUP] 构建安装程序"
if (Test-Path $appOut) { Remove-Item $appOut -Recurse -Force }
& dotnet publish (Join-Path $srcDir 'WorkBuddySetup.csproj') -c Release -r win-x64 `
    --self-contained false -o $appOut `
    -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true
if ($LASTEXITCODE -ne 0) { throw "dotnet publish 失败" }

$setupExe = Join-Path $appOut 'WorkBuddyManagerSetup.exe'
if (-not (Test-Path $setupExe)) { throw "没有找到 setup.exe" }

# ── 4. 拼接 payload + 尾部长度与魔数 ───────────────────────
New-Item -ItemType Directory -Path $dist -Force | Out-Null
$output = Join-Path $dist 'WorkBuddyManagerSetup.exe'
if (Test-Path $output) { Remove-Item $output -Force }
Copy-Item $setupExe $output -Force

$lengthBytes = [BitConverter]::GetBytes([int64]$payloadSize)
$magicBytes = [System.Text.Encoding]::ASCII.GetBytes('WBPKG001')
$outStream = [System.IO.File]::Open($output, [System.IO.FileMode]::Append)
try {
    $zipStream = [System.IO.File]::OpenRead($payloadZip)
    try { $zipStream.CopyTo($outStream) } finally { $zipStream.Dispose() }
    $outStream.Write($lengthBytes, 0, $lengthBytes.Length)
    $outStream.Write($magicBytes, 0, $magicBytes.Length)
} finally {
    $outStream.Dispose()
}

$finalSize = (Get-Item $output).Length
Write-Host ("[SETUP] 安装包: {0}（{1:N1} MB）" -f $output, ($finalSize / 1MB))

$hash = (Get-FileHash $output -Algorithm SHA256).Hash.ToLowerInvariant()
"$hash  WorkBuddyManagerSetup.exe" | Out-File (Join-Path $dist 'WorkBuddyManagerSetup.exe.sha256') -Encoding ascii
Write-Host "[SETUP] sha256: $hash"

Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $payloadZip -Force -ErrorAction SilentlyContinue
Write-Host "[SETUP] 完成"
