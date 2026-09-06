# 漫画发布器 —— 本地覆盖更新（保留 config.yaml / .venv / .tools / output）
# 更新前请先关闭正在运行的程序窗口；下载失败可加参数：
#   .\update.ps1 -Url "https://镜像/.../main.zip"
param(
    [switch]$Check,
    [string]$Repo = "",
    [string]$Branch = "",
    [string]$Url = "",
    [switch]$ForceZip,
    [switch]$NoDeps
)

. (Join-Path $PSScriptRoot "_common.ps1")
Set-Location -LiteralPath $ProjRoot

$venvPy = Ensure-PyEnv

Write-Host "[更新] 漫画发布器本地覆盖更新…" -ForegroundColor Cyan
$argv = @("update.py")
if ($Check) { $argv += @("--check") }
if ($Repo) { $argv += @("--repo", $Repo) }
if ($Branch) { $argv += @("--branch", $Branch) }
if ($Url) { $argv += @("--url", $Url) }
if ($ForceZip) { $argv += @("--force-zip") }
if ($NoDeps) { $argv += @("--no-deps") }

& $venvPy @argv
if ($LASTEXITCODE -ne 0) {
    Write-Host "[更新] 更新未完成，请把上面的提示发给开发者。" -ForegroundColor Red
}
Read-Host "按回车关闭窗口"
