# WorkBuddy Manager —— 跨机器通用自动化更新程序（Windows）
$ErrorActionPreference = 'Stop'
# 脚本住在 windows\ 子目录里：项目根（server/、web/out、.env、upstream/ 所在处）
# 是它的上一级。拼项目内的路径一律用 $root；只有找**同一目录里的兄弟脚本**才用 $scriptDir。
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
Set-Location $root

# 启用现代 TLS 协议支持
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12 -bor [System.Net.SecurityProtocolType]::Tls13

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "        WorkBuddy Manager 通用自动更新程序        " -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

# ── 1. 动态自适应探测用户下载路径（毫秒级精准定位，不搜全盘）─
function Get-SmartDownloadDirectories {
    $dirs = [System.Collections.Generic.List[string]]::new()

    # (1) 读取 .env 中用户显式指定的下载目录（若有）
    $envPath = Join-Path $root '.env'
    if (Test-Path $envPath) {
        foreach ($line in Get-Content $envPath) {
            if ($line -match '^\s*WB_DOWNLOAD_DIR\s*=\s*(.+)$') {
                $customDir = $Matches[1].Trim().Trim('"').Trim("'")
                if (Test-Path $customDir) { $dirs.Add($customDir) }
            }
        }
    }

    # (2) 探测 Windows 注册表系统权威下载文件夹（哪怕用户把下载移到了 D/E/F 盘也能精准命中）
    try {
        $regKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders'
        $regVal = (Get-ItemProperty -Path $regKey -ErrorAction SilentlyContinue).'{374DE290-123F-4565-9164-39C4925E467B}'
        if ($regVal) {
            $realDownloads = [System.Environment]::ExpandEnvironmentVariables($regVal)
            if (Test-Path $realDownloads) {
                if (-not $dirs.Contains($realDownloads)) { $dirs.Add($realDownloads) }
                $compSub = Join-Path $realDownloads 'Compressed'
                if ((Test-Path $compSub) -and (-not $dirs.Contains($compSub))) { $dirs.Add($compSub) }
            }
        }
    } catch {}

    # (3) 探测 IDM (Internet Download Manager) 注册表配置目录（若安装过）
    try {
        $idmKey = 'HKCU:\Software\DownloadManager'
        $idmProps = Get-ItemProperty -Path $idmKey -ErrorAction SilentlyContinue
        if ($idmProps) {
            if ($idmProps.SavePathCompressed -and (Test-Path $idmProps.SavePathCompressed) -and (-not $dirs.Contains($idmProps.SavePathCompressed))) {
                $dirs.Add($idmProps.SavePathCompressed)
            }
            if ($idmProps.SavePath -and (Test-Path $idmProps.SavePath) -and (-not $dirs.Contains($idmProps.SavePath))) {
                $dirs.Add($idmProps.SavePath)
            }
        }
    } catch {}

    # (4) 默认 UserProfile 下的 Downloads（作为双保险兜底）
    $defaultDownloads = Join-Path ([Environment]::GetFolderPath('UserProfile')) 'Downloads'
    if (Test-Path $defaultDownloads) {
        if (-not $dirs.Contains($defaultDownloads)) { $dirs.Add($defaultDownloads) }
        $compDef = Join-Path $defaultDownloads 'Compressed'
        if ((Test-Path $compDef) -and (-not $dirs.Contains($compDef))) { $dirs.Add($compDef) }
    }

    # (5) 当前项目所在根目录（支持用户直接拖入或保存在本目录）
    if (-not $dirs.Contains($root)) { $dirs.Add($root) }

    return $dirs
}

