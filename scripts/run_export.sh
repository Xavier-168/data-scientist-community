#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

NODE_BIN="${NODE_BIN:-$(command -v node || true)}"
if [[ -z "$NODE_BIN" ]]; then
  echo "[error] node 未安装或不在 PATH" >&2
  exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || command -v python || true)}"
if [[ -z "$PYTHON_BIN" ]]; then
  echo "[error] python 未安装或不在 PATH" >&2
  exit 1
fi
export PYTHON_BIN

export BROWSER_CHANNEL="${BROWSER_CHANNEL:-chrome}"
export SCAN_WAIT_MS="${SCAN_WAIT_MS:-600000}"
export SCAN_POLL_MS="${SCAN_POLL_MS:-2000}"
export VIDEO_LIMIT="${VIDEO_LIMIT:-999}"
export MIN_PUBLISH_DATE="${MIN_PUBLISH_DATE:-}"
export REFRESH_DAYS="${REFRESH_DAYS:-0}"
export REFRESH_LATEST_COUNT="${REFRESH_LATEST_COUNT:-$VIDEO_LIMIT}"
export FORCE_FULL_EXPORT="${FORCE_FULL_EXPORT:-false}"
export STALE_ROUNDS_LIMIT="${STALE_ROUNDS_LIMIT:-3}"
export HEADLESS="${HEADLESS:-true}"

"$NODE_BIN" scripts/douyin_export.mjs
