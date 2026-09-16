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

# ------------------------------------------------------------ pip 进度显示
# 依赖安装要下载几十 MB（首次 Pillow/qrcode 等），而 pip 自带的进度条只在真终端上画：
# 输出一旦被重定向到文件（我们要读它才能显示进度），pip 就完全静默，看起来像卡死。
# 所以这里：
#   1) 用 `--progress-bar raw` 让 pip 输出 “Progress 已下载字节 of 总字节” 的纯文本进度；
#   2) pip 丢到后台跑、输出写进临时日志，主线程每 0.35 秒刷新一行，例如：
#        / [######--------]  45%  1.4/3.1MB  已用 00:12  正在下载 Pillow-...whl
#   3) 读不到字节进度（老版本 pip）就退化成移动指示条 + 已用时间 + pip 最近一行输出。

function Get-ConsoleWidth {
    try {
        $w = [Console]::WindowWidth
        if ($w -gt 40) { return [int]($w - 1) }
    } catch { }
    return 99
}

function ConvertFrom-LogBytes {
    param([byte[]]$Bytes)
    if (-not $Bytes -or $Bytes.Length -eq 0) { return "" }
    try {
        $utf8 = New-Object System.Text.UTF8Encoding($false, $true)
        return $utf8.GetString($Bytes)
    } catch {
        try { return [System.Text.Encoding]::GetEncoding(936).GetString($Bytes) } catch { return "" }
    }
}

# 读日志末尾（最多 16KB），返回非空片段，按“新 → 旧”排列
function Get-LogTailSegments {
    param([string]$Path, [int]$Max = 60)
    if (-not (Test-Path -LiteralPath $Path)) { return @() }
    try {
        $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
    } catch { return @() }
    $text = ""
    try {
        if ($fs.Length -gt 0) {
            $count = [int][Math]::Min(16384, $fs.Length)
            [void]$fs.Seek(-$count, [System.IO.SeekOrigin]::End)
            $buf = New-Object byte[] $count
            $read = $fs.Read($buf, 0, $count)
            if ($read -gt 0) {
                if ($read -lt $count) { $buf = $buf[0..($read - 1)] }
                $text = ConvertFrom-LogBytes $buf
            }
        }
    } catch {
        $text = ""
    } finally {
        $fs.Dispose()
    }
    $all = New-Object System.Collections.ArrayList
    foreach ($seg in ($text -split "[`r`n]+")) {
        $t = $seg.Trim()
        if ($t) { [void]$all.Add($t) }
    }
    $out = New-Object System.Collections.ArrayList
    for ($i = $all.Count - 1; $i -ge 0 -and $out.Count -lt $Max; $i--) { [void]$out.Add($all[$i]) }
    return $out.ToArray()
}

# pip 的进度行：raw 模式是 “Progress 2097152 of 6292343”，终端模式形如 “45%|####  | 1.2M/2.7M”
function Test-PipProgressLine {
    param([string]$Line)
    if (-not $Line) { return $false }
    if ($Line -match '^Progress\s+\d+\s+of\s+\d+$') { return $true }
    return ($Line -match '\d+\s*%' -and $Line -match '\|')
}

function Format-Bytes {
    param([double]$Bytes)
    if ($Bytes -ge 1048576) { return ("{0:0.0}MB" -f ($Bytes / 1048576)) }
    if ($Bytes -ge 1024) { return ("{0:0}KB" -f ($Bytes / 1024)) }
    return ("{0:0}B" -f $Bytes)
}

function Format-ProgressBar {
    param([int]$Percent, [int]$Width = 14)
    $p = [Math]::Max(0, [Math]::Min(100, $Percent))
    $filled = [int][Math]::Round($p * $Width / 100)
    return ("#" * $filled).PadRight($Width, "-")
}

# 拿不到真实百分比时的“还在跑”指示条：光标在条内来回移动
function Format-SpinBar {
    param([double]$Seconds, [int]$Width = 14)
    $period = 2.5
    $pos = $Seconds % ($period * 2)
    if ($pos -gt $period) { $pos = $period * 2 - $pos }
    $filled = [int][Math]::Round(($pos / $period) * $Width)
    if ($filled -lt 1) { $filled = 1 }
    if ($filled -gt $Width) { $filled = $Width }
    return ("#" * $filled).PadRight($Width, "-")
}

