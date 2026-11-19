# 给缺失 BOM 的 PowerShell 脚本补上 UTF-8 BOM。
# Windows PowerShell 5.1 读取无 BOM 的 .ps1 时按 ANSI（中文系统为 GBK）解码，
# 含中文的脚本会直接解析失败 —— 所以仓库里的 .ps1 统一带 BOM。
# 用法：powershell -ExecutionPolicy Bypass -File dev\add-ps1-bom.ps1 [目录1 目录2 ...]
# 不带参数时默认处理 windows\ 与 dev\。
param(
    [string[]]$Paths = @('windows', 'dev')
)

$ErrorActionPreference = 'Stop'
$fixed = 0
$checked = 0

foreach ($root in $Paths) {
    if (-not (Test-Path $root)) { continue }
    foreach ($file in (Get-ChildItem $root -Recurse -Filter '*.ps1' -File)) {
        $checked++
        $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
        if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) { continue }
        $content = [System.IO.File]::ReadAllText($file.FullName, [System.Text.Encoding]::UTF8)
        [System.IO.File]::WriteAllText($file.FullName, $content, (New-Object System.Text.UTF8Encoding($true)))
        Write-Host ('BOM 已补：' + $file.FullName)
        $fixed++
    }
}

Write-Host ('检查 ' + $checked + ' 个脚本，补 BOM ' + $fixed + ' 个。')
