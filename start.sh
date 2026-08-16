#!/usr/bin/env bash
# 自媒体数据分析大师 - 启动脚本
# 用法: 双击运行或在终端中执行 ./start.sh
# 兼容 macOS (Intel x86_64 / Apple Silicon arm64) 与 Linux / WSL
# WSL 环境建议先运行 scripts/install_wsl_deps.sh 一键安装依赖

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# --- 检测操作系统与芯片架构 ---
OS_NAME="$(uname -s)"
ARCH="$(uname -m)"
if [[ "$OS_NAME" == "Linux" ]]; then
  if grep -qi "microsoft\|wsl" /proc/version 2>/dev/null; then
    echo "[info] 检测到 WSL (Linux) 环境"
  else
    echo "[info] 检测到 Linux 环境"
  fi
elif [[ "$ARCH" == "arm64" ]]; then
  echo "[info] 检测到 Apple Silicon (M系列) 芯片"
elif [[ "$ARCH" == "x86_64" ]]; then
  echo "[info] 检测到 Intel 芯片"
else
  echo "[warn] 未知架构: $ARCH，继续尝试..."
fi

# --- 注入运行时路径 ---
# macOS: Homebrew 在 /opt/homebrew/bin (ARM) 或 /usr/local/bin (Intel)
# Linux/WSL: uv 装 Python 到 ~/.local/bin，Node 22 到 ~/.local/node22/bin，nvm 在 ~/.nvm
for EXTRA_PATH in \
  /opt/homebrew/bin /usr/local/bin \
  "$HOME/.local/bin" "$HOME/.local/node22/bin" \
  "$HOME/.nvm/versions/node"/*/bin \
  "$HOME/.config/nvm/versions/node"/*/bin; do
  if [[ -d "$EXTRA_PATH" ]] && [[ ":$PATH:" != *":$EXTRA_PATH:"* ]]; then
    export PATH="$EXTRA_PATH:$PATH"
  fi
done

# --- 注入 Chromium 系统库（无 sudo 环境的用户级兜底） ---
# scripts/install_linux_browser_libs.sh 会把 Playwright 系统依赖解包到 ~/.local/chromium-libs；
# 有 sudo 并已运行 install-deps 时该目录不存在，此步自动跳过
CHROMIUM_LIBS="$HOME/.local/chromium-libs/usr/lib/x86_64-linux-gnu"
if [[ -d "$CHROMIUM_LIBS" ]] && [[ ":${LD_LIBRARY_PATH:-}:" != *":$CHROMIUM_LIBS:"* ]]; then
  export LD_LIBRARY_PATH="$CHROMIUM_LIBS${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

# --- WSLg 图形环境注入 ---
# WSL2 的 WSLg 提供 X server（/tmp/.X11-unix/X0），但部分发行版/配置不会自动
# 注入 DISPLAY，导致"吊起系统浏览器授权"时浏览器无法打开。
if [[ "$OS_NAME" == "Linux" ]] && [[ -z "${DISPLAY:-}" ]] && { [[ -d /mnt/wslg ]] || [[ -e /tmp/.X11-unix/X0 ]]; }; then
  export DISPLAY="${DISPLAY:-:0}"
  echo "[info] 检测到 WSLg，注入 DISPLAY=$DISPLAY（授权浏览器将显示到 Windows 桌面）"
fi

# --- 检测 Python3 ---
# 优先级: 项目 .venv > python3.12 > python3.11 > python3 > python
# .venv 已由 README 流程或 install_wsl_deps.sh 创建，其中装有全部 Python 依赖
PYTHON_BIN=""
if [[ -x ".venv/bin/python" ]]; then
  PYTHON_BIN="$(pwd)/.venv/bin/python"
fi
if [[ -z "$PYTHON_BIN" ]]; then
  for candidate in python3.12 python3.11 python3 python; do
    if command -v "$candidate" &>/dev/null; then
      PYTHON_BIN="$(command -v "$candidate")"
      break
    fi
  done
fi
if [[ -z "$PYTHON_BIN" ]]; then
  echo ""
  echo "[error] 未找到 Python。请先安装 Python 3.11+。"
  echo ""
  if [[ "$OS_NAME" == "Linux" ]]; then
    echo "  推荐安装方式（Linux / WSL）:"
    echo "    curl -LsSf https://astral.sh/uv/install.sh | sh   # 安装 uv"
    echo "    uv python install 3.12                            # 安装 Python 3.12"
    echo ""
    echo "  或使用系统包管理器: sudo apt-get install -y python3.12 python3.12-venv"
  elif [[ "$ARCH" == "arm64" ]]; then
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
  if [[ "$OS_NAME" == "Linux" ]]; then
    echo "  推荐安装方式（Linux / WSL）:"
    echo "    curl -fsSL https://nodejs.org/dist/v22.14.0/node-v22.14.0-linux-x64.tar.xz | tar -xJ -C ~/.local --strip-components=1 -f -"
    echo "    # 或将二进制解压到 ~/.local/node22 后，start.sh 会自动加入 PATH"
    echo ""
    echo "  或使用 nvm: nvm install 22"
  else
    echo "  请升级 Node.js:"
    echo "    brew upgrade node"
    echo ""
    echo "  或从官网下载最新版: https://nodejs.org/"
  fi
  exit 1
fi

# --- 设置环境变量 ---
export PYTHONUNBUFFERED=1
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

# --- WSL 环境默认参数 ---
# 1) 绑定 0.0.0.0，允许 Windows 侧浏览器访问（WSL2 NAT 下仅 Windows 宿主可达）
# 2) 无图形界面 (无 $DISPLAY) 时自动 --no-open，避免弹出浏览器报错
EXTRA_ARGS=()
if [[ "$OS_NAME" == "Linux" ]]; then
  if ! [[ " $* " == *" --host "* ]] && ! [[ " $* " == *" --host="* ]]; then
    EXTRA_ARGS+=(--host 0.0.0.0)
  fi
  if [[ -z "${DISPLAY:-}" ]] && ! [[ " $* " == *"--no-open"* ]]; then
    EXTRA_ARGS+=(--no-open)
  fi
  if [[ -n "${EXTRA_ARGS[*]}" ]]; then
    echo "[info] WSL 默认参数: ${EXTRA_ARGS[*]}（Windows 侧访问 http://<WSL_IP>:8811/monitor）"
  fi
fi

echo "[info] 架构: $ARCH"
echo "[info] Python: $PYTHON_BIN ($PY_VERSION)"
echo "[info] Node: $NODE_BIN (v$NODE_VERSION)"
echo "[info] 启动监控服务..."

"$PYTHON_BIN" scripts/start_monitor.py "${EXTRA_ARGS[@]}" "$@"
EXIT_CODE=$?

if [[ $EXIT_CODE -ne 0 ]]; then
  echo ""
  echo "[error] 启动失败 (退出码: $EXIT_CODE)，请查看上面的提示信息。"
  exit $EXIT_CODE
fi