function Find-LocalPackage {
    param(
        [string[]]$searchDirectories,
        [string]$targetVersion
    )
    foreach ($d in $searchDirectories) {
        if (-not (Test-Path $d)) { continue }
        # 只识别官方带签名的 .tar.gz 归档包
        $candidates = Get-ChildItem -Path $d -Filter "workbuddy-manager-*.tar.gz" -Recurse -Depth 2 -ErrorAction SilentlyContinue | `
            Where-Object {
                $_.Length -gt 5000000 -and `
                -not (Test-Path "$($_.FullName).crdownload") -and `
                -not (Test-Path "$($_.FullName).tmp")
            } | Sort-Object LastWriteTime -Descending
        
        foreach ($c in $candidates) {
            # 必须严格存在同名 .sig 签名文件，杜绝未签名包绕过校验
            $sigPath = "$($c.FullName).sig"
            if (-not (Test-Path $sigPath)) { continue }

            if ($targetVersion) {
                if ($c.Name -match [regex]::Escape($targetVersion)) {
                    return @{ Archive = $c; Sig = Get-Item $sigPath }
                }
            } else {
                return @{ Archive = $c; Sig = Get-Item $sigPath }
            }
        }
    }
    return $null
}

# ── 2. 免 API 限制的版本检测（彻底杜绝 GitHub API 403 频率超限）──
function Get-LatestGitHubTag {
    $proxyList = [System.Collections.Generic.List[string]]::new()
    if ($env:HTTPS_PROXY) { $proxyList.Add($env:HTTPS_PROXY) }
    if ($env:ALL_PROXY -and -not $proxyList.Contains($env:ALL_PROXY)) { $proxyList.Add($env:ALL_PROXY) }
    foreach ($dp in @('http://127.0.0.1:7897', 'http://127.0.0.1:7890', 'http://127.0.0.1:10809')) {
        if (-not $proxyList.Contains($dp)) { $proxyList.Add($dp) }
    }
    $proxyList.Add('')

    $curlExe = (Get-Command curl.exe -ErrorAction SilentlyContinue).Source

    # 方式 1: 直接向 github.com 发起 HEAD 请求取重定向 Location（无 60次/小时 API 限制）
    if ($curlExe) {
        foreach ($p in $proxyList) {
            try {
                $cArgs = @('-s', '-I', '--connect-timeout', '4')
                if ($p) { $cArgs += @('-x', $p) }
                $cArgs += 'https://github.com/ithtelab/workbuddy-manager/releases/latest'
                $lines = & $curlExe $cArgs
                foreach ($line in $lines) {
                    if ($line -match 'location:\s*.*?/releases/tag/([^\r\n/?#]+)') {
                        $foundTag = $Matches[1].Trim()
                        if ($foundTag) {
                            return @{ Tag = $foundTag; Proxy = $p }
                        }
                    }
                }
            } catch {}
        }
    }

    # 方式 2: 通过 git ls-remote 获取最新 tag（按 .NET 真实语义化版本大小排序，无 API 限制）
    try {
        $tags = git ls-remote --tags origin
        if ($tags) {
            $parsedList = @()
            foreach ($line in $tags) {
                if ($line -match 'refs/tags/(v?(\d+\.\d+\.\d+[\w\.\-]*))$') {
                    $rawTag = $Matches[1]
                    $coreVer = $Matches[2].Split('-')[0]
                    try {
                        $parsedList += [PSCustomObject]@{
                            Tag = $rawTag
                            SemVer = [version]$coreVer
                        }
                    } catch {}
                }
            }
            if ($parsedList.Count -gt 0) {
                $best = $parsedList | Sort-Object SemVer -Descending | Select-Object -First 1
                return @{ Tag = $best.Tag; Proxy = '' }
            }
        }
    } catch {}

    # 方式 3: 兜底调用 API
    foreach ($p in $proxyList) {
        try {
            $apiParams = @{
                Uri = "https://api.github.com/repos/ithtelab/workbuddy-manager/releases/latest"
                Headers = @{"User-Agent"="PowerShell"}
                TimeoutSec = 4
            }
            if ($p) { $apiParams['Proxy'] = $p }
            $res = Invoke-RestMethod @apiParams
            if ($res.tag_name) { return @{ Tag = $res.tag_name; Proxy = $p } }
        } catch {}
    }

    return $null
}

# ── 3. 官方发布包数字签名验签（ssh-keygen -Y verify）───────────
function Test-PackageSignature {
    param(
        [string]$archivePath,
        [string]$sigPath
    )
    if (-not (Test-Path $archivePath)) { return $false }
    if (-not (Test-Path $sigPath)) {
        Write-Host "[ERROR] 缺少对应的数字签名文件 ($sigPath)，已拒绝安装。" -ForegroundColor Red
        Write-Host "[ERROR] 官方发布均包含 .tar.gz.sig 签名，缺失签名说明发布流程可能被改动或包来源不可信。" -ForegroundColor Red
        return $false
    }

    $sshKeygen = (Get-Command ssh-keygen.exe -ErrorAction SilentlyContinue).Source
    if (-not $sshKeygen) {
        Write-Host "[ERROR] 系统缺少 ssh-keygen.exe，无法校验发布包签名（Windows 10+ 自带 OpenSSH 客户端）。" -ForegroundColor Red
        return $false
    }

    # 官方可信公钥锚点（与 deploy/update.py 内嵌公钥严格保持一致）
    $pubkey = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHEGhxZQjEEK/RbtgcRLuuWji0fVB4E2dVKMnhtLlCkx workbuddy release signing'
    $signer = 'release'

    $tempSigners = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), "allowed_signers_$([System.Guid]::NewGuid().ToString('N'))")
    try {
        # 必须**无 BOM**：PowerShell 5.1 的 [Text.Encoding]::UTF8 会写 BOM，
        # 而 ssh-keygen 读到 BOM 会整份文件解析失败 —— 真包也会被判「验签不过」。
        [System.IO.File]::WriteAllText($tempSigners, "$signer $($pubkey.Trim())`n", (New-Object System.Text.UTF8Encoding($false)))

        Write-Host "[INFO] 正在校验官方数字签名 (ssh-keygen -Y verify)..." -ForegroundColor Cyan
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $sshKeygen
        $psi.Arguments = "-Y verify -f `"$tempSigners`" -I $signer -n file -s `"$sigPath`""
        $psi.UseShellExecute = $false
        $psi.RedirectStandardInput = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.CreateNoWindow = $true

        $proc = [System.Diagnostics.Process]::Start($psi)
        
        $fs = [System.IO.File]::OpenRead($archivePath)
        $fs.CopyTo($proc.StandardInput.BaseStream)
        $fs.Close()
        $proc.StandardInput.Close()

        $stdout = $proc.StandardOutput.ReadToEnd()
        $stderr = $proc.StandardError.ReadToEnd()
        # 必须吞掉返回值：`WaitForExit()` 会把一个 bool 写进**输出流**，于是本函数的
        # 返回值变成 @($true, <下面的 $true/$false>) —— 而调用点是 `if (-not $isSigValid)`，
        # PowerShell 对「非空数组」取反恒为 $false，也就是**验签失败也不会中止**
        # （篡改包照样被解压安装）。实测：篡改包返回 [True,False] → 门禁 PROCEED。
        $null = $proc.WaitForExit(60000)

        $combined = ($stdout + "`n" + $stderr).Trim()

        if ($proc.ExitCode -eq 0 -and $combined -match 'Good') {
            Write-Host "[SUCCESS] 官方签名校验通过：$($combined -replace '`r|`n', ' ')" -ForegroundColor Green
            return $true
        } else {
            Write-Host "[ERROR] ❌ 发布包数字签名校验失败，已拒绝安装！" -ForegroundColor Red
            Write-Host "[ERROR] 可能是包被篡改或产物被替换。ssh-keygen 输出: $combined" -ForegroundColor Red
            return $false
        }
    } finally {
        if (Test-Path $tempSigners) { Remove-Item $tempSigners -Force -ErrorAction SilentlyContinue }
    }
}

