#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_TARGETS=(
  "/opt/homebrew/lib/libgdiplus.dylib"
  "/usr/local/lib/libgdiplus.dylib"
)
LIB_LINK="${ROOT_DIR}/liblibgdiplus.dylib"

for candidate in "${LIB_TARGETS[@]}"; do
  if [[ -f "${candidate}" ]]; then
    ln -sf "${candidate}" "${LIB_LINK}"
    break
  fi
done

FALLBACK_PATHS=("${ROOT_DIR}" "/opt/homebrew/lib" "/usr/local/lib")
FALLBACK_JOINED=""
for path in "${FALLBACK_PATHS[@]}"; do
  if [[ -d "${path}" ]]; then
    if [[ -n "${FALLBACK_JOINED}" ]]; then
      FALLBACK_JOINED="${FALLBACK_JOINED}:"
    fi
    FALLBACK_JOINED="${FALLBACK_JOINED}${path}"
  fi
done
if [[ -n "${DYLD_FALLBACK_LIBRARY_PATH:-}" ]]; then
  FALLBACK_JOINED="${FALLBACK_JOINED}:${DYLD_FALLBACK_LIBRARY_PATH}"
fi
export DYLD_FALLBACK_LIBRARY_PATH="${FALLBACK_JOINED}"

cd "${ROOT_DIR}"
uvicorn fastapi_app:app --host 127.0.0.1 --port 8001
