#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

APP_DIR=/opt/path-socks
STATE_DIR=/etc/path-socks
TMP_DIR="$(mktemp -d /tmp/path-socks-install.XXXXXX)"
ACTIVATING=0
BACKUP_DIR=""
die() { echo "错误：$*" >&2; exit 1; }
cleanup() {
  local result=$?
  trap - EXIT
  if (( result != 0 && ACTIVATING == 1 )); then
    echo "启用失败，正在恢复本项目的程序和配置：$BACKUP_DIR" >&2
    while IFS= read -r file; do
      if [[ -e "$BACKUP_DIR$file" ]]; then
        cp -a -- "$BACKUP_DIR$file" "$file.rollback"
        mv -f -- "$file.rollback" "$file"
      else
        rm -f -- "$file"
      fi
    done < "$BACKUP_DIR/files"
    if [[ "$INIT_SYSTEM" == systemd ]]; then systemctl daemon-reload; fi
    if [[ -e "$BACKUP_DIR$APP_DIR/path-socks" ]]; then
      restart_core || true
    else
      if [[ "$INIT_SYSTEM" == systemd ]]; then
        systemctl disable --now path-socks 2>/dev/null || true
      else
        rc-service path-socks stop 2>/dev/null || true
        rc-update del path-socks default 2>/dev/null || true
      fi
    fi
    echo "证书申请资料和备份保留在本机；没有操作Nginx。" >&2
  fi
  [[ "$TMP_DIR" == /tmp/path-socks-install.* ]] && rm -rf -- "$TMP_DIR"
  exit "$result"
}
trap cleanup EXIT
[[ "$EUID" -eq 0 ]] || die "请使用root运行"
[[ "$(uname -s)" == Linux ]] || die "仅支持Linux"
case "$(uname -m)" in
  x86_64|amd64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) die "CPU架构不支持" ;;
esac
if command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; then
  INIT_SYSTEM=systemd
elif command -v rc-service >/dev/null; then
  INIT_SYSTEM=openrc
else
  die "需要systemd或OpenRC"
fi
restart_core() {
  if [[ "$INIT_SYSTEM" == systemd ]]; then systemctl restart path-socks; else rc-service path-socks restart; fi
}

DOMAIN="${1:-}"
OLD_DOMAIN=""
[[ ! -s "$STATE_DIR/domain" ]] || OLD_DOMAIN="$(tr -d '\r\n' < "$STATE_DIR/domain")"
if [[ -z "$DOMAIN" ]]; then
  read -r -p "域名（灰云，回车沿用 ${OLD_DOMAIN:-无}）：" DOMAIN </dev/tty
  DOMAIN="${DOMAIN:-$OLD_DOMAIN}"
