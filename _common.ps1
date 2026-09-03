# 共享：漫画发布器环境准备（绿色 Python 3.12 + .venv + 依赖）
# 由 start-web.ps1 / start-gui.ps1 dot-source 后调用 Ensure-PyEnv。

$ErrorActionPreference = "Stop"

# 国内用户：pip 默认走清华镜像（离海外的用户可改）
$PipMirror = "https://pypi.tuna.tsinghua.edu.cn/simple"
$PipOfficial = "https://pypi.org/simple"

# 绿色 Python 3.12（python-build-standalone，npmmirror 镜像）
$PyVer = "3.12.14"
$PyTag = "20260901"

$ProjRoot = $PSScriptRoot
$toolsDir = Join-Path $ProjRoot ".tools"
$greenPy = Join-Path $toolsDir "python\python.exe"
$venvPy = Join-Path $ProjRoot ".venv\Scripts\python.exe"

function Fail-Custom($msg) {
    Write-Host $msg -ForegroundColor Red
    Read-Host "回车退出"
    exit 1
}

function Test-Env {
    & $venvPy -c "import sys, requests, yaml, PIL; assert sys.version_info[:2] == (3, 12)" 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Ensure-PyEnv {
    # 1) .venv 已就绪 → 直接用
    if (Test-Path $venvPy) {
        if (Test-Env) { return $venvPy }
        Write-Host "[提示] .venv 不可用（版本不对或依赖缺失），重建…"
        Remove-Item -Recurse -Force (Join-Path $ProjRoot ".venv")
    }

    # 2) 绿色 Python 3.12（仅在缺失时下载一次，约 110MB）
    if (-not (Test-Path $greenPy)) {
        Write-Host "[初始化] 下载绿色 Python $PyVer（npmmirror 国内镜像，仅首次，约 110MB）…"
        New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
        $arc = Join-Path $env:TEMP "py312.tar.gz"
        $url = "https://registry.npmmirror.com/-/binary/python-build-standalone/$PyTag/" +
            "cpython-$PyVer%2B$PyTag-x86_64-pc-windows-msvc-install_only.tar.gz"
        & curl.exe -L --connect-timeout 20 -o $arc $url
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $arc) -or (Get-Item $arc).Length -lt 10MB) {
            Fail-Custom "[错误] Python 下载失败，请检查网络后重跑"
        }
        & tar.exe -xzf $arc -C $toolsDir
        Remove-Item -Force $arc
        if (-not (Test-Path $greenPy)) {
            Fail-Custom "[错误] 解压后找不到 python.exe（tar 解压失败？）"
        }
    }

    # 3) 建 .venv
    Write-Host "[初始化] 创建虚拟环境 .venv（仅首次）…"
    & $greenPy -m venv (Join-Path $ProjRoot ".venv")
    if (-not (Test-Path $venvPy)) {
        Fail-Custom "[错误] .venv 创建失败，请把上面的输出发给开发者"
    }

    # 4) 依赖（版本由 requirements.txt 控制）
    Write-Host "[初始化] 安装依赖（清华镜像）…"
    & $venvPy -m pip install -i $PipMirror --timeout 60 -r (Join-Path $ProjRoot "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[提示] 清华镜像拉取失败，改用官方源重试…"
        & $venvPy -m pip install -i $PipOfficial --timeout 60 -r (Join-Path $ProjRoot "requirements.txt")
    }
    if (-not (Test-Env)) {
        Fail-Custom "[错误] 依赖安装失败，请把上面的输出发给开发者"
    }
    return $venvPy
}
