#!/usr/bin/env bash
set -euo pipefail

SOURCE="${LIBREOFFICE_FONTCONFIG_SOURCE:-/app/config/fontconfig/local.conf}"
TARGET="${LIBREOFFICE_FONTCONFIG_TARGET:-/etc/fonts/local.conf}"

if [ ! -f "$SOURCE" ]; then
  echo "fontconfig source not found: $SOURCE" >&2
  exit 1
fi

mkdir -p "$(dirname "$TARGET")"
cp "$SOURCE" "$TARGET"
fc-cache -f