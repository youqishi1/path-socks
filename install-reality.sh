#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
[[ $EUID -eq 0 && "$(uname -s)" == Linux ]] || { echo '需要Linux root'; exit 1; }
echo '[1/3] 安装基础组件，不安装Nginx/Certbot'
if command -v apt-get >/dev/null; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y python3 curl ca-certificates iproute2
elif command -v dnf >/dev/null; then
  dnf install -y python3 curl ca-certificates iproute
elif command -v yum >/dev/null; then
  yum install -y python3 curl ca-certificates iproute
elif command -v apk >/dev/null; then
  apk add --no-cache python3 curl ca-certificates iproute2 bash
elif command -v zypper >/dev/null; then
  zypper --non-interactive install python3 curl ca-certificates iproute2
else echo '暂不支持此包管理器'; exit 1; fi
echo '[2/3] 下载管理组件'
tmp="$(mktemp -d /tmp/sbb-reality.XXXXXX)"
trap '[[ "$tmp" == /tmp/sbb-reality.* ]] && rm -rf -- "$tmp"' EXIT
release="$(curl -fsSL --retry 3 --max-time 60 https://api.github.com/repos/youqishi1/path-socks/commits/main | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])')"
[[ "$release" =~ ^[0-9a-f]{40}$ ]]
for file in reality.py port.py sbb path-manager xray-release.json client-helper.html; do
  curl -fsSL --retry 3 --connect-timeout 10 --max-time 120 "https://raw.githubusercontent.com/youqishi1/path-socks/$release/$file" -o "$tmp/$file"
done
echo '[3/3] 配置并验证REALITY'
python3 "$tmp/reality.py" install
