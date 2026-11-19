# WorkBuddy Manager —— 跨机器通用自动化更新程序（Windows）
param([switch]$AllowUnverified)
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
        # 只识别官方命名的 .tar.gz 归档包（大小与临时后缀做粗筛）
        $candidates = Get-ChildItem -Path $d -Filter "workbuddy-manager-*.tar.gz" -Recurse -Depth 2 -ErrorAction SilentlyContinue | `
            Where-Object {
                $_.Length -gt 5000000 -and `
                -not (Test-Path "$($_.FullName).crdownload") -and `
                -not (Test-Path "$($_.FullName).tmp") -and `
                -not (Test-Path "$($_.FullName).part") -and `
                -not (Test-Path "$($_.FullName).partial") -and `
                -not (Test-Path "$($_.FullName).download")
            } | Sort-Object LastWriteTime -Descending
        
        foreach ($c in $candidates) {
            if ($targetVersion) {
                if ($c.Name -match [regex]::Escape($targetVersion)) { return $c }
            } else {
                return $c
            }
        }
    }
    return $null
}

# ── 2. 免 API 限制的版本检测（彻底杜绝 GitHub API 403 频率超限）──

# GitHub 镜像（issue #216）：国内机器直连 github.com / api.github.com 常超时。
# gh-proxy 一类服务的用法是「镜像基址 + 完整原始地址」。只在显式配置时启用；
# 走镜像时**不发送任何 Token**（镜像会把请求转给第三方）。
function Get-GitHubMirror {
    $raw = $env:WB_GITHUB_MIRROR
    if (-not $raw) {
        # 面板里配的镜像存在数据库里，这个独立脚本读不到；.env 是它能拿到的口径。
        $envPath = Join-Path $root '.env'
        if (Test-Path $envPath) {
            foreach ($line in Get-Content $envPath) {
                if ($line -match '^\s*WB_GITHUB_MIRROR\s*=\s*(.+)$') {
                    $raw = $Matches[1].Trim().Trim('"').Trim("'")
                    break
                }
            }
        }
    }
    if (-not $raw) { return '' }
    $raw = $raw.Trim()
    if ($raw -notmatch '^https?://') { $raw = 'https://' + $raw }
    if ($raw -notmatch '^https?://[^/?#]+') { return '' }
    # 拼地址的规则是「基址 + 完整原始地址」：带查询串/片段必然拼出错误页，直接不用
    if ($raw -match '[?#]') { return '' }
    return $raw.TrimEnd('/') + '/'
}

function New-MirrorUrl {
    param([string]$Url, [string]$Mirror)
    if (-not $Mirror) { return $Url }
    if ($Url -notmatch '^https://(github\.com|api\.github\.com|codeload\.github\.com|raw\.githubusercontent\.com|objects\.githubusercontent\.com)/') {
        return $Url
    }
    return "$Mirror$Url"
}

$mirrorBase = Get-GitHubMirror

function Get-LatestGitHubTag {
    $proxyList = [System.Collections.Generic.List[string]]::new()
    if ($env:HTTPS_PROXY) { $proxyList.Add($env:HTTPS_PROXY) }
    if ($env:ALL_PROXY -and -not $proxyList.Contains($env:ALL_PROXY)) { $proxyList.Add($env:ALL_PROXY) }
    foreach ($dp in @('http://127.0.0.1:7897', 'http://127.0.0.1:7890', 'http://127.0.0.1:10809')) {
        if (-not $proxyList.Contains($dp)) { $proxyList.Add($dp) }
    }
    $proxyList.Add('')

    $curlExe = (Get-Command curl.exe -ErrorAction SilentlyContinue).Source

    # 方式 0: 经镜像取 Releases API（镜像可代理 api.github.com，且不受网页正文限制）
    if ($mirrorBase) {
        try {
            $apiParams = @{
                Uri = "$mirrorBase" + 'https://api.github.com/repos/ithtelab/workbuddy-manager/releases/latest'
                Headers = @{"User-Agent"="workbuddy-updater"}
                TimeoutSec = 15
            }
            $res = Invoke-RestMethod @apiParams
            if ($res.tag_name) {
                $digest = ''
                foreach ($a in @($res.assets)) {
                    if ($a.name -like '*.tar.gz') { $digest = "$($a.digest)" }
                }
                return @{ Tag = $res.tag_name; Proxy = ''; Digest = $digest }
            }
        } catch {
            Write-Host "[WARN] 镜像版本检测失败，回退直连：$($_.Exception.Message)" -ForegroundColor Yellow
        }
    }

    # 方式 1: 直接向 github.com 发起 HEAD 请求取重定向 Location（无 60次/小时 API 限制）
    # 注意：gh-proxy 一类镜像只放行资源下载，对网页正文返回 403，所以这条**不走镜像**。
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
            if ($res.tag_name) {
                # 顺手取发布包摘要（下载后核对用；Releases 页面上是同一个值）
                $digest = ''
                foreach ($a in @($res.assets)) {
                    if ($a.name -like '*.tar.gz') { $digest = "$($a.digest)" }
                }
                return @{ Tag = $res.tag_name; Proxy = $p; Digest = $digest }
            }
        } catch {}
    }

    return $null
}

