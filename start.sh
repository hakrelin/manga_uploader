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

# ---- 依赖（装进 .venv，缺才装） ----
if ! "$PY" -c "import requests, yaml, PIL" >/dev/null 2>&1; then
    echo "[初始化] 安装依赖 requests / PyYAML / Pillow（清华镜像）…"
    "$PY" -m pip install -i "$PIP_MIRROR" requests PyYAML Pillow
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
