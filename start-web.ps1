# 漫画发布器 —— 浏览器前端（Web UI）一键启动（Windows PowerShell）
# 环境（绿色 Python 3.12 + .venv）由 _common.ps1 准备。
# 用法：
#   .\start-web.ps1             # 本机启动（127.0.0.1，自动拉起浏览器）
#   .\start-web.ps1 -Lan        # 局域网可访问（0.0.0.0）
#   .\start-web.ps1 -Port 9000  # 指定端口
param(
    [switch]$Lan,
    [int]$Port = 0
)

. (Join-Path $PSScriptRoot "_common.ps1")

$venvPy = Ensure-PyEnv

Write-Host "[启动] 漫画发布器 Web 前端…" -ForegroundColor Green
$argv = @("-m", "manga_uploader", "--web")
if ($Lan) { $argv += @("--host", "0.0.0.0") }
if ($Port -gt 0) { $argv += @("--port", "$Port") }
& $venvPy @argv
Read-Host "服务已退出，回车关闭窗口"
