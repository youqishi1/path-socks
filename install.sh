#!/usr/bin/env bash
set -Eeuo pipefail

RAW_BASE="https://raw.githubusercontent.com/youqishi1/path-socks/main"
APP_DIR="/opt/path-socks"
STATE_DIR="/etc/path-socks"
USERS_FILE="$STATE_DIR/users.db"
DOMAIN_FILE="$STATE_DIR/domain"
NGINX_PATH_FILE="$STATE_DIR/nginx-site"
INIT_FILE="$STATE_DIR/init-system"
TMP_DIR="$(mktemp -d /tmp/path-socks-install.XXXXXX)"
trap '[[ -n "${TMP_DIR:-}" && "$TMP_DIR" == /tmp/path-socks-install.* ]] && rm -rf -- "$TMP_DIR"' EXIT

die() { echo "错误：$*" >&2; exit 1; }

[[ "$EUID" -eq 0 ]] || die "请使用root用户运行"
[[ "$(uname -s)" == "Linux" ]] || die "目前只支持Linux VPS"

case "$(uname -m)" in
  x86_64|amd64) ARCH="amd64" ;;
  aarch64|arm64) ARCH="arm64" ;;
  *) die "暂不支持该CPU架构：$(uname -m)" ;;
esac

install_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y nginx certbot python3-certbot-nginx ca-certificates curl
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y nginx certbot python3-certbot-nginx ca-certificates curl
  elif command -v yum >/dev/null 2>&1; then
    yum install -y epel-release || true
    yum install -y nginx certbot python3-certbot-nginx ca-certificates curl
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache nginx certbot certbot-nginx ca-certificates curl bash python3
  elif command -v zypper >/dev/null 2>&1; then
    zypper --non-interactive install nginx certbot python3-certbot-nginx ca-certificates curl python3
  else
    die "不支持该系统的软件包管理器"
  fi
}

detect_init() {
  if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
    INIT_SYSTEM="systemd"
  elif command -v rc-service >/dev/null 2>&1; then
    INIT_SYSTEM="openrc"
  else
    die "只支持systemd或OpenRC服务管理器"
  fi
}

service_enable_start() {
  local name="$1"
  if [[ "$INIT_SYSTEM" == "systemd" ]]; then
    systemctl enable --now "$name"
  else
    rc-update add "$name" default >/dev/null 2>&1 || true
    rc-service "$name" restart 2>/dev/null || rc-service "$name" start
  fi
}

service_restart() {
  if [[ "$INIT_SYSTEM" == "systemd" ]]; then systemctl restart "$1"; else rc-service "$1" restart; fi
}

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

echo "[1/9] 自动识别系统并安装组件"
install_packages
detect_init

if [[ -d /etc/nginx/sites-available ]]; then
  NGINX_SITE="/etc/nginx/sites-available/path-socks"
  NGINX_LINK="/etc/nginx/sites-enabled/path-socks"
elif [[ -d /etc/nginx/http.d ]]; then
  NGINX_SITE="/etc/nginx/http.d/path-socks.conf"
  NGINX_LINK=""
else
  install -d -m 0755 /etc/nginx/conf.d
  NGINX_SITE="/etc/nginx/conf.d/path-socks.conf"
  NGINX_LINK=""
fi

echo "[2/9] 检查域名解析"
PUBLIC_IP="$(curl -4fsS --max-time 10 https://api.ipify.org || true)"
DOMAIN_IP="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR==1 {print $1}' || true)"
if [[ -z "$DOMAIN_IP" ]]; then
  DOMAIN_IP="$(python3 - "$DOMAIN" 2>/dev/null <<'PY' || true
import socket, sys
print(socket.gethostbyname(sys.argv[1]))
PY
)"
fi
echo "VPS公网IPv4：${PUBLIC_IP:-获取失败}"
echo "域名解析IPv4：${DOMAIN_IP:-获取失败}"
[[ -n "$DOMAIN_IP" ]] || die "域名暂时没有IPv4解析记录"
if [[ -n "$PUBLIC_IP" && "$PUBLIC_IP" != "$DOMAIN_IP" ]]; then
  die "域名没有直接解析到本VPS；请设置A记录并关闭Cloudflare代理（灰云）"
fi

echo "[3/9] 下载轻量静态核心"
for FILE in nginx.conf.template nginx-tls.conf.template path-socks.service path-socks.openrc sbb; do
  curl -fL --retry 3 --connect-timeout 10 "$RAW_BASE/$FILE" -o "$TMP_DIR/$FILE"
