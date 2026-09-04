#!/bin/sh
set -eu

RAW="https://raw.githubusercontent.com/youqishi1/path-socks/main/install.sh"

if ! command -v curl >/dev/null 2>&1; then
  echo "系统缺少curl，请先安装curl后重试。" >&2
  exit 1
fi

if ! command -v bash >/dev/null 2>&1; then
  if command -v apk >/dev/null 2>&1; then
    apk add --no-cache bash
  else
    echo "系统缺少bash，请先安装bash后重试。" >&2
    exit 1
  fi
fi

TMP="$(mktemp /tmp/path-socks-install.XXXXXX)"
trap 'rm -f -- "$TMP"' EXIT INT TERM
curl -fL --retry 3 --connect-timeout 10 "$RAW" -o "$TMP"
bash "$TMP"