# ── 3. 发布包完整性校验（sha256）────────────────────────────
function Test-PackageDigest {
    param(
        [string]$archivePath,
        # 发布页给的摘要，形如 "sha256:abc…"；只有走 GitHub API 那条路才拿得到，
        # 免 API 的重定向路径拿不到 —— 那时只打印本地摘要供人工比对。
        [string]$expectedDigest
    )
    if (-not (Test-Path $archivePath)) { return $false }

    $actual = (Get-FileHash -Path $archivePath -Algorithm SHA256).Hash.ToLower()

    if ($expectedDigest -and $expectedDigest.StartsWith('sha256:')) {
        $want = $expectedDigest.Substring(7).Trim().ToLower()
        if ($actual -eq $want) {
            Write-Host "[SUCCESS] 发布包校验通过（sha256 与发布页一致）：$($actual.Substring(0,16))…" -ForegroundColor Green
            return $true
        }
        Write-Host "[ERROR] ❌ 发布包 sha256 与发布页不一致，已拒绝安装！" -ForegroundColor Red
        Write-Host "[ERROR]   发布页：$want" -ForegroundColor Red
        Write-Host "[ERROR]   下载到：$actual" -ForegroundColor Red
        Write-Host "[ERROR]   多为下载不完整（重下一次即可）；始终不一致就别装了。" -ForegroundColor Red
        return $false
    }

    if ($AllowUnverified) {
        Write-Host "[WARN] 未取得发布页摘要，按 -AllowUnverified 继续（无法证明包未被替换）。" -ForegroundColor Yellow
        Write-Host "[WARN] 本地 sha256：$actual" -ForegroundColor Yellow
        return $true
    }
    Write-Host "[ERROR] 未取得发布页摘要，无法校验安装包，已拒绝安装。" -ForegroundColor Red
    Write-Host "[INFO] 本地 sha256：$actual" -ForegroundColor Cyan
    Write-Host "[INFO] 请确认网络能访问 GitHub API 后重试；只在确认包来自官方发布页时才加 -AllowUnverified。" -ForegroundColor DarkGray
    return $false
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
    if ($existingPkg.Name -match 'workbuddy-manager-(v?\d+\.\d+\.\d+[\w\.\-]*)\.tar\.gz') {
        $latestVer = $Matches[1]
        Write-Host "[INFO] 已根据本地离线安装包识别版本: $latestVer" -ForegroundColor Yellow
    } else {
        $latestVer = "离线安装包"
    }
}

