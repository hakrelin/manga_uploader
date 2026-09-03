# 漫画发布器一键启动（Windows PowerShell）
# 首次运行会在程序目录自动创建 .venv 虚拟环境并安装依赖，不污染系统 Python。
# 用法：
#   .\start.ps1              # 本机启动（127.0.0.1，自动拉起浏览器）
#   .\start.ps1 -Lan         # 局域网可访问（0.0.0.0）
#   .\start.ps1 -Port 9000   # 指定端口
param(
    [switch]$Lan,
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

# 国内用户：pip 默认走清华镜像（离海外的用户可改成官方源）
$PipMirror = "https://pypi.tuna.tsinghua.edu.cn/simple"

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

# ---- 虚拟环境：默认装在程序目录（.venv） ----
if (-not (Test-Path $venvPy)) {
    # 找系统 Python（仅用于创建 venv）
    $pyExe = $null
    $pyArgs = @()
    foreach ($cand in @(@("python"), @("py", "-3"))) {
        $cmd = Get-Command $cand[0] -ErrorAction SilentlyContinue
        if ($cmd) {
            $pyExe = $cand[0]
            if ($cand.Count -gt 1) { $pyArgs = $cand[1..($cand.Count - 1)] }
            break
        }
    }
    if (-not $pyExe) {
        Write-Host "[错误] 未找到 Python，请先安装 Python 3.10+ 并加入 PATH" -ForegroundColor Red
        Read-Host "回车退出"
        exit 1
    }
    Write-Host "[初始化] 创建虚拟环境 .venv（仅首次）…"
    & $pyExe @pyArgs -m venv .venv
    if (-not (Test-Path $venvPy)) {
        Write-Host "[错误] venv 创建失败，请检查 python -m venv 是否可用" -ForegroundColor Red
        Read-Host "回车退出"
        exit 1
    }
}

# ---- 依赖（装进 .venv，缺才装；版本由 requirements.txt 控制） ----
& $venvPy -c "import requests, yaml, PIL" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[初始化] 安装依赖（清华镜像）…"
    & $venvPy -m pip install -i $PipMirror --timeout 60 -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[提示] 清华镜像拉取失败（网络/分流原因），改用官方源重试…"
        & $venvPy -m pip install --timeout 60 -r requirements.txt
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[错误] 依赖安装失败。" -ForegroundColor Red
        Write-Host "  常见原因：系统代理拦截了 pip。可尝试关闭代理后重跑，" -ForegroundColor Yellow
        Write-Host "  或手动安装：.venv\Scripts\python -m pip install -r requirements.txt" -ForegroundColor Yellow
        Read-Host "回车退出"
        exit 1
    }
}

# ---- 启动（--web 会自动拉起浏览器） ----
Write-Host "[启动] 漫画发布器 Web 前端…" -ForegroundColor Green
$argv = @("-m", "manga_uploader", "--web")
if ($Lan) { $argv += @("--host", "0.0.0.0") }
if ($Port -gt 0) { $argv += @("--port", "$Port") }
& $venvPy @argv
Read-Host "服务已退出，回车关闭窗口"
