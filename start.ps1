# 漫画发布器一键启动（Windows PowerShell）
# Python 策略：从国内镜像（npmmirror）用 curl 下载"绿色版"独立 Python 3.12 到 .tools\python\，
# 用它建 .venv，依赖用 .venv 的 pip 从清华源装（版本由 requirements.txt 控制）。
# 全程不碰系统 Python / pip 配置 / 代理设置。
# 用法：.\start.ps1 [-Lan] [-Port 9000]
param(
    [switch]$Lan,
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

# 国内用户：pip 默认走清华镜像（离海外的用户可改成官方源）
$PipMirror = "https://pypi.tuna.tsinghua.edu.cn/simple"
$PipOfficial = "https://pypi.org/simple"

# 绿色 Python 3.12（python-build-standalone，npmmirror 镜像）
$PyVer = "3.12.14"
$PyTag = "20260901"

$toolsDir = Join-Path $PSScriptRoot ".tools"
$greenPy = Join-Path $toolsDir "python\python.exe"
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

function Fail($msg) {
    Write-Host $msg -ForegroundColor Red
    Read-Host "回车退出"
    exit 1
}

# .venv 是否可用：Python 3.12 且依赖齐全
function Test-Env {
    & $venvPy -c "import sys, requests, yaml, PIL; assert sys.version_info[:2] == (3, 12)" 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Start-Main {
    Write-Host "[启动] 漫画发布器 Web 前端…" -ForegroundColor Green
    $argv = @("-m", "manga_uploader", "--web")
    if ($Lan) { $argv += @("--host", "0.0.0.0") }
    if ($Port -gt 0) { $argv += @("--port", "$Port") }
    & $venvPy @argv
    Read-Host "服务已退出，回车关闭窗口"
    exit 0
}

# ---- 0) .venv 已就绪 → 直接启动 ----
if (Test-Path $venvPy) {
    if (Test-Env) { Start-Main }
}

# ---- 1) 绿色 Python 3.12（仅在缺失时下载一次，约 110MB） ----
if (-not (Test-Path $greenPy)) {
    Write-Host "[初始化] 下载绿色 Python $PyVer（npmmirror 国内镜像，仅首次，约 110MB）…"
    New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
    $arc = Join-Path $env:TEMP "py312.tar.gz"
    $url = "https://registry.npmmirror.com/-/binary/python-build-standalone/$PyTag/" +
        "cpython-$PyVer%2B$PyTag-x86_64-pc-windows-msvc-install_only.tar.gz"
    & curl.exe -L --connect-timeout 20 -o $arc $url
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $arc) -or (Get-Item $arc).Length -lt 10MB) {
        Fail "[错误] Python 下载失败，请检查网络后重跑"
    }
    & tar.exe -xzf $arc -C $toolsDir
    Remove-Item -Force $arc
    if (-not (Test-Path $greenPy)) {
        Fail "[错误] 解压后找不到 python.exe（tar 解压失败？）"
    }
}

# ---- 2) 建 / 重建 .venv（固定 Python 3.12） ----
if (-not (Test-Path $venvPy)) {
    Write-Host "[初始化] 创建虚拟环境 .venv（仅首次）…"
    & $greenPy -m venv (Join-Path $PSScriptRoot ".venv")
} elseif (-not (Test-Env)) {
    Write-Host "[提示] .venv 不可用（版本不对或依赖缺失），重建…"
    Remove-Item -Recurse -Force (Join-Path $PSScriptRoot ".venv")
    & $greenPy -m venv (Join-Path $PSScriptRoot ".venv")
}
if (-not (Test-Path $venvPy)) {
    Fail "[错误] .venv 创建失败，请把上面的输出发给开发者"
}

# ---- 3) 依赖（装进 .venv，版本由 requirements.txt 控制） ----
if (-not (Test-Env)) {
    Write-Host "[初始化] 安装依赖（清华镜像）…"
    & $venvPy -m pip install -i $PipMirror --timeout 60 -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[提示] 清华镜像拉取失败，改用官方源重试…"
        & $venvPy -m pip install -i $PipOfficial --timeout 60 -r requirements.txt
    }
    if ($LASTEXITCODE -ne 0) {
        Fail "[错误] 依赖安装失败，请把上面的输出发给开发者"
    }
}

Start-Main
