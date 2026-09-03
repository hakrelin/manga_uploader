# 漫画发布器一键启动（Windows PowerShell）
# Python 策略：用本机任意 Python 装 uv，由 uv 建独立的 Python 3.12 虚拟环境（.venv），
# 依赖版本由 requirements.txt 控制，不使用系统 Python 运行程序。
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

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

function Fail($msg) {
    Write-Host $msg -ForegroundColor Red
    Read-Host "回车退出"
    exit 1
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

function Test-Deps {
    & $venvPy -c "import requests, yaml, PIL" 2>$null
    return ($LASTEXITCODE -eq 0)
}

# ---- 0) .venv 已就绪 → 直接启动 ----
if (Test-Path $venvPy) {
    if (Test-Deps) { Start-Main }
}

# ---- 1) 本机任意 Python（只用来装 uv / 驱动 uv） ----
$pyArr = $null
foreach ($cand in @(@("python"), @("py", "-3"))) {
    $cmd = Get-Command $cand[0] -ErrorAction SilentlyContinue
    if ($cmd) {
        $extra = @()
        if ($cand.Count -gt 1) { $extra = $cand[1..($cand.Count - 1)] }
        & $cand[0] @extra -c "print(1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $pyArr = @($cand[0]) + $extra; break }
    }
}
if (-not $pyArr) { Fail "[错误] 未找到 Python，请先安装任意 Python 3.8+（https://www.python.org）" }

function Uv {
    & $pyArr[0] $($pyArr | Select-Object -Skip 1) -m uv @args
}

# ---- 2) 装 uv（清华镜像 → 跳过 SSL 校验 → 官方源） ----
Write-Host "[初始化] 安装 uv（清华镜像）…"
& $pyArr[0] $($pyArr | Select-Object -Skip 1) -m pip install -i $PipMirror -q uv
if ($LASTEXITCODE -ne 0) {
    Write-Host "[提示] 重试（跳过 SSL 校验）…"
    & $pyArr[0] $($pyArr | Select-Object -Skip 1) -m pip install -i $PipMirror `
        --trusted-host pypi.tuna.tsinghua.edu.cn -q uv
}
if ($LASTEXITCODE -ne 0) {
    Write-Host "[提示] 改用官方源…"
    & $pyArr[0] $($pyArr | Select-Object -Skip 1) -m pip install -i $PipOfficial -q uv
}
if ($LASTEXITCODE -ne 0) {
    Fail "[错误] uv 安装失败，请把上面的输出发给开发者"
}

# ---- 3) uv 建独立 Python 3.12 + .venv（已存在但不可用则重建） ----
if ((Test-Path $venvPy) -and -not (Test-Deps)) {
    Write-Host "[提示] 现有 .venv 不可用，重建…"
    Remove-Item -Recurse -Force (Join-Path $PSScriptRoot ".venv")
}
if (-not (Test-Path $venvPy)) {
    Write-Host "[初始化] 准备 Python 3.12 虚拟环境（仅首次）…"
    Uv python install 3.12
    if ($LASTEXITCODE -ne 0) { Fail "[错误] Python 3.12 下载失败，请检查网络后重跑" }
    Uv venv --python 3.12 .venv
    if (-not (Test-Path $venvPy)) {
        Fail "[错误] .venv 创建失败，请把上面的输出发给开发者"
    }
}

# ---- 4) 依赖（版本由 requirements.txt 控制） ----
if (-not (Test-Deps)) {
    Write-Host "[初始化] 安装依赖（清华镜像）…"
    Uv pip install --python $venvPy -i $PipMirror -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[提示] 清华镜像拉取失败，改用官方源重试…"
        Uv pip install --python $venvPy -i $PipOfficial -r requirements.txt
    }
    if ($LASTEXITCODE -ne 0) {
        Fail "[错误] 依赖安装失败，请把上面的输出发给开发者"
    }
}

Start-Main