# 汇总当前要显示的信息：字节百分比 / 已下载量 / pip 最近一条输出
function Get-PipSnapshot {
    param([string]$OutPath, [string]$ErrPath)
    $lines = @(Get-LogTailSegments $OutPath) + @(Get-LogTailSegments $ErrPath)
    $percent = $null
    $sizes = ""
    foreach ($line in $lines) {
        if ($line -match '^Progress\s+(\d+)\s+of\s+(\d+)$') {
            $done = [double]$Matches[1]
            $total = [double]$Matches[2]
            if ($total -gt 0) {
                $percent = [int][Math]::Round($done * 100 / $total)
                $sizes = (Format-Bytes $done) + "/" + (Format-Bytes $total)
            }
            break
        }
    }
    $context = ""
    foreach ($line in $lines) {
        if (-not (Test-PipProgressLine $line)) { $context = $line; break }
    }
    return [pscustomobject]@{ Percent = $percent; Sizes = $sizes; Context = $context }
}

function Get-PipLogMatch {
    param([string[]]$Paths, [string]$Pattern)
    $best = ""
    foreach ($p in $Paths) {
        if (-not (Test-Path -LiteralPath $p)) { continue }
        try { $text = [System.IO.File]::ReadAllText($p) } catch { continue }
        foreach ($seg in ($text -split "[`r`n]+")) {
            $t = $seg.Trim()
            if ($t -and $t -match $Pattern) { $best = $t }
        }
    }
    return $best
}

# 日志里有没有 pip 自己报的 ERROR:（拿不到退出码时兜底用）
function Test-PipLogHasError {
    param([string[]]$Paths)
    foreach ($p in $Paths) {
        if (-not (Test-Path -LiteralPath $p)) { continue }
        try { $text = [System.IO.File]::ReadAllText($p) } catch { continue }
        foreach ($seg in ($text -split "[`r`n]+")) {
            if ($seg.Trim() -match '^ERROR:') { return $true }
        }
    }
    return $false
}

function Show-PipLogTail {
    param([string]$Path, [int]$Count = 20)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    try { $text = [System.IO.File]::ReadAllText($Path) } catch { return }
    $lines = New-Object System.Collections.ArrayList
    foreach ($seg in ($text -split "[`r`n]+")) {
        $t = $seg.Trim()
        if ($t -and -not (Test-PipProgressLine $t)) { [void]$lines.Add($t) }
    }
    if ($lines.Count -eq 0) { return }
    $skip = [Math]::Max(0, $lines.Count - $Count)
    for ($i = $skip; $i -lt $lines.Count; $i++) {
        Write-Host ("      " + $lines[$i]) -ForegroundColor DarkGray
    }
}