fi
DOMAIN="$(printf '%s' "$DOMAIN" | tr '[:upper:]' '[:lower:]')"
[[ "$DOMAIN" =~ ^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$ && ${#DOMAIN} -le 253 ]] || die "域名格式不正确"

echo "[1/7] 安装DNS证书组件（不安装、不操作Nginx）"
if command -v apt-get >/dev/null; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y certbot python3-certbot-dns-cloudflare ca-certificates curl python3 iproute2 util-linux
elif command -v dnf >/dev/null; then
  dnf install -y certbot python3-certbot-dns-cloudflare ca-certificates curl python3 iproute util-linux
elif command -v yum >/dev/null; then
  yum install -y certbot python3-certbot-dns-cloudflare ca-certificates curl python3 iproute util-linux
elif command -v apk >/dev/null; then
  apk add --no-cache certbot certbot-dns-cloudflare ca-certificates curl bash python3 iproute2 util-linux
elif command -v zypper >/dev/null; then
  zypper --non-interactive install certbot python3-certbot-dns-cloudflare ca-certificates curl python3 iproute2 util-linux
else
  die "未知包管理器"
fi
certbot plugins | grep -q dns-cloudflare || die "系统软件源没有提供Cloudflare插件，请启用对应软件源后重试"
exec 9>/run/path-socks-maintenance.lock
flock -n 9 || die "其他安装、改端口或续期操作正在运行，请稍后再试"

echo "[2/7] 下载同一提交的组件并校验核心"
RELEASE="$(curl -fsSL --retry 3 --connect-timeout 10 --max-time 60 https://api.github.com/repos/youqishi1/path-socks/commits/main | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])')"
[[ "$RELEASE" =~ ^[0-9a-f]{40}$ ]] || die "无法取得发布版本"
RAW_BASE="https://raw.githubusercontent.com/youqishi1/path-socks/$RELEASE"
for file in path-socks-tls.service path-socks-tls.openrc sbb port.py renew.sh path-socks-renew.service path-socks-renew.timer checksums.txt; do
  curl -fsSL --retry 3 --connect-timeout 10 --max-time 120 "$RAW_BASE/$file" -o "$TMP_DIR/$file"
done
curl -fsSL --retry 3 --connect-timeout 10 --max-time 180 "$RAW_BASE/bin/path-socks-linux-$ARCH" -o "$TMP_DIR/path-socks"
EXPECTED="$(awk -v name="path-socks-linux-$ARCH" '$2==name {print $1}' "$TMP_DIR/checksums.txt")"
ACTUAL="$(sha256sum "$TMP_DIR/path-socks" | awk '{print $1}')"
[[ -n "$EXPECTED" && "$EXPECTED" == "$ACTUAL" ]] || die "核心校验失败"
chmod 0755 "$TMP_DIR/path-socks"
"$TMP_DIR/path-socks" -h >/dev/null 2>&1

echo "[3/7] 选择独立高位端口"
SAVED_PORT=25443
[[ ! -s "$STATE_DIR/port" ]] || SAVED_PORT="$(tr -d '\r\n' < "$STATE_DIR/port")"
PORT_INPUT="${2:-}"
if [[ -z "$PORT_INPUT" ]]; then
  read -r -p "TLS端口（回车使用 $SAVED_PORT，占用则自动换空闲端口）：" PORT_INPUT </dev/tty
fi
PID=0
if [[ "$INIT_SYSTEM" == systemd ]]; then
  PID="$(systemctl show path-socks -p MainPID --value 2>/dev/null || true)"
else
  PID="$(pgrep -x path-socks 2>/dev/null | head -n 1 || true)"
fi
[[ "$PID" =~ ^[0-9]+$ ]] || PID=0
PORT_ARGS=("${PORT_INPUT:-$SAVED_PORT}" --pid "$PID")
[[ -z "$PORT_INPUT" ]] || PORT_ARGS+=(--explicit)
PORT="$(python3 "$TMP_DIR/port.py" "${PORT_ARGS[@]}")"
echo "本次使用 TCP $PORT；不会占用80、443或18080。"

echo "[4/7] Cloudflare DNS验证申请证书（无需开放80/443）"
if ! id path-socks >/dev/null 2>&1; then
  if command -v useradd >/dev/null; then
    getent group path-socks >/dev/null || groupadd --system path-socks
    useradd --system --gid path-socks --home-dir "$APP_DIR" --shell /usr/sbin/nologin path-socks
  else
    addgroup -S path-socks
    adduser -S -D -H -G path-socks -s /sbin/nologin path-socks
  fi
fi
install -d -o root -g path-socks -m 0750 "$STATE_DIR"
install -d -o root -g root -m 0700 "$STATE_DIR/acme" /var/lib/path-socks-acme /var/log/path-socks-acme
echo "Token仅需目标Zone的DNS编辑权限；不要使用Global API Key。"
echo "输入不会显示；已有Token可直接回车沿用。请勿把Token发到聊天中。"
read -r -s -p "Cloudflare API Token：" CF_TOKEN </dev/tty
echo
if [[ -n "$CF_TOKEN" ]]; then
  [[ "$CF_TOKEN" =~ ^[A-Za-z0-9_-]+$ ]] || die "Token格式不正确"
  printf 'dns_cloudflare_api_token = %s\n' "$CF_TOKEN" > "$STATE_DIR/cloudflare.ini.new"
  chmod 0600 "$STATE_DIR/cloudflare.ini.new"
  mv -f "$STATE_DIR/cloudflare.ini.new" "$STATE_DIR/cloudflare.ini"
fi
unset CF_TOKEN
[[ -s "$STATE_DIR/cloudflare.ini" ]] || die "DNS验证需要Cloudflare Token"
chmod 0600 "$STATE_DIR/cloudflare.ini"
CERT_NAME="path-socks-$DOMAIN"
certbot certonly --config-dir "$STATE_DIR/acme" --work-dir /var/lib/path-socks-acme --logs-dir /var/log/path-socks-acme \
  --dns-cloudflare --dns-cloudflare-credentials "$STATE_DIR/cloudflare.ini" --dns-cloudflare-propagation-seconds 60 \
  --cert-name "$CERT_NAME" -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email --keep-until-expiring
cat "$STATE_DIR/acme/live/$CERT_NAME/fullchain.pem" "$STATE_DIR/acme/live/$CERT_NAME/privkey.pem" > "$TMP_DIR/tls.pem"
printf '%s\n' "$PORT" > "$TMP_DIR/port"
printf '%s\n' "$DOMAIN" > "$TMP_DIR/domain"
printf '%s\n' "$INIT_SYSTEM" > "$TMP_DIR/init-system"
if [[ -s "$STATE_DIR/users.db" ]]; then
  cp -a "$STATE_DIR/users.db" "$TMP_DIR/users.db"
else
  for i in $(seq 1 10); do printf '用户%02d|%s\n' "$i" "$(cat /proc/sys/kernel/random/uuid)"; done > "$TMP_DIR/users.db"
fi
"$TMP_DIR/path-socks" -check -port-file "$TMP_DIR/port" -tls-pem "$TMP_DIR/tls.pem" -users "$TMP_DIR/users.db"

echo "[5/7] 备份旧版本并启用独立TLS服务"
install -d -m 0755 /var/backups
BACKUP_DIR="$(mktemp -d /var/backups/path-socks.XXXXXX)"
if [[ "$INIT_SYSTEM" == systemd ]]; then UNIT=/etc/systemd/system/path-socks.service; else UNIT=/etc/init.d/path-socks; fi
FILES=("$APP_DIR/path-socks" "$APP_DIR/port.py" "$APP_DIR/renew.sh" /usr/local/bin/sbb "$UNIT")
for file in domain port tls.pem users.db init-system; do FILES+=("$STATE_DIR/$file"); done
for file in "${FILES[@]}"; do
  printf '%s\n' "$file" >> "$BACKUP_DIR/files"
  if [[ -e "$file" ]]; then
    mkdir -p "$BACKUP_DIR$(dirname "$file")"
    cp -a -- "$file" "$BACKUP_DIR$file"
  fi
done
ACTIVATING=1
install -d -o root -g path-socks -m 0750 "$APP_DIR"
install -o root -g root -m 0755 "$TMP_DIR/path-socks" "$APP_DIR/path-socks.new"
mv -f "$APP_DIR/path-socks.new" "$APP_DIR/path-socks"
install -o root -g root -m 0644 "$TMP_DIR/port.py" "$APP_DIR/port.py"
install -o root -g root -m 0755 "$TMP_DIR/renew.sh" "$APP_DIR/renew.sh"
install -o root -g root -m 0755 "$TMP_DIR/sbb" /usr/local/bin/sbb
for file in domain port tls.pem users.db init-system; do
  install -o root -g path-socks -m 0640 "$TMP_DIR/$file" "$STATE_DIR/$file.new"
  mv -f "$STATE_DIR/$file.new" "$STATE_DIR/$file"
done
if [[ "$INIT_SYSTEM" == systemd ]]; then
  install -m 0644 "$TMP_DIR/path-socks-tls.service" "$UNIT"
  systemctl daemon-reload
  systemctl enable path-socks
else
  install -m 0755 "$TMP_DIR/path-socks-tls.openrc" "$UNIT"
  rc-update add path-socks default
fi
restart_core
HEALTHY=0
for attempt in {1..10}; do
  if [[ "$(curl --noproxy '*' -fsS --max-time 3 --resolve "$DOMAIN:$PORT:127.0.0.1" "https://$DOMAIN:$PORT/health" 2>/dev/null || true)" == ok ]]; then
    HEALTHY=1; break
  fi
  sleep 1
done
(( HEALTHY == 1 )) || die "TLS健康检查失败；端口可能刚被其他程序抢占，将恢复旧版本"
ACTIVATING=0
echo "已启用。旧版本备份（含私密配置，仅root可读）：$BACKUP_DIR"

echo "[6/7] 配置本项目独立证书续期与防火墙"
if [[ "$INIT_SYSTEM" == systemd ]]; then
  install -m 0644 "$TMP_DIR/path-socks-renew.service" /etc/systemd/system/path-socks-renew.service
  install -m 0644 "$TMP_DIR/path-socks-renew.timer" /etc/systemd/system/path-socks-renew.timer
  systemctl daemon-reload
  systemctl enable --now path-socks-renew.timer
else
  [[ -d /etc/periodic/daily ]] || die "当前OpenRC发行版缺少daily目录，请手动配置每天执行 /opt/path-socks/renew.sh"
  install -m 0755 "$TMP_DIR/renew.sh" /etc/periodic/daily/path-socks-renew
  rc-update add crond default
  rc-service crond start
fi
if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then ufw allow "$PORT/tcp"; fi
if command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port="$PORT/tcp"
  firewall-cmd --add-port="$PORT/tcp"
fi
echo "[7/7] 安装完成：请在云厂商安全组放行 TCP $PORT，并保持域名A记录指向本VPS、灰云。"
echo "本版监听IPv4；该域名不要配置指向其他地址的AAAA记录。"
echo "未修改、停止或重载任何Nginx；旧版Nginx配置仍保留，由原管理员按需处理。"
echo "以后输入 sbb 管理；已有客户端务必更新端口。"
/usr/local/bin/sbb show
