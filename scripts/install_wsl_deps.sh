#!/usr/bin/env bash
# 数据科学家 Community - WSL / Linux 一键安装依赖
# 用法: bash scripts/install_wsl_deps.sh
# 作用: 安装 Python 3.12 (uv, 用户级) + Node 22 (用户级) + Playwright 系统依赖 (sudo)
#       + venv + npm ci + Playwright Chromium
# 说明: 仅 apt 系统依赖需要 sudo；Python / Node 均装在用户目录，不污染系统。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$ROOT_DIR"

# 检测是否 Linux / WSL（非 Linux 直接提示退出）
OS_NAME="$(uname -s)"
if [[ "$OS_NAME" != "Linux" ]]; then
  echo "[warn] 本脚本仅适用于 Linux / WSL；macOS 请直接运行 ./start.sh"
  exit 1
fi

echo "[1/6] 准备 Python 3.12 (uv) ..."
if command -v uv &>/dev/null; then
  UV_BIN="$(command -v uv)"
elif [[ -x "$HOME/.local/bin/uv" ]]; then
  UV_BIN="$HOME/.local/bin/uv"
else
  echo "  未找到 uv，正在安装到 ~/.local/bin ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  UV_BIN="$HOME/.local/bin/uv"
  export PATH="$HOME/.local/bin:$PATH"
fi
echo "  uv: $UV_BIN"
PY312="$("$UV_BIN" python find 3.12 2>/dev/null || true)"
if [[ -z "$PY312" ]]; then
  "$UV_BIN" python install 3.12
fi
PY312="$("$UV_BIN" python find 3.12)"
echo "  Python 3.12: $PY312"

echo "[2/6] 准备 Node.js 22 (>=22.12 <23) ..."
NODE_BIN="$(command -v node || true)"
NODE_VERSION=""
if [[ -n "$NODE_BIN" ]]; then
  NODE_VERSION="$("$NODE_BIN" --version 2>/dev/null | sed 's/^v//' || true)"
fi
if [[ "$NODE_VERSION" =~ ^22\.1[2-9]\. ]]; then
  echo "  已有受支持 Node: v$NODE_VERSION ($NODE_BIN)"
else
  NODE22_DIR="$HOME/.local/node22"
  if [[ -x "$NODE22_DIR/bin/node" ]]; then
    NODE_VERSION="$("$NODE22_DIR/bin/node" --version | sed 's/^v//')"
    echo "  使用 ~/.local/node22: v$NODE_VERSION"
    export PATH="$NODE22_DIR/bin:$PATH"
  else
    echo "  当前 Node: ${NODE_BIN:-未找到} v${NODE_VERSION:-?}，下载 Node 22.14.0 到 ~/.local/node22 ..."
    mkdir -p "$NODE22_DIR"
    curl -fsSL -o /tmp/node22.tar.xz https://nodejs.org/dist/v22.14.0/node-v22.14.0-linux-x64.tar.xz
    tar -xJf /tmp/node22.tar.xz -C "$NODE22_DIR" --strip-components=1
    rm -f /tmp/node22.tar.xz
    export PATH="$NODE22_DIR/bin:$PATH"
    echo "  Node: v$("$NODE22_DIR/bin/node" --version)"
  fi
fi

echo "[3/6] 创建 Python venv 并安装依赖 ..."
if [[ ! -d .venv ]]; then
  "$PY312" -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip -q
.venv/bin/python -m pip install -r requirements.txt -q

echo "[4/6] 安装 npm 依赖 ..."
npm ci

echo "[5/6] 安装 Playwright Chromium 系统依赖 (优先 sudo) ..."
if npx playwright install-deps chromium; then
  echo "  系统依赖 OK"
elif sudo npx playwright install-deps chromium; then
  echo "  系统依赖 OK (sudo)"
else
  echo "  [warn] sudo 不可用，改用用户级解包（无需 root）..."
  bash scripts/install_linux_browser_libs.sh
fi

echo "[6/6] 安装 Playwright Chromium 浏览器 ..."
npx playwright install chromium

chmod +x start.sh scripts/*.sh

echo ""
echo "=========================================="
echo " WSL 依赖安装完成 ✅"
echo " 启动服务: ./start.sh"
echo " (浏览器采集需在可见窗口中登录各平台)"
echo "=========================================="
