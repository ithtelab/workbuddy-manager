# WorkBuddy Manager 桌面版构建脚本
#
# 用法（任意目录）：
#   powershell -ExecutionPolicy Bypass -File desktop\build.ps1
#   powershell -ExecutionPolicy Bypass -File desktop\build.ps1 -NoShortcut
#   powershell -ExecutionPolicy Bypass -File desktop\build.ps1 -SelfContained
#
# 产物：
#   desktop\app\WorkBuddyManager.exe   （框架依赖，启动最快）
#   如果发布结果是单文件，会自动复制一份到项目根目录 WorkBuddyManager.exe
#
# 目标机要求：Windows 10/11 + Microsoft Edge WebView2 运行时 + .NET 8 Desktop Runtime。
# -SelfContained 会把 .NET 运行时一起打包（体积约 150 MB），适合拷到别的机器。

param(
    [switch]$SelfContained,
    [switch]$NoShortcut
)

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir          # desktop\ 的上一级 = 项目根
$project = Join-Path $scriptDir 'src\WorkBuddyDesktop.csproj'
$outDir = Join-Path $scriptDir 'app'

Write-Host "[BUILD] 项目根: $root"
Write-Host "[BUILD] 工程:   $project"

if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    throw "未找到 dotnet。请安装 .NET 8 SDK（https://dotnet.microsoft.com/download）。"
}

if (Test-Path $outDir) { Remove-Item $outDir -Recurse -Force }

$publishArgs = @(
    'publish', $project,
    '-c', 'Release',
    '-r', 'win-x64',
    '--self-contained', $(if ($SelfContained) { 'true' } else { 'false' }),
    '-o', $outDir,
    '-p:PublishSingleFile=true',
    '-p:IncludeNativeLibrariesForSelfExtract=true'
)

Write-Host "[BUILD] dotnet $($publishArgs -join ' ')"
& dotnet @publishArgs
if ($LASTEXITCODE -ne 0) { throw "dotnet publish 失败（退出码 $LASTEXITCODE）" }

$exe = Join-Path $outDir 'WorkBuddyManager.exe'
if (-not (Test-Path $exe)) { throw "没有找到发布产物：$exe" }

# 单文件发布时把 exe 放到项目根，双击就能启动；带附属 DLL 时留在 desktop\app\
$extraFiles = Get-ChildItem $outDir -File | Where-Object {
    $_.Name -ne 'WorkBuddyManager.exe' -and $_.Extension -notin @('.pdb', '.xml')
}
if ($extraFiles.Count -eq 0) {
    $targetExe = Join-Path $root 'WorkBuddyManager.exe'
    Copy-Item $exe $targetExe -Force
    Write-Host "[BUILD] 已生成单文件：$targetExe"
} else {
    $targetExe = Join-Path $outDir 'WorkBuddyManager.exe'
    Write-Host "[BUILD] 发布目录包含附属文件，exe 位于：$targetExe"
    Write-Host "[BUILD] 附带的文件：$($extraFiles.Name -join ', ')"
}

if (-not $NoShortcut) {
    try {
        $desktop = [Environment]::GetFolderPath('Desktop')
        $lnkPath = Join-Path $desktop 'WorkBuddy Manager.lnk'
        $ws = New-Object -ComObject WScript.Shell
        $lnk = $ws.CreateShortcut($lnkPath)
        $lnk.TargetPath = $targetExe
        $lnk.WorkingDirectory = Split-Path -Parent $targetExe
        $lnk.IconLocation = $targetExe
        $lnk.Description = 'WorkBuddy Manager 桌面控制台'
        $lnk.Save()
        Write-Host "[BUILD] 已创建桌面快捷方式：$lnkPath"
    }
    catch {
        Write-Warning "创建快捷方式失败（不影响使用）：$($_.Exception.Message)"
    }
}

Write-Host "[BUILD] 完成。"
