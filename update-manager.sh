#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
[[ $EUID -eq 0 ]] || { echo '请使用root'; exit 1; }
command -v python3 >/dev/null || { echo '请先安装Python3.9以上'; exit 1; }
exec 8>/run/sbb-reality.lock
flock -n 8 || { echo 'REALITY管理正在运行，请先退出其他sbb菜单'; exit 1; }
exec 9>/run/path-socks-maintenance.lock
flock -n 9 || { echo 'Path维护正在运行，请稍后重试'; exit 1; }
tmp="$(mktemp -d /tmp/sbb-manager.XXXXXX)"
trap '[[ "$tmp" == /tmp/sbb-manager.* ]] && rm -rf -- "$tmp"' EXIT
release="$(curl -fsSL --retry 3 --max-time 60 https://api.github.com/repos/youqishi1/path-socks/commits/main | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])')"
[[ "$release" =~ ^[0-9a-f]{40}$ ]]
for file in reality.py port.py sbb path-manager; do
  curl -fsSL --retry 3 --connect-timeout 10 --max-time 120 "https://raw.githubusercontent.com/youqishi1/path-socks/$release/$file" -o "$tmp/$file"
done
bash -n "$tmp/sbb"
bash -n "$tmp/path-manager"
python3 - "$tmp" <<'PY'
import os, sys, tempfile
from pathlib import Path
if sys.version_info<(3,9):raise SystemExit('需要Python3.9以上')
stage=Path(sys.argv[1]);sys.path.insert(0,str(stage))
for name in ('reality.py','port.py'):compile((stage/name).read_text(),name,'exec')
from reality import atomic
targets={Path('/usr/local/bin/sbb'):stage/'sbb'}
for root,names in ((Path('/opt/sbb-reality'),('reality.py','port.py')),(Path('/opt/path-socks'),('sbb-path',))):
    if root.is_symlink() or root.resolve()!=root:raise SystemExit('拒绝异常程序目录')
    if root.is_dir():
        for name in names:targets[root/name]=stage/('path-manager' if name=='sbb-path' else name)
if len(targets)==1:raise SystemExit('未找到已有安装，请先安装方案1或2')
for target in targets:
    if target.is_symlink() or target.resolve()!=target:raise SystemExit('拒绝符号链接管理文件')
Path('/var/backups').mkdir(exist_ok=True)
backup=Path(tempfile.mkdtemp(prefix='sbb-manager.',dir='/var/backups'));backup.chmod(0o700)
old={p:(p.read_bytes(),p.stat().st_mode&0o777,p.stat().st_gid) if p.exists() else None for p in targets}
for p,value in old.items():
    if value:
        dest=backup/str(p).lstrip('/');dest.parent.mkdir(parents=True,exist_ok=True);atomic(dest,value[0],0o600)
try:
    for target,source in targets.items():
        value=old[target];atomic(target,source.read_bytes(),value[1] if value else 0o755,value[2] if value else 0)
except Exception:
    for p,value in old.items():
        if value:atomic(p,*value)
        else:p.unlink(missing_ok=True)
    raise
print('管理工具升级完成；未重启服务，未修改端口/密钥/UUID。备份：',backup)
print('输入sbb管理。IP诊断：sbb reality diagnose；IP修复：sbb reality repair；IP卸载：sbb reality uninstall')
PY
