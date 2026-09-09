#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
[[ $EUID -eq 0 && "$(uname -s)" == Linux ]] || { echo '需要Linux root'; exit 1; }
if ! command -v python3 >/dev/null; then
  if command -v apt-get >/dev/null; then apt-get update; apt-get install -y python3
  elif command -v apk >/dev/null; then apk add --no-cache python3
  elif command -v dnf >/dev/null; then dnf install -y python3
  elif command -v yum >/dev/null; then yum install -y python3
  elif command -v zypper >/dev/null; then zypper --non-interactive install python3
  else echo '请先安装Python3.9或更新版本'; exit 1; fi
fi
tmp="$(mktemp -d /tmp/sbb-nginx.XXXXXX)"
trap '[[ "$tmp" == /tmp/sbb-nginx.* ]] && rm -rf -- "$tmp"' EXIT
release="$(curl -fsSL --retry 3 --max-time 60 https://api.github.com/repos/youqishi1/path-socks/commits/main | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])')"
[[ "$release" =~ ^[0-9a-f]{40}$ ]]
for file in nginx_mode.py reality.py port.py sbb path-manager renew.sh path-socks-tls.service path-socks-tls.openrc path-socks-renew.service path-socks-renew.timer checksums.txt; do
  curl -fsSL --retry 3 --connect-timeout 10 --max-time 120 "https://raw.githubusercontent.com/youqishi1/path-socks/$release/$file" -o "$tmp/$file"
done
case "$(uname -m)" in x86_64) arch=amd64 ;; aarch64) arch=arm64 ;; *) echo '架构不支持'; exit 1 ;; esac
curl -fsSL --retry 3 --connect-timeout 10 --max-time 180 "https://raw.githubusercontent.com/youqishi1/path-socks/$release/bin/path-socks-linux-$arch" -o "$tmp/path-socks"
python3 "$tmp/nginx_mode.py" "${1:-}"
