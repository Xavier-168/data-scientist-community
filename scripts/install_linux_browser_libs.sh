#!/usr/bin/env bash
# 无 sudo 环境下，为 Playwright Chromium 安装用户级系统库
# 用法: bash scripts/install_linux_browser_libs.sh
# 原理: apt download + dpkg -x 解包到 ~/.local/chromium-libs，
#       由 start.sh 自动注入 LD_LIBRARY_PATH。有 sudo 时更推荐:
#       sudo npx playwright install-deps chromium

set -euo pipefail

TARGET="$HOME/.local/chromium-libs"
LIBS_DIR="$TARGET/usr/lib/x86_64-linux-gnu"

if [[ -f "$LIBS_DIR/libnspr4.so" && -f "$LIBS_DIR/libnss3.so" && -f "$LIBS_DIR/libasound.so.2" ]]; then
  echo "  已有用户级系统库: $LIBS_DIR"
  exit 0
fi

if ! command -v apt &>/dev/null || ! command -v dpkg &>/dev/null; then
  echo "[error] 需要 apt 与 dpkg（Debian/Ubuntu/WSL 默认自带）"
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cd "$TMP"

echo "  下载并解包 libnspr4 / libnss3 / libasound2 ..."
apt download libnspr4 libnss3 libasound2 >/dev/null 2>&1 || {
  echo "[error] apt download 失败，请检查网络或改用: sudo npx playwright install-deps chromium"
  exit 1
}
for f in *.deb; do
  dpkg -x "$f" "$TARGET"
done

echo "  系统库已解包到: $LIBS_DIR"
echo "  start.sh 会自动注入 LD_LIBRARY_PATH，无需手动配置"