# ── 4. 版本比对与流程确认 ─────────────────────────────────
$currentVer = "未知"
$verFile = Join-Path $root '.version'
if (Test-Path $verFile) {
    $currentVer = (Get-Content $verFile -Raw).Trim()
}
Write-Host "[INFO] 本地当前运行版本: $currentVer" -ForegroundColor White

Write-Host "[INFO] 正在获取 GitHub 官方最新发布版本..."
$tagInfo = Get-LatestGitHubTag
$latestVer = if ($tagInfo) { $tagInfo.Tag } else { $null }
$detectedProxy = if ($tagInfo) { $tagInfo.Proxy } else { if ($env:HTTPS_PROXY) { $env:HTTPS_PROXY } else { 'http://127.0.0.1:7897' } }

$searchDirs = Get-SmartDownloadDirectories
$existingPkg = Find-LocalPackage -searchDirectories $searchDirs -targetVersion $null

# 若网络未连通但找到了离线包，从离线包文件名直接推导版本号
if (-not $latestVer -and $existingPkg) {
    if ($existingPkg.Archive.Name -match 'workbuddy-manager-(v?\d+\.\d+\.\d+[\w\.\-]*)\.tar\.gz') {
        $latestVer = $Matches[1]
        Write-Host "[INFO] 已根据已签名离线安装包识别版本: $latestVer" -ForegroundColor Yellow
    } else {
        $latestVer = "离线签名包"
    }
}

