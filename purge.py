#!/usr/bin/env python3
"""Explicit, irreversible project purge. Shared packages and unknown rules stay."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

DIRECTORIES = (
    '/opt/path-socks','/etc/path-socks','/opt/sbb-reality','/etc/sbb-reality',
    '/etc/path-socks-nginx','/var/lib/path-socks-nginx',
    '/var/lib/path-socks-acme','/var/log/path-socks-acme',
    '/var/lib/path-socks-nginx-acme','/var/log/path-socks-nginx-acme','/var/log/path-socks-nginx')
SERVICES = ('path-socks-renew.timer','path-socks-renew.service','path-socks-nginx.service','path-socks.service','sbb-reality.service')
FILES = ('/usr/local/bin/sbb','/etc/periodic/daily/path-socks-renew','/run/path-socks-nginx.pid',
    '/var/lib/systemd/timers/stamp-path-socks-renew.timer',
    '/etc/init.d/path-socks','/etc/init.d/path-socks-nginx','/etc/init.d/sbb-reality',
    '/root/path-socks-client.txt') + tuple('/etc/systemd/system/'+name for name in SERVICES)
BACKUP = re.compile(r'(?:path-socks\.|path-socks-443\.|sbb-reality\.|sbb-reality-repair\.|sbb-reality-uninstall\.|sbb-manager\.)[A-Za-z0-9_-]+$')
LOG = re.compile(r'(?:path-socks|sbb-reality)\.log(?:\.\d+(?:\.gz)?)?$')


def command(*args, check=True):
    return subprocess.run(list(map(str,args)),check=check,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)


def ask(text):
    print(text, end='',flush=True)
    with open('/dev/tty') as tty:return tty.readline().strip()


def inventory(root=Path('/')):
    def path(name):return root/name.lstrip('/')
    result=[path(name) for name in DIRECTORIES+FILES]
    for directory,pattern in (('/var/backups',BACKUP),('/var/log',LOG)):
        base=path(directory)
        if base.is_symlink():raise RuntimeError('拒绝扫描符号链接目录：'+str(base))
        if base.is_dir():result.extend(p for p in base.iterdir() if pattern.fullmatch(p.name))
    # Only unlink named service registration links; never traverse their targets.
    base=path('/etc/systemd/system')
    if base.is_dir():
        for folder in base.iterdir():
            if folder.name.endswith(('.wants','.requires')) and folder.is_dir() and not folder.is_symlink():
                result.extend(folder/name for name in SERVICES if (folder/name).is_symlink())
    base=path('/etc/runlevels')
    if base.is_dir():
        for folder in base.iterdir():
            if folder.is_dir() and not folder.is_symlink():
                result.extend(folder/name for name in ('path-socks','path-socks-nginx','sbb-reality') if (folder/name).is_symlink())
    return sorted(set(p for p in result if p.exists() or p.is_symlink()),key=str)


def validate(targets):
    for p in targets:
        if p.parent.resolve()!=p.parent:raise RuntimeError('父目录含符号链接，停止：'+str(p))
        if p.is_symlink():continue  # Remove only the link itself.
        if p.is_dir():
            for base,dirs,_ in os.walk(p,followlinks=False):
                if os.path.ismount(base):raise RuntimeError('项目目录内含挂载点，停止：'+base)
                for name in dirs:
                    child=Path(base)/name
                    if not child.is_symlink() and os.path.ismount(child):raise RuntimeError('项目目录内含挂载点，停止：'+str(child))


def remove(targets):
    validate(targets)
    for p in targets:
        if p.is_symlink() or p.is_file():p.unlink()
        elif p.is_dir():shutil.rmtree(p)


def preflight_services():
    # Legacy shared Nginx needs a separate ownership-aware migration, not blind deletion.
    for name in ('/etc/nginx/sites-enabled/path-socks','/etc/nginx/sites-available/path-socks'):
        p=Path(name)
        if p.exists() or p.is_symlink():raise RuntimeError('发现旧版共享Nginx站点：'+name+'。请先确认其引用和证书归属，本次未删除任何文件。')
    systemd=Path('/run/systemd/system').is_dir() and shutil.which('systemctl')
    if systemd:
        for name in SERVICES:
            fragment=command('systemctl','show',name,'-p','FragmentPath','--value',check=False).stdout.strip()
            if fragment and fragment!='/etc/systemd/system/'+name:raise RuntimeError('服务来源不符合本项目安装位置：'+name)
    elif not shutil.which('rc-service'):raise RuntimeError('不支持此初始化系统')
    for name in SERVICES:
        p=Path('/etc/systemd/system')/name
        if not p.exists():continue
        if p.is_symlink():raise RuntimeError('服务文件为符号链接，需人工确认：'+str(p))
        text=p.read_text()
        marker='/opt/sbb-reality/xray' if name=='sbb-reality.service' else '/etc/path-socks-nginx/nginx.conf' if name=='path-socks-nginx.service' else 'Daily Path SOCKS certificate renewal check' if name.endswith('.timer') else '/opt/path-socks/'
        if marker not in text:raise RuntimeError('服务内容与本项目不符：'+name)
    if not systemd:
        for name,marker in (('path-socks','/opt/path-socks/'),('path-socks-nginx','/etc/path-socks-nginx/nginx.conf'),('sbb-reality','/opt/sbb-reality/')):
            p=Path('/etc/init.d')/name
            if p.exists() and (p.is_symlink() or marker not in p.read_text()):raise RuntimeError('OpenRC服务来源需人工确认：'+name)
    return bool(systemd)


def stop_services(systemd):
    if systemd:
        for name in SERVICES:
            if (Path('/etc/systemd/system')/name).exists():
                command('systemctl','stop',name)
                command('systemctl','disable',name,check=False)
                if command('systemctl','is-active','--quiet',name,check=False).returncode==0:raise RuntimeError('服务未停止：'+name)
    else:
        for name in ('path-socks-nginx','path-socks','sbb-reality'):
            if (Path('/etc/init.d')/name).exists():
                if command('rc-service',name,'status',check=False).returncode==0:command('rc-service',name,'stop')
                command('rc-update','del',name,'default',check=False)


def remove_accounts():
    import pwd,grp
    remaining=[]
    for name in ('path-socks','sbb-reality'):
        try:account=pwd.getpwnam(name)
        except KeyError:account=None
        if account:
            try:group=grp.getgrgid(account.pw_gid)
            except KeyError:group=None
            if account.pw_uid==0 or not account.pw_shell.endswith(('/nologin','/false')) or not group or group.gr_name!=name or any(u.pw_name!=name and u.pw_gid==account.pw_gid for u in pwd.getpwall()):
                remaining.append('账号归属异常，保留：'+name);continue
            tool='userdel' if shutil.which('userdel') else 'deluser'
            if command(tool,name,check=False).returncode:remaining.append('账号仍被使用，保留：'+name);continue
        try:group=grp.getgrnam(name)
        except KeyError:continue
        if group.gr_mem or any(u.pw_gid==group.gr_gid for u in pwd.getpwall()):remaining.append('组仍被使用，保留：'+name);continue
        tool='groupdel' if shutil.which('groupdel') else 'delgroup'
        if command(tool,name,check=False).returncode:remaining.append('未能删除组：'+name)
    return remaining


def main():
    import fcntl
    from contextlib import ExitStack
    if sys.platform!='linux' or os.geteuid()!=0:raise RuntimeError('仅支持Linux root')
    os.umask(0o077)
    with ExitStack() as stack:
        for name in ('/run/sbb-reality.lock','/run/path-socks-maintenance.lock'):
            lock=stack.enter_context(open(name,'a'))
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        systemd=preflight_services()
        targets=inventory();validate(targets)
        print('彻底卸载整个SBB项目：以下文件/目录将永久删除，包括Token、私钥、证书、日志和历史备份：')
        for target in targets:print('  '+str(target))
        print('会停止并移除本项目两套代理、续期任务及sbb命令，尝试删除专用账号。不会创建新备份。')
        print('保留共享Nginx/Certbot/系统软件、系统journal，以及未标明归属的防火墙/云安全组规则。')
        print('独立目录以外的历史共享证书、手动下载的源码和本地客户端文件不在自动删除范围。')
        if ask('确认继续请输入 yes：')!='yes':print('已取消，未卸载');return
        if ask('不可恢复！最后确认请输入 PURGE-SBB：')!='PURGE-SBB':print('已取消，未卸载');return
        stop_services(systemd)
        # Stop untracked survivors from being mistaken for a successfully removed service.
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit():continue
            try:
                exe=str((proc/'exe').readlink())
                cmd=(proc/'cmdline').read_bytes()
            except OSError:continue
            if exe.startswith(('/opt/path-socks/','/opt/sbb-reality/')) or (b'nginx' in cmd and b'/etc/path-socks-nginx/nginx.conf' in cmd):
                raise RuntimeError('仍有项目进程运行，停止删除。请先确认进程PID '+proc.name)
        # Disable may already have removed some registration links.
        remove([p for p in targets if p.exists() or p.is_symlink()])
        if systemd:
            command('systemctl','daemon-reload')
            for name in SERVICES:command('systemctl','reset-failed',name,check=False)
        remaining=remove_accounts()
        leftovers=inventory()
        for path in leftovers:remaining.append('残留：'+str(path))
        for name in ('/run/sbb-reality.lock','/run/path-socks-maintenance.lock'):Path(name).unlink(missing_ok=True)
        if remaining:raise RuntimeError('主要文件已删除，但未完全清理：'+'；'.join(remaining))
        print('本项目受管文件、服务、专用账号及历史备份已删除，不可由本工具恢复。')
        print('共享软件、系统journal和无归属标记的网络规则未动；云安全组规则请在云控制台确认后清理。')


if __name__=='__main__':
    try:main()
    except Exception as error:
        print('卸载停止：',str(error) if isinstance(error,RuntimeError) else type(error).__name__+'；请检查权限、服务状态或是否有其他管理操作正在运行')
        sys.exit(1)
