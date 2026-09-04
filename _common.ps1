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
    # 用 cmd 重定向 stdout/stderr：避免 $ErrorActionPreference=Stop 下依赖缺失的
    # traceback（stderr）被当作终止性错误，导致“首次安装依赖”流程直接崩溃
    & cmd.exe /c "`"$venvPy`" -c ""import sys, requests, yaml, PIL; assert sys.version_info[:2] == (3, 12)"" >nul 2>&1"
    return ($LASTEXITCODE -eq 0)
}

function Test-VenvLocal {
    # .venv 是否是在本机/本目录创建的：venv 会记录创建时 Python 的绝对路径，
    # 直接把别人的 .venv 拷过来时该路径不存在，必须重建
    $cfg = Join-Path $ProjRoot ".venv\pyvenv.cfg"
    if (-not (Test-Path $cfg)) { return $true }
    $homeLine = Select-String -Path $cfg -Pattern '^home\s*=\s*(.+)$' | Select-Object -First 1
    if ($homeLine -and $homeLine.Matches.Count -gt 0) {
        $homePath = $homeLine.Matches[0].Groups[1].Value.Trim()
        if ($homePath -and -not (Test-Path $homePath)) { return $false }
    }
    return $true
}

# 读取 Windows 系统代理（开了代理时 curl 直连镜像会失败，需要显式走代理）
function Get-SystemProxy {
    try {
        $reg = Get-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        if ($reg.ProxyEnable -eq 1 -and $reg.ProxyServer) {
            return [string]$reg.ProxyServer
        }
    } catch { }
    if ($env:HTTP_PROXY) { return $env:HTTP_PROXY }
    if ($env:HTTPS_PROXY) { return $env:HTTPS_PROXY }
    return ""
}

function Ensure-PyEnv {
    # 1) .venv 已就绪 → 直接用
    if (Test-Path $venvPy) {
        if (Test-Env) { return $venvPy }
        if (-not (Test-VenvLocal)) {
            Write-Host "[提示] 检测到 .venv 来自其他机器（Python 路径在本机不存在），自动重建（首次约需下载 110MB 绿色 Python，只此一次）…" -ForegroundColor Yellow
        } else {
            Write-Host "[提示] .venv 不可用（版本不对或依赖缺失），重建…"
        }
        Remove-Item -Recurse -Force (Join-Path $ProjRoot ".venv")
    }

    # 2) 绿色 Python 3.12（仅在缺失时下载一次，约 110MB）
    Write-Host ""
    if (Test-Path $greenPy) {
        Write-Host "[初始化] 第 1/3 步：使用已有的绿色 Python（.tools\python\python.exe）" -ForegroundColor Cyan
    } else {
        Write-Host "[初始化] 第 1/3 步：准备绿色 Python 3.12（本机没有 .tools 和可用的 .venv，约 110MB，只下载一次）…" -ForegroundColor Cyan
        # 网络预检：给出更明确的失败提示（镜像/代理问题最常见）
        & curl.exe -sI --connect-timeout 8 --max-time 10 "https://registry.npmmirror.com" *> $null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[提示] 连不上 npmmirror（下载源）。若开着代理但下载仍失败，可检查代理端口；程序会继续尝试 GitHub 源。" -ForegroundColor Yellow
        }
    }
    if (-not (Test-Path $greenPy)) {
        New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
        $pyUrl = "cpython-$PyVer%2B$PyTag-x86_64-pc-windows-msvc-install_only.tar.gz"
        $urls = @(
            "https://registry.npmmirror.com/-/binary/python-build-standalone/$PyTag/$pyUrl",
            "https://github.com/astral-sh/python-build-standalone/releases/download/$PyTag/$pyUrl",
            "https://github.com/indygreg/python-build-standalone/releases/download/$PyTag/$pyUrl"
        )
        $proxy = Get-SystemProxy
        if ($proxy) {
            Write-Host "[提示] 检测到系统代理 $proxy，下载将走该代理（直连镜像失败的常见原因）" -ForegroundColor Yellow
        }
        foreach ($url in $urls) {
            $arc = Join-Path $env:TEMP "py312-$([guid]::NewGuid().ToString('N')).tar.gz"
            Write-Host "[初始化] 下载绿色 Python $PyVer：$url"
            $curlArgs = @("-L", "--connect-timeout", "20", "--max-time", "600")
            if ($proxy) { $curlArgs += @("--proxy", $proxy) }
            $curlArgs += @("-o", $arc, $url)
            & curl.exe @curlArgs
            $ok = ($LASTEXITCODE -eq 0) -and (Test-Path $arc) -and (Get-Item $arc).Length -gt 10MB
            if ($ok) {
                Write-Host "[初始化] 解压绿色 Python…"
                & tar.exe -xzf $arc -C $toolsDir
                Remove-Item -Force $arc
                if (Test-Path $greenPy) { break }
                Write-Host "[警告] 解压后未找到 python.exe，尝试下一来源" -ForegroundColor Yellow
            } else {
                Write-Host "[警告] 该来源下载失败，尝试下一来源…" -ForegroundColor Yellow
                if (Test-Path $arc) { Remove-Item -Force $arc }
            }
        }
        if (-not (Test-Path $greenPy)) {
            Fail-Custom "[错误] Python 下载失败（npmmirror/GitHub 均失败）。若开了代理仍失败请检查代理；也可手动把 python-build-standalone 3.12.14 解压到 .tools\python\ 后重跑"
        }
    }

    # 3) 建 .venv
    Write-Host ""
    Write-Host "[初始化] 第 2/3 步：创建虚拟环境 .venv（仅首次）…" -ForegroundColor Cyan
    & $greenPy -m venv (Join-Path $ProjRoot ".venv")
    if (-not (Test-Path $venvPy)) {
        Fail-Custom "[错误] .venv 创建失败，请把上面的输出发给开发者"
    }

    # 4) 依赖（版本由 requirements.txt 控制）
    Write-Host ""
    Write-Host "[初始化] 第 3/3 步：安装依赖（requirements.txt，清华镜像优先）…" -ForegroundColor Cyan
    & $venvPy -m pip install -i $PipMirror --timeout 60 -r (Join-Path $ProjRoot "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[提示] 清华镜像拉取失败，改用官方源重试…"
        & $venvPy -m pip install -i $PipOfficial --timeout 60 -r (Join-Path $ProjRoot "requirements.txt")
    }
    if (-not (Test-Env)) {
        Fail-Custom "[错误] 依赖安装失败，请把上面的输出发给开发者"
    }
    Write-Host ""
    Write-Host "[就绪] 环境初始化完成，后续启动不再重复下载。" -ForegroundColor Green
    return $venvPy
}