# 若网络未连通且没有任何离线包，提示前往面板点「一键更新」或手动下载
if (-not $latestVer -and -not $existingPkg) {
    Write-Host "[WARN] 无法连接 GitHub 且未在常用目录中找到带签名的离线安装包。" -ForegroundColor Yellow
    Write-Host "[INFO] 已检索目录: $($searchDirs -join ' | ')" -ForegroundColor DarkGray
    Write-Host "[TIP] 提示：您可登录 Web 管理面板，在「系统设置 → 一键更新」中直接更新。" -ForegroundColor Cyan
    $openBrowser = Read-Host "是否在浏览器中打开 GitHub Releases 页面手动下载？(Y/n)"
    if ($openBrowser -ne 'n' -and $openBrowser -ne 'N') {
        Start-Process "https://github.com/ithtelab/workbuddy-manager/releases"
        Write-Host "[WAIT] 正在监听下载目录（下载 tar.gz 与同名 .sig 完成后将自动识别并更新）..." -ForegroundColor Cyan
        
        $startTime = [DateTime]::Now
        while ($true) {
            Start-Sleep -Seconds 2
            $detected = Find-LocalPackage -searchDirectories $searchDirs -targetVersion $null
            if ($detected -and ($detected.Archive.LastWriteTime -gt $startTime.AddMinutes(-5))) {
                $existingPkg = $detected
                if ($detected.Archive.Name -match 'workbuddy-manager-(v?\d+\.\d+\.\d+[\w\.\-]*)\.tar\.gz') {
                    $latestVer = $Matches[1]
                } else {
                    $latestVer = "离线签名包"
                }
                break
            }
            if (([DateTime]::Now - $startTime).TotalSeconds -gt 300) {
                Write-Host "[ERROR] 等待下载超时，更新已取消。" -ForegroundColor Red
                exit 1
            }
        }
    } else {
        exit 1
    }
}

Write-Host "[INFO] 官方最新版本: $latestVer" -ForegroundColor Green

if ($currentVer -eq $latestVer) {
    Write-Host "[INFO] 当前已是最新版本 ($currentVer)。" -ForegroundColor Green
    $reinstall = Read-Host "是否强制重新更新覆盖？(y/N)"
    if ($reinstall -ne 'y' -and $reinstall -ne 'Y') {
        Write-Host "[INFO] 操作已安全退出。"
        exit 0
    }
}

# ── 5. 安装包捕获与验签 ───────────────────────────────────
$tempTar = Join-Path $root "update_temp.tar.gz"
$tempSig = Join-Path $root "update_temp.tar.gz.sig"
if (Test-Path $tempTar) { Remove-Item $tempTar -Force }
if (Test-Path $tempSig) { Remove-Item $tempSig -Force }

$targetPkg = Find-LocalPackage -searchDirectories $searchDirs -targetVersion $latestVer

if ($targetPkg) {
    Write-Host "[INFO] 已在目录中精准识别到签名安装包: $($targetPkg.Archive.FullName)" -ForegroundColor Green
    Copy-Item -Path $targetPkg.Archive.FullName -Destination $tempTar -Force
    Copy-Item -Path $targetPkg.Sig.FullName -Destination $tempSig -Force
} else {
    $tarUrl = "https://github.com/ithtelab/workbuddy-manager/releases/download/$latestVer/workbuddy-manager-$latestVer.tar.gz"
    $sigUrl = "https://github.com/ithtelab/workbuddy-manager/releases/download/$latestVer/workbuddy-manager-$latestVer.tar.gz.sig"
    $curlExe = (Get-Command curl.exe -ErrorAction SilentlyContinue).Source
    $downloadSuccess = $false

    # 尝试方式 1: 使用 curl.exe 命令行静默下载 tar.gz 与 .sig
    if ($curlExe) {
        Write-Host "[INFO] 正在尝试后台下载官方签名发布包 ($latestVer)..." -ForegroundColor Cyan
        $cArgsBase = @('-L', '--fail', '--connect-timeout', '10')
        if ($detectedProxy) { $cArgsBase += @('-x', $detectedProxy) }

        & $curlExe ($cArgsBase + @('-o', $tempTar, $tarUrl))
        & $curlExe ($cArgsBase + @('-o', $tempSig, $sigUrl))

        if ($LASTEXITCODE -eq 0 -and (Test-Path $tempTar) -and (Test-Path $tempSig) -and ((Get-Item $tempTar).Length -gt 5000000)) {
            $downloadSuccess = $true
            Write-Host "[SUCCESS] 发布包与签名文件下载完成。" -ForegroundColor Green
        }
    }

    # 尝试方式 2: 调起浏览器下载并自动监听感应
    if (-not $downloadSuccess) {
        Write-Host "[WARN] 命令行下载受阻，自动为您在浏览器中打开官方发布包与签名直链..." -ForegroundColor Yellow
        Start-Process $tarUrl
        Start-Process $sigUrl

        Write-Host "[WAIT] 正在实时监听下载目录（下载完 tar.gz 与 .sig 后将自动识别并验签更新）..." -ForegroundColor Cyan

        $watchTimeoutSeconds = 300
        $startTime = [DateTime]::Now

        while (-not (Test-Path $tempTar) -or -not (Test-Path $tempSig)) {
            Start-Sleep -Seconds 2
            
            if (([DateTime]::Now - $startTime).TotalSeconds -gt $watchTimeoutSeconds) {
                Write-Host "[ERROR] 等待下载超时，操作已取消。" -ForegroundColor Red
                exit 1
            }

            $detected = Find-LocalPackage -searchDirectories $searchDirs -targetVersion $latestVer
            if ($detected -and ($detected.Archive.LastWriteTime -gt $startTime.AddMinutes(-5))) {
                try {
                    $stream = [System.IO.File]::Open($detected.Archive.FullName, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::None)
                    $stream.Dispose()
                    Write-Host "[SUCCESS] 检测到下载完成: $($detected.Archive.FullName)" -ForegroundColor Green
                    Copy-Item -Path $detected.Archive.FullName -Destination $tempTar -Force
                    Copy-Item -Path $detected.Sig.FullName -Destination $tempSig -Force
                    break
                } catch {}
            }
        }
    }
}

