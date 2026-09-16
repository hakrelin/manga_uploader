#!/usr/bin/env bash
# 漫画发布器一键启动（Linux / macOS）
# 首次运行会在程序目录自动创建 .venv 虚拟环境并安装依赖，不污染系统 Python。
# 用法：
#   ./start.sh               # 本机启动（127.0.0.1，自动拉起浏览器）
#   ./start.sh --lan         # 局域网可访问（0.0.0.0）
#   其余参数透传给 python -m manga_uploader --web（如 --port 9000 --no-browser）
set -e
cd "$(dirname "$0")"

command -v python3 >/dev/null || { echo "[错误] 未找到 python3"; exit 1; }

# 国内用户：pip 默认走清华镜像（离海外的用户可改成官方源）
PIP_MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"

# ---- 虚拟环境：默认装在程序目录（.venv） ----
if [ ! -x ".venv/bin/python" ]; then
    echo "[初始化] 创建虚拟环境 .venv（仅首次）…"
    python3 -m venv .venv
fi
PY=.venv/bin/python

# ---- pip 进度显示 ----
# pip 自带的进度条只在真终端上画；而我们要把输出写进临时日志才能逐行读，
# 所以用 --progress-bar raw 让它输出 “Progress 已下载字节 of 总字节”，
# 再画成一行： | [######--------]  45%  1.4/3.1MB  已用 00:12  正在下载 xxx
PIP_PROGRESS="raw"
if ! "$PY" -m pip install --progress-bar raw --help >/dev/null 2>&1; then
    PIP_PROGRESS="on"   # 老版本 pip 没有 raw 选项：退回默认模式（此时只显示已用时间）
fi

_human_bytes() {
    awk -v b="$1" 'BEGIN {
        if (b >= 1048576) printf "%.1fMB", b / 1048576;
        else if (b >= 1024) printf "%.0fKB", b / 1024;
        else printf "%.0fB", b;
    }'
}

_draw_bar() {
    # $1 = 百分比(0~100)，$2 = 宽度
    local pct="${1:-0}" width="${2:-14}" filled i out=""
    [ "$pct" -lt 0 ] && pct=0
    [ "$pct" -gt 100 ] && pct=100
    filled=$(( pct * width / 100 ))
    for ((i = 0; i < filled; i++)); do out="$out#"; done
    for ((i = filled; i < width; i++)); do out="$out-"; done
    printf '%s' "$out"
}

_draw_spin_bar() {
    # $1 = 已用秒数；拿不到百分比时让光标在条里来回移动，表示“还在跑”
    local secs="${1:-0}" width=14 period=25 pos filled i out=""
    pos=$(( (secs * 10) % (period * 2) ))
    [ "$pos" -gt "$period" ] && pos=$(( period * 2 - pos ))
    filled=$(( pos * width / period ))
    [ "$filled" -lt 1 ] && filled=1
    for ((i = 0; i < filled; i++)); do out="$out#"; done
    for ((i = filled; i < width; i++)); do out="$out-"; done
    printf '%s' "$out"
}

# 生成一行状态：进度 + pip 最近一条输出
_pip_status_line() {
    local log="$1" elapsed="$2" etime raw ctx
    etime=$(printf '%02d:%02d' $((elapsed / 60)) $((elapsed % 60)))
    raw="$(tail -c 8192 "$log" 2>/dev/null | tr '\r' '\n' | awk 'NF { last = $0 } END { if (last != "") print last }')"
    if [[ "$raw" =~ ^Progress\ ([0-9]+)\ of\ ([0-9]+)$ ]] && [ "${BASH_REMATCH[2]}" -gt 0 ]; then
        local done_b="${BASH_REMATCH[1]}" total_b="${BASH_REMATCH[2]}"
        local pct=$(( done_b * 100 / total_b ))
        printf '  | [%s] %3d%%  %s/%s  已用 %s  ' "$(_draw_bar "$pct" 14)" "$pct" \
            "$(_human_bytes "$done_b")" "$(_human_bytes "$total_b")" "$etime"
    else
        printf '  | [%s] 已用 %s  ' "$(_draw_spin_bar "$elapsed")" "$etime"
    fi
    ctx="$(tail -c 8192 "$log" 2>/dev/null | tr '\r' '\n' \
        | grep -v -E '^[[:space:]]*$|^Progress [0-9]+ of [0-9]+$' | tail -n 1 | tr -d '\r')"
    if [ -z "$ctx" ]; then ctx="正在准备（连接下载源 / 解析依赖）…"; fi
    printf '%s' "$ctx"
}

# 跑一次 pip 并显示进度；返回 pip 的退出码
_run_pip() {
    local action="$1"; shift
    local log pid start now elapsed cols line rc
    log="$(mktemp "${TMPDIR:-/tmp}/mangaupload-pip.XXXXXX")"
    "$PY" -m pip "$@" --progress-bar "$PIP_PROGRESS" >"$log" 2>&1 &
    pid=$!
    start=$(date +%s)
    cols=$(tput cols 2>/dev/null || echo 100)
    case "$cols" in ''|*[!0-9]*) cols=100 ;; esac
    [ "$cols" -lt 40 ] && cols=100
    while kill -0 "$pid" 2>/dev/null; do
        now=$(date +%s)
        elapsed=$(( now - start ))
        line="$(_pip_status_line "$log" "$elapsed")"
        printf '\r%-*s' "$cols" "${line:0:$cols}"
        sleep 0.35
    done
    printf '\r%*s\r' "$cols" ""
    elapsed=$(( $(date +%s) - start ))
    rc=0
    wait "$pid" || rc=$?
    if [ "$rc" -eq 0 ]; then
        echo "  [完成] $action，用时 $(printf '%02d:%02d' $((elapsed / 60)) $((elapsed % 60)))"
    else
        echo "  [错误] $action 失败（退出码 $rc），最后几行输出："
        tr '\r' '\n' <"$log" | grep -v -E '^[[:space:]]*$|^Progress [0-9]+ of [0-9]+$' \
            | tail -n 15 | sed 's/^/      /'
    fi
    rm -f "$log"
    return "$rc"
}

# ---- 依赖（装进 .venv，缺才装） ----
if ! "$PY" -c "import requests, yaml, PIL" >/dev/null 2>&1; then
    echo "[初始化] 安装依赖（版本由 requirements.txt 控制，首次要下载几十 MB）…"
    if ! _run_pip "安装依赖（清华镜像）" install -i "$PIP_MIRROR" --timeout 60 -r requirements.txt; then
        echo "[提示] 清华镜像拉取失败（网络/分流原因），改用官方源重试…"
        _run_pip "安装依赖（官方源）" install --timeout 60 -r requirements.txt
    fi
fi

LAN=0
if [ "${1:-}" = "--lan" ]; then
    LAN=1
    shift
fi

echo "[启动] 漫画发布器 Web 前端…"
if [ "$LAN" = "1" ]; then
    exec "$PY" -m manga_uploader --web --host 0.0.0.0 "$@"
else
    exec "$PY" -m manga_uploader --web "$@"
fi