# 选一个 pip 支持的进度模式：raw 会把 “Progress 已下载 of 总字节” 写进日志，
# 重定向到文件后照样能读到；老版本 pip 没这个选项（invalid choice）→ 退回默认的 on。
function Get-PipProgressMode {
    param([string]$VenvPy)
    & cmd.exe /c "`"$VenvPy`" -m pip install --progress-bar raw --help >nul 2>&1"
    if ($LASTEXITCODE -eq 0) { return "raw" }
    return "on"
}

# cmd 用的最小引号处理：含空格/特殊字符的参数补上引号
function ConvertTo-CmdArg {
    param([string]$Text)
    if ($Text -match '[\s"&|<>^]') { return '"' + ($Text -replace '"', '""') + '"' }
    return $Text
}

# 跑一次 pip：返回退出码，过程中显示进度；失败时把日志尾部打出来便于排查
function Invoke-PipInstall {
    param(
        [Parameter(Mandatory = $true)][string]$VenvPy,
        [Parameter(Mandatory = $true)][string[]]$PipArgs,
        [Parameter(Mandatory = $true)][string]$Action,
        [string]$WorkingDirectory = ""
    )
    $stamp = [guid]::NewGuid().ToString("N")
    $outPath = Join-Path $env:TEMP "mangaupload-pip-$stamp.out"
    $errPath = Join-Path $env:TEMP "mangaupload-pip-$stamp.err"
    $prevIo = $env:PYTHONIOENCODING
    $env:PYTHONIOENCODING = "utf-8"   # 让 pip 输出固定 UTF-8，中文与进度都能正确读出来

    # 由 cmd 负责重定向输出：Windows PowerShell 5.1 里 Start-Process 一旦同时用
    # -RedirectStandardOutput/-RedirectStandardError 就取不到子进程退出码
    # （$proc.ExitCode 恒为 $null，会误判成安装失败），交给 cmd 重定向 + `if errorlevel`
    # 既拿得到退出码，也不影响我们实时读日志。
    $argLine = (@("-m", "pip") + $PipArgs | ForEach-Object { ConvertTo-CmdArg $_ }) -join " "
    $inner = '"' + $VenvPy + '" ' + $argLine + ' > "' + $outPath + '" 2> "' + $errPath + '" & if errorlevel 1 (exit /b 1) else (exit /b 0)'
    $startArgs = @{
        FilePath     = $env:ComSpec
        ArgumentList = @("/d", "/s", "/c", ('"' + $inner + '"'))
        PassThru     = $true
        WindowStyle  = "Hidden"
    }
    if ($WorkingDirectory) { $startArgs["WorkingDirectory"] = $WorkingDirectory }
    try {
        $proc = Start-Process @startArgs
    } catch {
        if ($null -eq $prevIo) {
            Remove-Item Env:\PYTHONIOENCODING -ErrorAction SilentlyContinue
        } else {
            $env:PYTHONIOENCODING = $prevIo
        }
        Write-Host ("[错误] 无法启动 pip：" + $_.Exception.Message) -ForegroundColor Red
        return 1
    }
    $width = Get-ConsoleWidth
    $spin = @("|", "/", "-", "\")
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $i = 0
    while (-not $proc.HasExited) {
        $snap = Get-PipSnapshot $outPath $errPath
        $elapsed = "{0:mm\:ss}" -f $sw.Elapsed
        $head = "  " + $spin[$i % 4] + " ["
        if ($null -ne $snap.Percent) {
            $head = $head + (Format-ProgressBar $snap.Percent 14) + "] " + ("{0,3}" -f $snap.Percent) + "%  " + $snap.Sizes + "  已用 " + $elapsed
        } else {
            $head = $head + (Format-SpinBar $sw.Elapsed.TotalSeconds 14) + "] 已用 " + $elapsed
        }
        $text = $snap.Context
        if (-not $text) { $text = "正在准备（连接下载源 / 解析依赖）…" }
        $line = $head + "  " + $text
        if ($line.Length -gt $width) { $line = $line.Substring(0, $width) }
        Write-Host ("`r" + $line.PadRight($width)) -NoNewline -ForegroundColor Cyan
        $i++
        Start-Sleep -Milliseconds 350
    }
    $sw.Stop()
    Write-Host ("`r" + (" " * $width) + "`r") -NoNewline
    $code = $null
    try { $code = $proc.ExitCode } catch { $code = $null }
    if ($null -eq $code) {
        # 兜底：拿不到退出码时看日志里有没有 pip 报的 ERROR:（调用方随后还会用 Test-Env 复核）
        if (Test-PipLogHasError @($outPath, $errPath)) { $code = 1 } else { $code = 0 }
    }
    if ($null -eq $prevIo) {
        Remove-Item Env:\PYTHONIOENCODING -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONIOENCODING = $prevIo
    }
    if ($code -eq 0) {
        $summary = Get-PipLogMatch @($outPath, $errPath) "Successfully installed"
        if ($summary) {
            if ($summary.Length -gt $width) { $summary = $summary.Substring(0, $width) }
            Write-Host ("      " + $summary) -ForegroundColor DarkGray
        }
        Write-Host ("      " + $Action + "完成，用时 " + ("{0:mm\:ss}" -f $sw.Elapsed)) -ForegroundColor Green
    } else {
        Write-Host ("[错误] " + $Action + "失败（退出码 " + $code + "），最后几行输出：") -ForegroundColor Yellow
        Show-PipLogTail $outPath 25
        Show-PipLogTail $errPath 25
    }
    Remove-Item -LiteralPath $outPath, $errPath -Force -ErrorAction SilentlyContinue
    return $code
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

    # 4) 依赖（版本由 requirements.txt 控制；带实时进度，避免“卡住”错觉）
    Write-Host ""
    Write-Host "[初始化] 第 3/3 步：安装依赖（requirements.txt，清华镜像优先）…" -ForegroundColor Cyan
    Write-Host "         首次要下载几十 MB，下面一行进度会实时刷新（已下载量 + 已用时间），慢的时候请别关窗口。" -ForegroundColor DarkGray
    $progMode = Get-PipProgressMode $venvPy
    $pipCommon = @("install", "--progress-bar", $progMode, "--timeout", "60", "-r", "requirements.txt")
    $code = Invoke-PipInstall -VenvPy $venvPy -PipArgs (@("-i", $PipMirror) + $pipCommon) -Action "安装依赖（清华镜像）" -WorkingDirectory $ProjRoot
    if ($code -ne 0) {
        Write-Host "[提示] 清华镜像拉取失败，改用官方源重试…" -ForegroundColor Yellow
        $code = Invoke-PipInstall -VenvPy $venvPy -PipArgs (@("-i", $PipOfficial) + $pipCommon) -Action "安装依赖（官方源）" -WorkingDirectory $ProjRoot
    }
    if ($code -ne 0 -or -not (Test-Env)) {
        Fail-Custom "[错误] 依赖安装失败，请把上面的输出发给开发者"
    }
    Write-Host ""
    Write-Host "[就绪] 环境初始化完成，后续启动不再重复下载。" -ForegroundColor Green
    return $venvPy
}
