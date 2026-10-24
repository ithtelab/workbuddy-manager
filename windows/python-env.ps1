# WorkBuddy Manager —— Python 解释器解析（供其他脚本 dot-source 使用）
# 预置运行时（.runtime\python，随安装包分发）优先；uv 建的虚拟环境（.venv）兜底。

# 禁止用户级 site-packages 覆盖预置运行时的依赖（issue #268）：
# 用户目录里旧版 typing_extensions 等包会排在 .runtime 前面，首次运行自检
# 直接 ImportError。所有 dot-source 本文件的启动/更新脚本都继承这个设置。
$env:PYTHONNOUSERSITE = '1'

function Get-WbPython {
    param([Parameter(Mandatory = $true)][string]$Root)
    foreach ($rel in @('.runtime\python\python.exe', '.venv\Scripts\python.exe')) {
        $path = Join-Path $Root $rel
        if (Test-Path $path) { return $path }
    }
    return $null
}
