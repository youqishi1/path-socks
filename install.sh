#!/usr/bin/env bash
set -Eeuo pipefail

RAW_BASE="https://raw.githubusercontent.com/youqishi1/path-socks/main"
APP_DIR="/opt/path-socks"
STATE_DIR="/etc/path-socks"
USERS_FILE="$STATE_DIR/users.db"
DOMAIN_FILE="$STATE_DIR/domain"
NGINX_SITE="/etc/nginx/sites-available/path-socks"
TMP_DIR="$(mktemp -d /tmp/path-socks-install.XXXXXX)"
trap '[[ -n "${TMP_DIR:-}" && "$TMP_DIR" == /tmp/path-socks-install.* ]] && rm -rf -- "$TMP_DIR"' EXIT

die() { echo "错误：$*" >&2; exit 1; }

[[ "$EUID" -eq 0 ]] || die "请使用root用户运行"

DOMAIN="${1:-}"
if [[ -z "$DOMAIN" && -s "$DOMAIN_FILE" ]]; then
  OLD_DOMAIN="$(tr -d '\r\n' < "$DOMAIN_FILE")"
  read -r -p "请输入域名（直接回车继续使用 $OLD_DOMAIN）：" DOMAIN </dev/tty
  DOMAIN="${DOMAIN:-$OLD_DOMAIN}"
elif [[ -z "$DOMAIN" ]]; then
  read -r -p "请输入已经解析到本VPS的域名：" DOMAIN </dev/tty
fi
DOMAIN="$(printf '%s' "$DOMAIN" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
[[ "$DOMAIN" =~ ^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$ ]] || die "域名格式不正确"

echo "[1/9] 安装系统组件"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y nginx certbot python3-certbot-nginx nodejs npm ca-certificates curl

echo "[2/9] 检查域名解析"
PUBLIC_IP="$(curl -4fsS --max-time 10 https://api.ipify.org || true)"
DOMAIN_IP="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1 {print $1}' || true)"
echo "VPS公网IPv4：${PUBLIC_IP:-获取失败}"
echo "域名解析IPv4：${DOMAIN_IP:-获取失败}"
[[ -n "$DOMAIN_IP" ]] || die "域名暂时没有IPv4解析记录"
if [[ -n "$PUBLIC_IP" && "$PUBLIC_IP" != "$DOMAIN_IP" ]]; then
  die "域名没有直接解析到本VPS；请在Cloudflare设置A记录并关闭代理（灰云）"
fi

echo "[3/9] 下载程序文件"
for FILE in worker.js config.capnp.template nginx.conf.template nginx-tls.conf.template path-socks.service sb; do
  curl -fL --retry 3 --connect-timeout 10 "$RAW_BASE/$FILE" -o "$TMP_DIR/$FILE"
done

echo "[4/9] 创建本机私密配置"
install -d -m 0700 "$STATE_DIR"
printf '%s\n' "$DOMAIN" > "$DOMAIN_FILE"
chmod 0600 "$DOMAIN_FILE"

if [[ ! -s "$USERS_FILE" ]]; then
  OLD_UUIDS=""
  if [[ -s "$APP_DIR/config.capnp" ]]; then
    OLD_UUIDS="$(sed -n 's/.*UUIDS", text = "\([^"]*\)".*/\1/p' "$APP_DIR/config.capnp" | head -n 1)"
  fi
  : > "$USERS_FILE"
  if [[ -n "$OLD_UUIDS" ]]; then
    IFS=',' read -ra UUID_ARRAY <<< "$OLD_UUIDS"
    for i in "${!UUID_ARRAY[@]}"; do
      printf '用户%02d|%s\n' "$((i + 1))" "${UUID_ARRAY[$i]}" >> "$USERS_FILE"
    done
  else
    for i in $(seq 1 10); do
      printf '用户%02d|%s\n' "$i" "$(cat /proc/sys/kernel/random/uuid)" >> "$USERS_FILE"
    done
  fi
fi
chmod 0600 "$USERS_FILE"

echo "[5/9] 安装网关"
id path-socks >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin path-socks
install -d -o path-socks -g path-socks -m 0750 "$APP_DIR"
install -o path-socks -g path-socks -m 0640 "$TMP_DIR/worker.js" "$APP_DIR/worker.js"
install -o path-socks -g path-socks -m 0640 "$TMP_DIR/config.capnp.template" "$APP_DIR/config.capnp.template"
install -o root -g root -m 0644 "$TMP_DIR/nginx.conf.template" "$APP_DIR/nginx.conf.template"
install -o root -g root -m 0644 "$TMP_DIR/nginx-tls.conf.template" "$APP_DIR/nginx-tls.conf.template"
install -m 0644 "$TMP_DIR/path-socks.service" /etc/systemd/system/path-socks.service
install -m 0755 "$TMP_DIR/sb" /usr/local/bin/sb

UUIDS="$(cut -d '|' -f2 "$USERS_FILE" | paste -sd, -)"
[[ -n "$UUIDS" ]] || die "没有可用UUID"
sed "s/__UUIDS__/$UUIDS/g" "$APP_DIR/config.capnp.template" > "$APP_DIR/config.capnp"
chown path-socks:path-socks "$APP_DIR/config.capnp"
chmod 0640 "$APP_DIR/config.capnp"

cd "$APP_DIR"
npm install --omit=dev --no-audit --no-fund workerd@1.20260904.1
chown -R path-socks:path-socks "$APP_DIR"
systemctl daemon-reload
systemctl enable --now path-socks.service
sleep 2
curl -fsS --max-time 5 http://127.0.0.1:18080/health >/dev/null || die "网关没有正常启动，请运行 journalctl -u path-socks -n 50 查看"

echo "[6/9] 配置Nginx"
sed "s/__DOMAIN__/$DOMAIN/g" "$APP_DIR/nginx.conf.template" > "$NGINX_SITE"
ln -sfn "$NGINX_SITE" /etc/nginx/sites-enabled/path-socks
nginx -t
systemctl reload nginx

echo "[7/9] 申请TLS证书"
certbot certonly --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email
sed "s/__DOMAIN__/$DOMAIN/g" "$APP_DIR/nginx-tls.conf.template" > "$NGINX_SITE"
nginx -t
systemctl reload nginx

echo "[8/9] 网络优化"
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
  ufw allow 80/tcp
  ufw allow 443/tcp
fi
if modprobe tcp_bbr 2>/dev/null; then
  printf '%s\n' tcp_bbr > /etc/modules-load.d/path-socks-bbr.conf
  printf '%s\n' 'net.core.default_qdisc=fq' 'net.ipv4.tcp_congestion_control=bbr' > /etc/sysctl.d/99-path-socks-bbr.conf
  sysctl --system >/dev/null
fi

echo "[9/9] 完成"
echo
echo "安装成功。以后在SSH中输入下面两个字母即可管理："
echo
echo "    sb"
echo
/usr/local/bin/sb show