# 执行严格验签（信任链核心防线）
$isSigValid = Test-PackageSignature -archivePath $tempTar -sigPath $tempSig
if (-not $isSigValid) {
    Remove-Item $tempTar, $tempSig -Force -ErrorAction SilentlyContinue
    Write-Host "[ERROR] 验签未通过，更新已被强制终止。未做任何文件变动。" -ForegroundColor Red
    exit 1
}

# ── 6. 停服与覆盖确认（关键交互节点）───────────────────────
Write-Host ""
Write-Host "[CONFIRM] 官方数字签名校验完全通过！" -ForegroundColor Green
Write-Host "[CONFIRM] 即将停止当前服务，清空 web/out 并覆盖更新核心文件至 $latestVer。" -ForegroundColor Yellow
Write-Host "[CONFIRM] 更新期间 Web 管理面板服务将短暂离线。" -ForegroundColor Yellow
$confirmProceed = Read-Host "确认继续执行更新？(Y/n)"
if ($confirmProceed -eq 'n' -or $confirmProceed -eq 'N') {
    Remove-Item $tempTar, $tempSig -Force -ErrorAction SilentlyContinue
    Write-Host "[INFO] 用户已取消更新操作。" -ForegroundColor DarkGray
    exit 0
}

# ── 7. 停止运行中服务 ───────────────────────────────────
Write-Host "[INFO] 正在安全停止当前服务进程..." -ForegroundColor Yellow
$stopScript = Join-Path $scriptDir 'stop.ps1'
if (Test-Path $stopScript) { & $stopScript }

# ── 8. 备份本地 Windows 适配脚本（它们在 windows\ 下，按 $scriptDir 找）──
$backupDir = Join-Path $root '.tools\scripts_backup'
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
$scriptsToProtect = @('start.ps1', 'stop.ps1', 'service-tools.ps1', 'start.cmd', 'stop.cmd', 'update.cmd', 'update.ps1')
foreach ($s in $scriptsToProtect) {
    $src = Join-Path $scriptDir $s
    if (Test-Path $src) { Copy-Item $src (Join-Path $backupDir $s) -Force }
}

# ── 9. 安全解压并覆盖 ───────────────────────────────────
Write-Host "[INFO] 正在安全解压并更新前端静态资源 (web/out) 与服务端核心文件..." -ForegroundColor Cyan

# 清空旧的前端静态编译目录，避免历史版本残留
$webOut = Join-Path $root 'web\out'
if (Test-Path $webOut) { Remove-Item $webOut -Recurse -Force }

$venvPython = Join-Path $root '.venv\Scripts\python.exe'
$extractScript = Join-Path $root '.tools\extract_release.py'
New-Item -ItemType Directory -Path (Join-Path $root '.tools') -Force | Out-Null

$pyContent = @'
import tarfile, sys, os
from pathlib import Path

archive_path = sys.argv[1]
root_dir = Path(sys.argv[2])