done
curl -fL --retry 3 --connect-timeout 10 "$RAW_BASE/bin/path-socks-linux-$ARCH" -o "$TMP_DIR/path-socks"
curl -fL --retry 3 --connect-timeout 10 "$RAW_BASE/checksums.txt" -o "$TMP_DIR/checksums.txt"
EXPECTED_HASH="$(awk -v name="path-socks-linux-$ARCH" '$2==name {print $1}' "$TMP_DIR/checksums.txt")"
ACTUAL_HASH="$(sha256sum "$TMP_DIR/path-socks" | awk '{print $1}')"
[[ -n "$EXPECTED_HASH" && "$ACTUAL_HASH" == "$EXPECTED_HASH" ]] || die "核心文件校验失败，已停止安装"
chmod 0755 "$TMP_DIR/path-socks"

echo "[4/9] 创建本机私密配置"
if ! id path-socks >/dev/null 2>&1; then
  if command -v useradd >/dev/null 2>&1; then
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin path-socks
  else
    adduser -S -D -H -s /sbin/nologin path-socks
  fi
fi
install -d -o root -g path-socks -m 0750 "$STATE_DIR"
printf '%s\n' "$DOMAIN" > "$DOMAIN_FILE"
printf '%s\n' "$NGINX_SITE" > "$NGINX_PATH_FILE"
printf '%s\n' "$INIT_SYSTEM" > "$INIT_FILE"
chmod 0600 "$DOMAIN_FILE" "$NGINX_PATH_FILE" "$INIT_FILE"

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
chown root:path-socks "$USERS_FILE"
chmod 0640 "$USERS_FILE"

echo "[5/9] 安装服务和sbb管理命令"
install -d -o path-socks -g path-socks -m 0750 "$APP_DIR"
install -o root -g root -m 0755 "$TMP_DIR/path-socks" "$APP_DIR/path-socks"
install -o root -g root -m 0644 "$TMP_DIR/nginx.conf.template" "$APP_DIR/nginx.conf.template"
install -o root -g root -m 0644 "$TMP_DIR/nginx-tls.conf.template" "$APP_DIR/nginx-tls.conf.template"
install -o root -g root -m 0755 "$TMP_DIR/sbb" /usr/local/bin/sbb
if [[ -f /usr/local/bin/sb ]] && grep -q 'youqishi1/path-socks' /usr/local/bin/sb 2>/dev/null; then
  rm -f /usr/local/bin/sb
fi

if [[ "$INIT_SYSTEM" == "systemd" ]]; then
  install -m 0644 "$TMP_DIR/path-socks.service" /etc/systemd/system/path-socks.service
  systemctl daemon-reload
else
  install -m 0755 "$TMP_DIR/path-socks.openrc" /etc/init.d/path-socks
fi

# 清理旧workerd版本，降低磁盘和常驻内存占用。
if [[ -d "$APP_DIR/node_modules" ]]; then rm -rf -- "$APP_DIR/node_modules"; fi
rm -f "$APP_DIR/package.json" "$APP_DIR/package-lock.json" "$APP_DIR/config.capnp" "$APP_DIR/config.capnp.template" "$APP_DIR/worker.js"

service_enable_start path-socks
sleep 2
curl -fsS --max-time 5 http://127.0.0.1:18080/health >/dev/null || die "核心没有正常启动"

echo "[6/9] 配置Nginx"
sed "s/__DOMAIN__/$DOMAIN/g" "$APP_DIR/nginx.conf.template" > "$NGINX_SITE"
if [[ -n "$NGINX_LINK" ]]; then ln -sfn "$NGINX_SITE" "$NGINX_LINK"; fi
nginx -t
service_enable_start nginx
service_restart nginx

echo "[7/9] 申请TLS证书"
certbot certonly --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email
sed "s/__DOMAIN__/$DOMAIN/g" "$APP_DIR/nginx-tls.conf.template" > "$NGINX_SITE"
nginx -t
service_restart nginx

echo "[8/9] 防火墙和网络稳定性优化"
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
  ufw allow 80/tcp
  ufw allow 443/tcp
fi
if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  firewall-cmd --permanent --add-service=http
  firewall-cmd --permanent --add-service=https
  firewall-cmd --reload
fi
if command -v getenforce >/dev/null 2>&1 && [[ "$(getenforce)" == "Enforcing" ]] && command -v setsebool >/dev/null 2>&1; then
  setsebool -P httpd_can_network_connect 1
fi
if modprobe tcp_bbr 2>/dev/null; then
  install -d -m 0755 /etc/modules-load.d /etc/sysctl.d
  printf '%s\n' tcp_bbr > /etc/modules-load.d/path-socks-bbr.conf
  printf '%s\n' 'net.core.default_qdisc=fq' 'net.ipv4.tcp_congestion_control=bbr' 'net.core.somaxconn=4096' 'net.ipv4.tcp_keepalive_time=300' > /etc/sysctl.d/99-path-socks.conf
  sysctl --system >/dev/null 2>&1 || true
fi

echo "[9/9] 完成"
echo
echo "以后登录SSH输入以下命令即可管理："
echo
echo "    sbb"
echo
/usr/local/bin/sbb show
