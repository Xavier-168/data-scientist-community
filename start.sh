#!/usr/bin/env bash
# 自媒体数据分析大师 - macOS 启动脚本
# 用法: 双击运行或在终端中执行 ./start.sh
# 兼容 Intel (x86_64) 和 Apple Silicon (arm64)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# --- 检测芯片架构 ---
ARCH="$(uname -m)"
if [[ "$ARCH" == "arm64" ]]; then
  echo "[info] 检测到 Apple Silicon (M系列) 芯片"
elif [[ "$ARCH" == "x86_64" ]]; then
  echo "[info] 检测到 Intel 芯片"
else
  echo "[warn] 未知架构: $ARCH，继续尝试..."
fi

# --- 注入 Homebrew 路径 ---
# ARM Mac (M系列): Homebrew 安装在 /opt/homebrew/bin
# Intel Mac: Homebrew 安装在 /usr/local/bin
# 两个都加上，确保无论哪种架构都能找到 node/python3/npm
for BREW_PATH in /opt/homebrew/bin /usr/local/bin; do
  if [[ -d "$BREW_PATH" ]] && [[ ":$PATH:" != *":$BREW_PATH:"* ]]; then
    export PATH="$BREW_PATH:$PATH"
  fi
done

# --- 检测 Python3 ---
PYTHON_BIN=""
# 优先级: brew python3 > 系统 python3 > python
for candidate in python3 python; do
  if command -v "$candidate" &>/dev/null; then
    PYTHON_BIN="$(command -v "$candidate")"
    break
  fi
done
if [[ -z "$PYTHON_BIN" ]]; then
  echo ""
  echo "[error] 未找到 Python。请先安装 Python 3.11+。"
  echo ""
  if [[ "$ARCH" == "arm64" ]]; then
    echo "  推荐安装方式（Apple Silicon）:"
    echo "    brew install python@3.12"
    echo ""
    echo "  如果还没有 Homebrew:"
    echo "    /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
  else
    echo "  推荐安装方式（Intel Mac）:"
    echo "    brew install python@3.12"
    echo ""
    echo "  或从官网下载: https://www.python.org/downloads/"
  fi
  exit 1
fi

# --- 检查 Python 版本 >= 3.11 ---
PY_VERSION=$("$PYTHON_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
PY_MAJOR=$("$PYTHON_BIN" -c "import sys; print(sys.version_info.major)" 2>/dev/null || echo "0")
PY_MINOR=$("$PYTHON_BIN" -c "import sys; print(sys.version_info.minor)" 2>/dev/null || echo "0")

if [[ "$PY_MAJOR" -lt 3 ]] || { [[ "$PY_MAJOR" -eq 3 ]] && [[ "$PY_MINOR" -lt 11 ]]; }; then
  echo ""
  echo "[error] Python 版本过低: $PY_VERSION（当前: $PYTHON_BIN）"
  echo "  本项目需要 Python >= 3.11（pandas 2.x 的最低要求）。"
  echo ""
  echo "  你当前的 Python 可能是 macOS 系统自带的旧版本。"
  echo "  请通过 Homebrew 安装新版 Python:"
  echo "    brew install python@3.12"
  echo ""
  echo "  安装后重新运行此脚本即可。"
  echo "  如果已安装但没生效，请重启终端或执行: export PATH=\"/opt/homebrew/bin:\$PATH\""
  exit 1
fi

# --- 检测 Node.js ---
NODE_BIN=""
if command -v node &>/dev/null; then
  NODE_BIN="$(command -v node)"
fi
if [[ -z "$NODE_BIN" ]]; then
  echo ""
  echo "[error] 未找到 Node.js。请先安装 Node.js 22.12.x。"
  echo ""
  echo "  推荐安装方式:"
  echo "    brew install node"
  echo ""
  echo "  或从官网下载: https://nodejs.org/"
  echo ""
  echo "  安装后重新运行此脚本即可。"
  exit 1
fi

# --- 检查 Node.js 版本 >= 22.12 且 < 23 ---
NODE_VERSION=$("$NODE_BIN" --version 2>/dev/null | sed 's/^v//' || echo "0.0.0")
if [[ "$NODE_VERSION" =~ ^([0-9]+)\.([0-9]+)(\.[0-9]+)?([-+].*)?$ ]]; then
  NODE_MAJOR="${BASH_REMATCH[1]}"
  NODE_MINOR="${BASH_REMATCH[2]}"
else
  NODE_MAJOR=0
  NODE_MINOR=0
fi

if [[ "$NODE_MAJOR" -ne 22 ]] || [[ "$NODE_MINOR" -lt 12 ]]; then
  echo ""
  echo "[error] Node.js 版本不受支持: v$NODE_VERSION（当前: $NODE_BIN）"
  echo "  本项目需要 Node.js >= 22.12 且 < 23。"
  echo ""
  echo "  请升级 Node.js:"
  echo "    brew upgrade node"
  echo ""
  echo "  或从官网下载最新版: https://nodejs.org/"
  exit 1
fi

# --- 设置环境变量 ---
export PYTHONUNBUFFERED=1
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

echo "[info] 架构: $ARCH"
echo "[info] Python: $PYTHON_BIN ($PY_VERSION)"
echo "[info] Node: $NODE_BIN (v$NODE_VERSION)"
echo "[info] 启动监控服务..."

"$PYTHON_BIN" scripts/start_monitor.py "$@"
EXIT_CODE=$?

if [[ $EXIT_CODE -ne 0 ]]; then
  echo ""
  echo "[error] 启动失败 (退出码: $EXIT_CODE)，请查看上面的提示信息。"
  exit $EXIT_CODE
fi
