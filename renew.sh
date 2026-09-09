#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
exec 9>/run/path-socks-maintenance.lock
flock -n 9 || exit 0
STATE_DIR=/etc/path-socks
DOMAIN="$(tr -d '\r\n' < "$STATE_DIR/domain")"
[[ "$DOMAIN" =~ ^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$ ]] || exit 1
CERT_NAME="path-socks-$DOMAIN"
if [[ "$(cat "$STATE_DIR/transport" 2>/dev/null || true)" == nginx ]]; then
  certbot renew --config-dir /etc/path-socks-nginx/acme --work-dir /var/lib/path-socks-nginx-acme --logs-dir /var/log/path-socks-nginx-acme --cert-name "$CERT_NAME" --quiet
  NGINX="$(cat /etc/path-socks-nginx/nginx-bin)"
  [[ "$NGINX" =~ ^/[A-Za-z0-9_./-]+$ ]] || exit 1
  "$NGINX" -t -c /etc/path-socks-nginx/nginx.conf -p /var/lib/path-socks-nginx
  if [[ "$(cat "$STATE_DIR/init-system")" == systemd ]]; then
    if systemctl is-active --quiet path-socks-nginx; then systemctl reload path-socks-nginx; fi
  else
    if rc-service path-socks-nginx status >/dev/null 2>&1; then rc-service path-socks-nginx reload; fi
  fi
  exit 0
fi
certbot renew --config-dir "$STATE_DIR/acme" --work-dir /var/lib/path-socks-acme --logs-dir /var/log/path-socks-acme \
  --cert-name "$CERT_NAME" --quiet
TEMP="$(mktemp "$STATE_DIR/tls.XXXXXX")"
trap 'rm -f -- "$TEMP"' EXIT
cat "$STATE_DIR/acme/live/$CERT_NAME/fullchain.pem" "$STATE_DIR/acme/live/$CERT_NAME/privkey.pem" > "$TEMP"
/opt/path-socks/path-socks -check -port-file "$STATE_DIR/port" -tls-pem "$TEMP" -users "$STATE_DIR/users.db"
if ! cmp -s "$TEMP" "$STATE_DIR/tls.pem"; then
  chown root:path-socks "$TEMP"
  chmod 0640 "$TEMP"
  mv -f "$TEMP" "$STATE_DIR/tls.pem"
fi
# The gateway reloads the PEM within one minute, without disconnecting clients.
