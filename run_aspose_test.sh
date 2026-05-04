#!/bin/zsh
set -euo pipefail

ROOT_DIR="/Users/yeyeong/ai_translation"
PYTHON_BIN="/usr/local/bin/python3"
LIB_TARGET="/opt/homebrew/lib/libgdiplus.dylib"
LIB_LINK="${ROOT_DIR}/liblibgdiplus.dylib"
TEST_FILE="${ROOT_DIR}/test.py"

if [[ ! -f "${LIB_TARGET}" ]]; then
  echo "libgdiplus를 찾을 수 없습니다: ${LIB_TARGET}" >&2
  echo "먼저 'brew install mono-libgdiplus' 상태를 확인하세요." >&2
  exit 1
fi

ln -sf "${LIB_TARGET}" "${LIB_LINK}"

export DYLD_FALLBACK_LIBRARY_PATH="${ROOT_DIR}:/opt/homebrew/lib:${DYLD_FALLBACK_LIBRARY_PATH:-}"

exec "${PYTHON_BIN}" "${TEST_FILE}"
