# 诊断：枚举指定进程主窗口的 UIAutomation 控件树
param([string]$ProcessName = 'WorkBuddyManagerSetup')
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$proc = Get-Process -Name $ProcessName -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $proc) { Write-Host '进程不存在'; exit 1 }
$proc.Refresh()
if ($proc.MainWindowHandle -eq 0) { Write-Host '无主窗口'; exit 1 }

$element = [System.Windows.Automation.AutomationElement]::FromHandle($proc.MainWindowHandle)
Write-Host ('窗口: ' + $element.Current.Name + ' | ' + $element.Current.ClassName + ' | ' + $element.Current.ControlType.ProgrammaticName)

$all = $element.FindAll([System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.Condition]::TrueCondition)
Write-Host ('后代元素数: ' + $all.Count)
foreach ($item in $all) {
    Write-Host ('  ' + $item.Current.ControlType.ProgrammaticName + ' | name="' + $item.Current.Name + '" | class=' + $item.Current.ClassName)
}
