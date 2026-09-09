#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo '请使用root运行'; exit 1; }
echo 'SBB 双方案安装 / 更新'
echo '1. REALITY + 本地住宅链式代理（推荐，不需要域名和CF Token）'
echo '2. Path动态SOCKS5（需要域名和CF Token，保留原操作方式）'
echo '0. 退出'
echo '两套配置独立保留；安装另一套不会自动停止已有服务。'
read -r -p '选择方案 [1]：' choice </dev/tty
case "${choice:-1}" in
  1) script=install-reality.sh ;;
  2) script=install-path.sh ;;
  0) exit 0 ;;
  *) echo '选择无效'; exit 1 ;;
esac
tmp="$(mktemp /tmp/sbb-scheme.XXXXXX)"
trap 'rm -f -- "$tmp"' EXIT
curl -fsSL --retry 3 --connect-timeout 10 --max-time 120 "https://raw.githubusercontent.com/youqishi1/path-socks/main/$script" -o "$tmp"
bash "$tmp"
