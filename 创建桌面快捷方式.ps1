# 一键在桌面生成「化学文献库」快捷方式（带图标）。
# 指向 .venv 里的 pythonw.exe，用 pythonw 静默启动桌面窗口，避免闪出命令行。
$ErrorActionPreference = 'Stop'

$root = (Get-Item -LiteralPath $PSScriptRoot).FullName
$venvPython = Join-Path $root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "找不到运行环境：$venvPython"
}

$icon = Join-Path $root 'app\static\icon.ico'
if (-not (Test-Path -LiteralPath $icon)) {
    $icon = $venvPython
}

$desktop = [Environment]::GetFolderPath('Desktop')
$lnkPath = Join-Path $desktop '化学文献库.lnk'

$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath = $venvPython
$lnk.Arguments = '-m app.desktop'
$lnk.WorkingDirectory = $root
$lnk.WindowStyle = 1
$lnk.IconLocation = "$icon,0"
$lnk.Description = '化学文献库：文献收录 · 知识卡片 · 检索 · 导出PPT'
$lnk.Save()

Write-Output "已生成桌面快捷方式：$lnkPath"