with tarfile.open(archive_path, "r:gz") as tf:
    for member in tf.getmembers():
        name = member.name.replace("\\", "/")
        parts = name.split("/")
        if len(parts) > 1 and parts[0].startswith("workbuddy-manager-"):
            rel_path = "/".join(parts[1:])
        else:
            rel_path = name
        
        if not rel_path:
            continue
            
        # 严格隔离保护项：绝不覆盖用户个人配置文件及环境
        if (rel_path == ".env" or 
            rel_path.startswith("data/") or 
            rel_path.startswith(".venv/") or 
            rel_path.startswith(".tools/") or 
            rel_path.startswith("upstream/auths/") or 
            rel_path == "upstream/config.json" or 
            rel_path == "upstream/wb2api.exe"):
            continue
            
        # 允许更新的白名单项目产物
        if (rel_path.startswith("web/out/") or 
            rel_path.startswith("server/") or 
            rel_path.startswith("deploy/") or 
            rel_path.startswith("docs/") or 
            rel_path in (".version", "CHANGELOG.md", "README.md", "README.en.md")):
            target = root_dir / rel_path
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with tf.extractfile(member) as source, open(target, "wb") as dest:
                    dest.write(source.read())
'@

[System.IO.File]::WriteAllText($extractScript, $pyContent, [System.Text.Encoding]::UTF8)

& $venvPython $extractScript $tempTar $root
Remove-Item $extractScript -Force -ErrorAction SilentlyContinue

# 更新本地 .version 标记文件
        # 无 BOM：别让版本号被读成「﻿v1.0.x」（面板与更新脚本都会读这个文件）
[System.IO.File]::WriteAllText((Join-Path $root '.version'), $latestVer, (New-Object System.Text.UTF8Encoding($false)))

# 彻底清理临时安装包及签名文件，绝不遗留磁盘垃圾
Remove-Item $tempTar, $tempSig -Force -ErrorAction SilentlyContinue
Write-Host "[INFO] 临时签名安装包已自动彻底清理删除。" -ForegroundColor DarkGray

# 还原 Windows 脚本与带空格路径引号修复
foreach ($s in $scriptsToProtect) {
    $bak = Join-Path $backupDir $s
    if (Test-Path $bak) { Copy-Item $bak (Join-Path $scriptDir $s) -Force }
}
Remove-Item $backupDir -Recurse -Force -ErrorAction SilentlyContinue

$upstreamCmds = @('upstream\start-workbuddy2api.cmd', 'upstream\status-workbuddy2api.cmd', 'upstream\stop-workbuddy2api.cmd')
foreach ($ucmd in $upstreamCmds) {
    $fullCmd = Join-Path $root $ucmd
    if (Test-Path $fullCmd) {
        $content = [System.IO.File]::ReadAllText($fullCmd)
        $content = $content.Replace('-FilePath $env:WB2API_EXE', '-FilePath \"$env:WB2API_EXE\"')
        $content = $content.Replace('-WorkingDirectory $env:WB2API_ROOT', '-WorkingDirectory \"$env:WB2API_ROOT\"')
        $content = $content.Replace('[IO.Path]::GetFullPath($env:WB2API_EXE)', '[IO.Path]::GetFullPath(\"$env:WB2API_EXE\")')
        [System.IO.File]::WriteAllText($fullCmd, $content)
    }
}

# ── 10. 同步 Python 运行环境依赖 ────────────────────────
Write-Host "[INFO] 正在同步 Python 依赖库..." -ForegroundColor Cyan
$uvExe = Join-Path $root '.tools\uv\uv.exe'
$reqFile = Join-Path $root 'server\requirements.txt'

if ((Test-Path $uvExe) -and (Test-Path $venvPython) -and (Test-Path $reqFile)) {
    & $uvExe pip install -r $reqFile --python $venvPython
}

Write-Host "==================================================" -ForegroundColor Green
Write-Host " [SUCCESS] WorkBuddy Manager 已成功安全更新至 $latestVer " -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Green
Write-Host "[INFO] 个人配置（.env、账号授权、数据库历史）完好无损。" -ForegroundColor White

$startNow = Read-Host "是否立即启动服务？(Y/n)"
if ($startNow -ne 'n' -and $startNow -ne 'N') {
    Write-Host "[INFO] 正在启动服务..." -ForegroundColor Cyan
    $startCmd = Join-Path $scriptDir 'start.cmd'
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "`"$startCmd`""
}
