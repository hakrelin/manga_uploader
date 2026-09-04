# 打包给朋友 / 其它电脑运行的分发包。
# 用法：
#   .\pack-for-friend.ps1             # 小包：不带 Python 环境，朋友首次运行需联网下载
#   .\pack-for-friend.ps1 -IncludeTools  # 大包：带 .tools 绿色 Python（约 150MB+），朋友无需下载
#
# 关键点：绝不打包 .venv（虚拟环境路径绑定本机，别人打开必坏），
# 也不带本机 config.yaml（含 Cookie）与运行产物。
param(
    [switch]$IncludeTools
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
$zipName = "manga_uploader-分享版-$stamp.zip"
$zipPath = Join-Path (Split-Path $Root -Parent) $zipName

$stage = Join-Path $env:TEMP ("mu-pack-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $stage | Out-Null

# 排除目录：运行环境 / 缓存 / 输出 / git / 他人代码环境
$xd = @(".venv", "output", ".git", ".agents", ".codex", "__pycache__")
if (-not $IncludeTools) { $xd += ".tools" }
$xf = @("*.zip", "*.pyc", "config.yaml", "config.local.yaml")

$argList = @($Root, $stage, "/E")
foreach ($d in $xd) { $argList += @("/XD", $d) }
foreach ($f in $xf) { $argList += @("/XF", $f) }
$argList += @("/NFL", "/NDL", "/NJH", "/NJS", "/NC", "/NS")
& robocopy.exe @argList | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy 复制失败（退出码 $LASTEXITCODE）" }

Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zipPath -CompressionLevel Optimal
Remove-Item -LiteralPath $stage -Recurse -Force

$mode = if ($IncludeTools) { "带 .tools 绿色 Python（大包，朋友无需联网下载）" } else { "不带 Python 环境（小包，朋友首次运行需联网下载）" }
Write-Host ""
Write-Host "已生成分享包：$zipPath" -ForegroundColor Green
Write-Host "打包模式：$mode"
Write-Host "提示：发送前请确认压缩包内没有 config.yaml / .venv；朋友解压后双击 start.bat 即可。"
