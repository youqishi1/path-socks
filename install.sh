#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo '请使用root运行'; exit 1; }
echo 'SBB 三选一安装 / 更新'
echo '1. 直连VPS IP（REALITY，显示节点链接/手填参数，不需要自己的域名）'
echo '2. 连接域名（80/443，客户端Path手填住宅SOCKS5，无需CF Token）'
echo '3. 其他 / 备用方案'
echo '0. 退出'
echo 'REALITY独立保留；两种Path模式共用UUID，切换会短暂断开Path连接。'
read -r -p '选择方案 [1]：' choice </dev/tty
case "${choice:-1}" in
  1) script=install-reality.sh ;;
  2) script=install-nginx.sh ;;
  3)
    echo '1. 高位端口Path（需要域名和CF DNS Token）'
    echo '2. REALITY配置助手流程（备用，需要生成文件再导入）'
    echo '0. 退出'
    read -r -p '选择备用方案：' backup </dev/tty
    case "$backup" in
      1) script=install-path.sh ;;
      2) script=install-reality.sh; export SBB_BACKUP_HELPER=1 ;;
      0) exit 0 ;;
      *) echo '选择无效'; exit 1 ;;
    esac ;;
  0) exit 0 ;;
  *) echo '选择无效'; exit 1 ;;
esac
tmp="$(mktemp /tmp/sbb-scheme.XXXXXX)"
trap 'rm -f -- "$tmp"' EXIT
curl -fsSL --retry 3 --connect-timeout 10 --max-time 120 "https://raw.githubusercontent.com/youqishi1/path-socks/main/$script" -o "$tmp"
bash "$tmp"
