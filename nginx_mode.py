#!/usr/bin/env python3
"""Original 80/443 Path mode, with isolated Nginx configuration and HTTP-01."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

from reality import atomic, firewall, prompt, run, system
from port import available, choose

HERE=Path(__file__).resolve().parent
APP=Path('/opt/path-socks')
STATE=Path('/etc/path-socks')
EDGE=Path('/etc/path-socks-nginx')
PREFIX=Path('/var/lib/path-socks-nginx')
WEBROOT=PREFIX/'www'


def svc(name,action,check=True):
    return run('systemctl',action,name,check=check) if system()=='systemd' else run('rc-service',name,action,check=check)


def active(name):
    args=['systemctl','is-active','--quiet',name] if system()=='systemd' else ['rc-service',name,'status']
    return run(*args,capture=True,check=False).returncode==0


def enable(name):
    run('systemctl','enable',name) if system()=='systemd' else run('rc-update','add',name,'default')


def pid(name):
    if system()=='systemd':
        value=run('systemctl','show',name,'-p','MainPID','--value',capture=True,check=False).stdout.strip()
    elif name=='path-socks-nginx':
        path=Path('/run/path-socks-nginx.pid');value=path.read_text().strip() if path.exists() else '0'
    else:
        value=run('pgrep','-x','path-socks',capture=True,check=False).stdout.strip().split('\n')[0]
    return int(value) if value.isdigit() else 0


def check_ports():
    """Allow only empty ports or listening sockets owned by our exact Nginx master."""
    master=pid('path-socks-nginx')
    owned=set()
    try:
        args=Path(f'/proc/{master}/cmdline').read_bytes()
        if master>1 and b'/etc/path-socks-nginx/nginx.conf' in args and b'nginx' in args:
            for entry in Path(f'/proc/{master}/fd').iterdir():
                try:owned.add(entry.readlink().name)
                except OSError:pass
    except OSError:pass
    for port in (80,443):
        if available(port):continue
        listeners=[]
        for name in ('tcp','tcp6'):
            path=Path('/proc/net')/name
            if not path.exists():continue
            for line in path.read_text().splitlines()[1:]:
                fields=line.split()
                if fields[3]=='0A' and int(fields[1].split(':')[1],16)==port:listeners.append(f'socket:[{fields[9]}]')
        if not listeners or not all(item in owned for item in listeners):
            raise RuntimeError(f'TCP {port}已被其他服务占用，已停止；不会关闭占用者。可选择方案1或3，或先由管理员处理原服务。')


def packages():
    fresh=shutil.which('nginx') is None
    if fresh and Path('/etc/nginx').exists():raise RuntimeError('检测到已有Nginx配置但找不到程序；请先由管理员确认，不自动接管')
    common=['certbot','curl','ca-certificates','python3','util-linux']
    if fresh:common.append('nginx')
    if shutil.which('apt-get'):
        run('apt-get','update');run('env','DEBIAN_FRONTEND=noninteractive','apt-get','install','-y',*common,'iproute2')
    elif shutil.which('dnf'):run('dnf','install','-y',*common,'iproute')
    elif shutil.which('yum'):run('yum','install','-y',*common,'iproute')
    elif shutil.which('apk'):run('apk','add','--no-cache',*common,'bash','iproute2')
    elif shutil.which('zypper'):run('zypper','--non-interactive','install',*common,'iproute2')
    else:raise RuntimeError('不支持此包管理器')
    if fresh:
        # Only the default service just created by this installation is stopped.
        print('停用本次新安装包自动启动的默认Nginx，改由本项目独立实例提供80/443。')
        if system()=='systemd':run('systemctl','disable','--now','nginx')
        else:svc('nginx','stop',check=False);run('rc-update','del','nginx','default',check=False)
    check_ports()


def nginx_config(domain,backend,tls=False):
    cert=EDGE/'acme/live'/('path-socks-'+domain)
    text=f'''# SBB managed standalone Nginx; does not include system sites.
user path-socks;
worker_processes auto;
daemon off;
pid /run/path-socks-nginx.pid;
error_log /var/log/path-socks-nginx/error.log warn;
events {{ worker_connections 4096; }}
http {{
    access_log off;
    client_body_temp_path /var/lib/path-socks-nginx/body;
    proxy_temp_path /var/lib/path-socks-nginx/proxy;
    server {{
        listen 80;
        server_name {domain};
        location ^~ /.well-known/acme-challenge/ {{ root {WEBROOT}; default_type text/plain; }}
        location / {{ return 404; }}
    }}
'''
    if tls:text+=f'''
    server {{
        listen 443 ssl;
        server_name {domain};
        ssl_certificate {cert}/fullchain.pem;
        ssl_certificate_key {cert}/privkey.pem;
        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_session_cache shared:SBB:10m;
        ssl_session_timeout 1d;
        error_log /var/log/path-socks-nginx/proxy-error.log crit;
        location / {{
            proxy_pass http://127.0.0.1:{backend};
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
            proxy_buffering off;
            proxy_request_buffering off;
            proxy_connect_timeout 10s;
            proxy_read_timeout 86400s;
            proxy_send_timeout 86400s;
            proxy_socket_keepalive on;
        }}
    }}
'''
    return text+'}\n'


def nginx_unit(binary):
    args=f'-c {EDGE}/nginx.conf -p {PREFIX}'
    if system()=='systemd':return f'''[Unit]
Description=SBB Path standalone Nginx 80/443
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
ExecStart={binary} {args}
ExecReload=/bin/kill -s HUP $MAINPID
Restart=on-failure
RestartSec=3
KillSignal=SIGQUIT
TimeoutStopSec=15
LimitNOFILE=131072
PrivateTmp=true
ProtectHome=true
[Install]
WantedBy=multi-user.target
'''
    return f'''#!/sbin/openrc-run
name="path-socks-nginx"
command="{binary}"
command_args="{args}"
supervisor="supervise-daemon"
respawn_delay=3
respawn_max=0
depend() {{ need net; }}
reload() {{ "{binary}" {args} -t && "{binary}" {args} -s reload; }}
'''


def certbot_args():
    return ['--config-dir',str(EDGE/'acme'),'--work-dir','/var/lib/path-socks-nginx-acme','--logs-dir','/var/log/path-socks-nginx-acme']


def install(domain=''):
    import grp
    import pwd
    print('[1/6] 检查80/443占用；本模式不需要Cloudflare Token')
    check_ports()
    old_domain=(STATE/'domain').read_text().strip() if (STATE/'domain').exists() else ''
    domain=(domain or prompt('域名（A记录指向本VPS，灰云）',old_domain)).lower()
    if not re.fullmatch(r'([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}',domain):raise ValueError('域名格式不正确')
    public=run('curl','-4fsS','--max-time','15','https://api.ipify.org',capture=True).stdout.strip()
    addresses={item[4][0] for item in socket.getaddrinfo(domain,80,socket.AF_INET,socket.SOCK_STREAM)}
    if public not in addresses:raise RuntimeError('域名A记录未直接指向本VPS；请关闭橙云并核对解析')
    print('请先在云安全组放行TCP 80和443；本模式仅IPv4，不要保留错误的AAAA记录。')
    print('[2/6] 安装Nginx和HTTP证书组件')
    packages()
    binary=shutil.which('nginx')
    if not binary or not re.fullmatch(r'/[A-Za-z0-9_./-]+',binary):raise RuntimeError('Nginx程序路径不支持')
    arch={'x86_64':'amd64','aarch64':'arm64'}.get(os.uname().machine)
    expected={row.split()[1]:row.split()[0] for row in (HERE/'checksums.txt').read_text().splitlines()}
    if not arch or hashlib.sha256((HERE/'path-socks').read_bytes()).hexdigest()!=expected.get('path-socks-linux-'+arch):raise RuntimeError('代理核心SHA256校验失败')
    (HERE/'path-socks').chmod(0o755)
    try:pwd.getpwnam('path-socks')
    except KeyError:
        if shutil.which('useradd'):
            try:grp.getgrnam('path-socks')
            except KeyError:run('groupadd','--system','path-socks')
            run('useradd','--system','--gid','path-socks','--no-create-home','--shell','/usr/sbin/nologin','path-socks')
        else:run('addgroup','-S','path-socks');run('adduser','-S','-D','-H','-G','path-socks','-s','/sbin/nologin','path-socks')
    gid=grp.getgrnam('path-socks').gr_gid
    for directory in (APP,STATE,EDGE):
        if directory.is_symlink():raise RuntimeError('拒绝符号链接目录')
        directory.mkdir(exist_ok=True,parents=True);directory.chmod(0o750);os.chown(directory,0,gid)
    for directory in (PREFIX,WEBROOT,WEBROOT/'.well-known',WEBROOT/'.well-known/acme-challenge',Path('/var/log/path-socks-nginx')):
        directory.mkdir(exist_ok=True,parents=True);directory.chmod(0o755)
    for name in ('body','proxy'):
        path=PREFIX/name;path.mkdir(exist_ok=True);os.chown(path,pwd.getpwnam('path-socks').pw_uid,gid);path.chmod(0o700)
    backend=choose(int((EDGE/'backend-port').read_text()) if (EDGE/'backend-port').exists() else 28180,pid('path-socks'))
    users=(STATE/'users.db').read_bytes() if (STATE/'users.db').exists() else ''.join(f'用户{i:02}|{uuid.uuid4()}\n' for i in range(1,11)).encode()
    if not users.strip():raise RuntimeError('已有用户文件为空，请先检查，未覆盖UUID')
    init=system();unit_dir=Path('/etc/systemd/system' if init=='systemd' else '/etc/init.d')
    core_unit=unit_dir/('path-socks.service' if init=='systemd' else 'path-socks')
    edge_unit=unit_dir/('path-socks-nginx.service' if init=='systemd' else 'path-socks-nginx')
    core=(HERE/('path-socks-tls.service' if init=='systemd' else 'path-socks-tls.openrc')).read_text().replace('-port-file /etc/path-socks/port -tls-pem /etc/path-socks/tls.pem',f'-listen 127.0.0.1:{backend}')
    files={APP/'path-socks':(HERE/'path-socks').read_bytes(),APP/'sbb-path':(HERE/'path-manager').read_bytes(),APP/'renew.sh':(HERE/'renew.sh').read_bytes(),APP/'port.py':(HERE/'port.py').read_bytes(),Path('/usr/local/bin/sbb'):(HERE/'sbb').read_bytes(),STATE/'users.db':users,STATE/'domain':domain.encode(),STATE/'port':b'443\n',STATE/'transport':b'nginx\n',STATE/'init-system':init.encode(),EDGE/'backend-port':str(backend).encode(),EDGE/'nginx-bin':binary.encode(),core_unit:core.encode(),edge_unit:nginx_unit(binary).encode(),EDGE/'nginx.conf':b''}
    backups={path:path.read_bytes() if path.exists() else None for path in files}
    modes={path:path.stat().st_mode&0o777 for path in files if path.exists()}
    Path('/var/backups').mkdir(exist_ok=True)
    backup=Path(tempfile.mkdtemp(prefix='path-socks-443.',dir='/var/backups'))
    for path,data in backups.items():
        if data is not None:
            dest=backup/str(path).lstrip('/');dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(data)
    was_core=active('path-socks');was_edge=active('path-socks-nginx');changed_core=False
    def write(path,data):
        mode=0o755 if path in (APP/'path-socks',APP/'sbb-path',APP/'renew.sh',Path('/usr/local/bin/sbb')) or (init=='openrc' and path in (core_unit,edge_unit)) else 0o640
        atomic(path,data,mode,gid if path.is_relative_to(APP) or path.is_relative_to(STATE) or path.is_relative_to(EDGE) else None)
    try:
        print('[3/6] 启用独立HTTP验证入口')
        write(edge_unit,files[edge_unit])
        # Preserve an existing 443 listener during same-domain renewals/upgrades.
        has_cert=(EDGE/'acme/live'/('path-socks-'+domain)/'fullchain.pem').exists()
        write(EDGE/'nginx.conf',nginx_config(domain,backend,has_cert))
        run(binary,'-t','-c',EDGE/'nginx.conf','-p',PREFIX)
        if init=='systemd':run('systemctl','daemon-reload')
        enable('path-socks-nginx');svc('path-socks-nginx','restart')
        firewall(80);firewall(443)
        print('[4/6] HTTP-01申请证书，不调用Cloudflare API')
        run('certbot','certonly',*certbot_args(),'--webroot','-w',WEBROOT,'--cert-name','path-socks-'+domain,'-d',domain,'--non-interactive','--agree-tos','--register-unsafely-without-email','--keep-until-expiring')
        print('[5/6] 启用443 TLS和本机代理核心')
        files[EDGE/'nginx.conf']=nginx_config(domain,backend,True).encode()
        for path,data in files.items():write(path,data)
        run(APP/'path-socks','-check','-users',STATE/'users.db')
        if init=='systemd':run('systemctl','daemon-reload')
        changed_core=True;enable('path-socks');svc('path-socks','restart')
        run(binary,'-t','-c',EDGE/'nginx.conf','-p',PREFIX)
        svc('path-socks-nginx','reload')
        healthy=False
        for _ in range(10):
            result=run('curl','--noproxy','*','-fsS','--max-time','3','--resolve',domain+':443:127.0.0.1','https://'+domain+'/health',capture=True,check=False)
            if result.returncode==0 and result.stdout.strip()=='ok':healthy=True;break
            time.sleep(1)
        if not healthy:raise RuntimeError('443 TLS健康检查失败')
    except Exception:
        svc('path-socks-nginx','stop',check=False)
        if changed_core:svc('path-socks','stop',check=False)
        for name,unit in (('path-socks-nginx',edge_unit),('path-socks',core_unit)):
            if backups[unit] is None:
                if init=='systemd':run('systemctl','disable',name,check=False)
                else:run('rc-update','del',name,'default',check=False)
        for path,data in backups.items():
            if data is None:path.unlink(missing_ok=True)
            else:atomic(path,data,modes[path],gid if path.is_relative_to(APP) or path.is_relative_to(STATE) or path.is_relative_to(EDGE) else None)
        if init=='systemd':run('systemctl','daemon-reload')
        if was_edge:svc('path-socks-nginx','start',check=False)
        if changed_core and was_core:svc('path-socks','start',check=False)
        print('未完成，已尝试恢复本项目旧文件和运行状态。备份：',backup)
        raise
    print('[6/6] 设置独立自动续期')
    if init=='systemd':
        for name in ('path-socks-renew.service','path-socks-renew.timer'):atomic(unit_dir/name,(HERE/name).read_bytes(),0o644)
        run('systemctl','daemon-reload');run('systemctl','enable','--now','path-socks-renew.timer')
    else:
        daily=Path('/etc/periodic/daily')
        if not daily.is_dir():raise RuntimeError('需要配置每天执行 /opt/path-socks/renew.sh')
        atomic(daily/'path-socks-renew',(HERE/'renew.sh').read_bytes(),0o755)
        run('rc-update','add','crond','default');run('rc-service','crond','start')
    print('原版80/443模式安装完成！客户端端口443，Path和UUID操作不变；CF Token未使用。')
    print('HTTP验证需要80持续可达用于续期。原配置备份：',backup)
    run('bash','/usr/local/bin/sbb','path','show')


if __name__=='__main__':
    import fcntl
    try:
        if os.geteuid()!=0 or sys.version_info<(3,9):raise RuntimeError('需要Linux root和Python3.9以上')
        os.umask(0o077)
        with open('/run/path-socks-maintenance.lock','w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            install(sys.argv[1] if len(sys.argv)>1 else '')
    except Exception as error:
        print('安装停止：',str(error) if isinstance(error,(ValueError,RuntimeError)) else type(error).__name__+'；请检查上方命令输出、解析和安全组')
        sys.exit(1)
