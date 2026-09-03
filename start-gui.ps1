# 漫画发布器 —— 图形界面（tkinter GUI）一键启动（Windows PowerShell）
# 与 Web 前端共享同一个本地 .venv（绿色 Python 3.12 + 依赖，见 _common.ps1）。
param()

. (Join-Path $PSScriptRoot "_common.ps1")

$venvPy = Ensure-PyEnv

Write-Host "[启动] 漫画发布器 GUI…" -ForegroundColor Green
& $venvPy -m manga_uploader --gui
Read-Host "程序已退出，回车关闭窗口"