# 若网络未连通且没有任何离线包，提示前往面板点「一键更新」或手动下载
if (-not $latestVer -and -not $existingPkg) {
    Write-Host "[WARN] 无法连接 GitHub 且未在常用目录中找到离线安装包。" -ForegroundColor Yellow
    Write-Host "[INFO] 已检索目录: $($searchDirs -join ' | ')" -ForegroundColor DarkGray
    Write-Host "[TIP] 提示：您可登录 Web 管理面板，在「系统设置 → 一键更新」中直接更新。" -ForegroundColor Cyan
    $openBrowser = Read-Host "是否在浏览器中打开 GitHub Releases 页面手动下载？(Y/n)"
    if ($openBrowser -ne 'n' -and $openBrowser -ne 'N') {
        Start-Process "https://github.com/ithtelab/workbuddy-manager/releases"
        Write-Host "[WAIT] 正在监听下载目录（下载完 tar.gz 后会自动识别并更新）..." -ForegroundColor Cyan
        
        $startTime = [DateTime]::Now
        while ($true) {
            Start-Sleep -Seconds 2
            $detected = Find-LocalPackage -searchDirectories $searchDirs -targetVersion $null
            if ($detected -and ($detected.LastWriteTime -gt $startTime.AddMinutes(-5))) {
                $existingPkg = $detected
                if ($detected.Name -match 'workbuddy-manager-(v?\d+\.\d+\.\d+[\w\.\-]*)\.tar\.gz') {
                    $latestVer = $Matches[1]
                } else {
                    $latestVer = "离线安装包"
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

# ── 5. 安装包捕获与摘要校验 ───────────────────────────────
$tempTar = Join-Path $root "update_temp.tar.gz"
if (Test-Path $tempTar) { Remove-Item $tempTar -Force }

$targetPkg = Find-LocalPackage -searchDirectories $searchDirs -targetVersion $latestVer
# 走 GitHub API 时顺手拿到的发布页摘要（形如 sha256:…）；免 API 路径为空
$expectedDigest = if ($tagInfo -and $tagInfo.Digest) { "$($tagInfo.Digest)" } else { '' }

# 免 API 的版本检测（HEAD/重定向）拿不到摘要，但**摘要校验不能因此跳过**：
# 再向 Releases API 要一次 asset.digest。拿不到就由 Test-PackageDigest 拒绝安装
# （除非显式 -AllowUnverified）——「本机下载目录里恰好有个同名包」是最现实的
# 投毒路径，不能默认放行。
if (-not $expectedDigest -and $latestVer) {
    try {
        # 配了镜像时先经镜像取摘要（api.github.com 可被镜像代理）
        $apiUrl = "https://api.github.com/repos/ithtelab/workbuddy-manager/releases/tags/$latestVer"
        $apiTargets = @()
        if ($mirrorBase) { $apiTargets += (New-MirrorUrl -Url $apiUrl -Mirror $mirrorBase) }
        $apiTargets += $apiUrl
        $release = $null
        foreach ($target in $apiTargets) {
            try {
                $release = Invoke-RestMethod -Uri $target -TimeoutSec 15 -Headers @{
                    'User-Agent' = 'workbuddy-updater'
                    'Accept' = 'application/vnd.github+json'
                }
                break
            } catch {}
        }
        if (-not $release) { throw '所有地址都取不到 Releases API' }
        foreach ($asset in $release.assets) {
            if ($asset.name -like 'workbuddy-manager-*.tar.gz' -and $asset.digest) {
                $expectedDigest = [string]$asset.digest
                Write-Host "[INFO] 已取得发布页摘要：$expectedDigest" -ForegroundColor DarkGray
                break
            }
        }
    } catch {
        Write-Host "[WARN] 无法从 GitHub API 取得摘要：$($_.Exception.Message)" -ForegroundColor Yellow
    }
}

if ($targetPkg) {
    Write-Host "[INFO] 已在目录中识别到本地安装包: $($targetPkg.FullName)" -ForegroundColor Green
    Copy-Item -Path $targetPkg.FullName -Destination $tempTar -Force
} else {
    $tarUrl = "https://github.com/ithtelab/workbuddy-manager/releases/download/$latestVer/workbuddy-manager-$latestVer.tar.gz"
    $curlExe = (Get-Command curl.exe -ErrorAction SilentlyContinue).Source
    $downloadSuccess = $false

    # 尝试方式 1: 使用 curl.exe 命令行静默下载
    # 配了镜像时先走镜像，失败再直连 —— 镜像自己也会抖，不能因为它挂了就没法更新。
    if ($curlExe) {
        $downloadTargets = @()
        if ($mirrorBase) { $downloadTargets += (New-MirrorUrl -Url $tarUrl -Mirror $mirrorBase) }
        $downloadTargets += $tarUrl
        foreach ($dlUrl in $downloadTargets) {
            if ($mirrorBase -and $dlUrl -ne $tarUrl) {
                Write-Host "[INFO] 正在尝试经镜像下载官方发布包 ($latestVer)..." -ForegroundColor Cyan
            } else {
                Write-Host "[INFO] 正在尝试后台下载官方发布包 ($latestVer)..." -ForegroundColor Cyan
            }
            $cArgsBase = @('-L', '--fail', '--connect-timeout', '10')
            if ($detectedProxy) { $cArgsBase += @('-x', $detectedProxy) }

            & $curlExe ($cArgsBase + @('-o', $tempTar, $dlUrl))

            if ($LASTEXITCODE -eq 0 -and (Test-Path $tempTar) -and ((Get-Item $tempTar).Length -gt 5000000)) {
                $downloadSuccess = $true
                Write-Host "[SUCCESS] 发布包下载完成。" -ForegroundColor Green
                break
            }
            Remove-Item $tempTar -Force -ErrorAction SilentlyContinue
            if ($dlUrl -ne $tarUrl) {
                Write-Host "[WARN] 镜像下载失败，回退直连..." -ForegroundColor Yellow
            }
        }
    }

    # 尝试方式 2: 调起浏览器下载并自动监听感应
    if (-not $downloadSuccess) {
        Write-Host "[WARN] 命令行下载受阻，自动为您在浏览器中打开官方发布包直链..." -ForegroundColor Yellow
        Start-Process $tarUrl

        Write-Host "[WAIT] 正在实时监听下载目录（下载完 tar.gz 后会自动识别并继续）..." -ForegroundColor Cyan

        $watchTimeoutSeconds = 300
        $startTime = [DateTime]::Now

        while (-not (Test-Path $tempTar)) {
            Start-Sleep -Seconds 2

            if (([DateTime]::Now - $startTime).TotalSeconds -gt $watchTimeoutSeconds) {
                Write-Host "[ERROR] 等待下载超时，操作已取消。" -ForegroundColor Red
                exit 1
            }

            $detected = Find-LocalPackage -searchDirectories $searchDirs -targetVersion $latestVer
            if ($detected -and ($detected.LastWriteTime -gt $startTime.AddMinutes(-5))) {
                try {
                    $stream = [System.IO.File]::Open($detected.FullName, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::None)
                    $stream.Dispose()
                    Write-Host "[SUCCESS] 检测到下载完成: $($detected.FullName)" -ForegroundColor Green
                    Copy-Item -Path $detected.FullName -Destination $tempTar -Force
                    break
                } catch {}
            }
        }
    }
}

# 校验下载到的包：sha256 与发布页一致才继续（拿不到发布页摘要时只打印本地摘要）
if (-not (Test-PackageDigest -archivePath $tempTar -expectedDigest $expectedDigest)) {
    Remove-Item $tempTar -Force -ErrorAction SilentlyContinue
    Write-Host "[ERROR] 发布包校验未通过，更新已被强制终止。未做任何文件变动。" -ForegroundColor Red
    exit 1
}


# ── 6. 停服与覆盖确认（关键交互节点）───────────────────────
Write-Host ""
Write-Host "[CONFIRM] 发布包校验通过（sha256 见上方日志）！" -ForegroundColor Green
Write-Host "[CONFIRM] 即将停止当前服务，清空 web/out 并覆盖更新核心文件至 $latestVer。" -ForegroundColor Yellow
Write-Host "[CONFIRM] 更新期间 Web 管理面板服务将短暂离线。" -ForegroundColor Yellow
$confirmProceed = Read-Host "确认继续执行更新？(Y/n)"
if ($confirmProceed -eq 'n' -or $confirmProceed -eq 'N') {
    Remove-Item $tempTar -Force -ErrorAction SilentlyContinue
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

# ── 9. 安全解压到 staging，校验后再替换（失败不动现有文件）─────────
Write-Host "[INFO] 正在安全解压并校验发布包..." -ForegroundColor Cyan

# Python 解释器：预置运行时（.runtime\python）优先，uv 虚拟环境（.venv）兜底
. (Join-Path $scriptDir 'python-env.ps1')
$pyExe = Get-WbPython -Root $root
if (-not $pyExe) {
    Write-Host '[ERROR] 未找到可用的 Python 环境（.runtime\python 或 .venv），无法解压更新包；本次更新已中止。' -ForegroundColor Red
    exit 1
}

$stage = Join-Path $root ('.tools\stage-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage -Force | Out-Null
$extractScript = Join-Path $root '.tools\extract_release.py'
New-Item -ItemType Directory -Path (Join-Path $root '.tools') -Force | Out-Null

# 解压器只做一件事：把**白名单内**的成员写到 staging。
# 安全要点（缺一不可）：
#   · 拒绝 .. / 绝对路径 / 盘符 —— 以前的字符串前缀判断会被 `server/../.env` 绕过；
#   · 拒绝符号链接/硬链接/设备节点 —— 它们能把写入引到安装目录之外；
#   · 解析后再次确认落在 staging 之内（双保险）。
$pyContent = @'
import tarfile, sys, os
from pathlib import Path

archive_path = sys.argv[1]
root_dir = Path(sys.argv[2]).resolve()

def inside(target, base):
    try:
        target.relative_to(base)
        return True
    except ValueError:
        return False

def fail(message):
    print("[extract] " + message, flush=True)
    raise SystemExit(2)

ALLOWED_PREFIX = ("web/out/", "server/", "deploy/", "docs/")
ALLOWED_FILE = (".version", "CHANGELOG.md", "README.md", "README.en.md")

with tarfile.open(archive_path, "r:gz") as tf:
    for member in tf.getmembers():
        name = member.name.replace("\\", "/")
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if not parts:
            continue
        if parts[0].startswith("workbuddy-manager-"):
            parts = parts[1:]
        if not parts:
            continue
        if any(p == ".." for p in parts) or ":" in parts[0]:
            fail("unsafe member: " + member.name)
        rel_path = "/".join(parts)
        if not (rel_path in ALLOWED_FILE or rel_path.startswith(ALLOWED_PREFIX)):
            continue
        target = (root_dir / rel_path).resolve()
        if not inside(target, root_dir):
            fail("path escapes staging: " + member.name)
        if member.issym() or member.islnk() or member.isdev():
            fail("unsupported member type: " + member.name)
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
        elif member.isfile():
            target.parent.mkdir(parents=True, exist_ok=True)
            with tf.extractfile(member) as source, open(target, "wb") as dest:
                dest.write(source.read())
'@

[System.IO.File]::WriteAllText($extractScript, $pyContent, [System.Text.Encoding]::UTF8)

$extractExit = 0
try {
    & $pyExe $extractScript $tempTar $stage
    $extractExit = $LASTEXITCODE
} catch {
    $extractExit = 1
}
Remove-Item $extractScript -Force -ErrorAction SilentlyContinue

function Restore-Scripts {
    foreach ($s in $scriptsToProtect) {
        $bak = Join-Path $backupDir $s
        if (Test-Path $bak) { Copy-Item $bak (Join-Path $scriptDir $s) -Force }
    }
    Remove-Item $backupDir -Recurse -Force -ErrorAction SilentlyContinue
}

function Fail-Update([string]$message) {
    Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
    Restore-Scripts
    Write-Host "[ERROR] $message" -ForegroundColor Red
    Write-Host "[ERROR] 本次更新已中止，现有文件未做替换。" -ForegroundColor Red
    exit 1
}

if ($extractExit -ne 0) {
    Fail-Update "解压失败（退出码 $extractExit）—— 包可能损坏或被篡改"
}
# 必需产物校验：缺了任何一个都说明包不完整，绝不能进入替换阶段
foreach ($need in @('server\main.py', 'web\out\index.html')) {
    if (-not (Test-Path (Join-Path $stage $need))) {
        Fail-Update "发布包缺少 $need，拒绝替换"
    }
}

# 目录替换：先改名为 .old，再把新目录换上去；失败就把旧的换回来。
function Replace-Dir([string]$incoming, [string]$target) {
    $old = "$target.old"
    if (Test-Path $old) { Remove-Item $old -Recurse -Force -ErrorAction SilentlyContinue }
    $hadOld = Test-Path $target
    if ($hadOld) { Move-Item -Path $target -Destination $old -Force }
    try {
        Move-Item -Path $incoming -Destination $target -Force
    } catch {
        if ($hadOld -and (Test-Path $old)) { Move-Item -Path $old -Destination $target -Force }
        throw
    }
    if (Test-Path $old) { Remove-Item $old -Recurse -Force -ErrorAction SilentlyContinue }
}

Write-Host "[INFO] 正在替换 server/ 与 web/out/..." -ForegroundColor Cyan
try {
    Replace-Dir (Join-Path $stage 'server') (Join-Path $root 'server')
    if (Test-Path (Join-Path $stage 'web\out')) {
        Replace-Dir (Join-Path $stage 'web\out') (Join-Path $root 'web\out')
    }
} catch {
    Fail-Update "替换文件失败：$($_.Exception.Message)"
}

# 其余白名单项按整体覆盖（deploy/docs 是脚本与文档；失败不影响服务启动）
foreach ($item in @('deploy', 'docs', 'CHANGELOG.md', 'README.md', 'README.en.md')) {
    $src = Join-Path $stage $item
    if (-not (Test-Path $src)) { continue }
    $dst = Join-Path $root $item
    if (Test-Path $dst) { Remove-Item $dst -Recurse -Force -ErrorAction SilentlyContinue }
    Copy-Item -Path $src -Destination $dst -Recurse -Force
}
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue

# .version 只在全部替换成功后写（以前失败也写，导致「代码是旧的、版本号是新的」）
[System.IO.File]::WriteAllText((Join-Path $root '.version'), $latestVer, (New-Object System.Text.UTF8Encoding($false)))

# 彻底清理临时安装包，绝不遗留磁盘垃圾
Remove-Item $tempTar -Force -ErrorAction SilentlyContinue
Write-Host "[INFO] 临时安装包已自动彻底清理删除。" -ForegroundColor DarkGray

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

if ((Test-Path $reqFile) -and $pyExe) {
    if (Test-Path $uvExe) { & $uvExe pip install -r $reqFile --python $pyExe }
    else { & $pyExe -m pip install -r $reqFile }
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
