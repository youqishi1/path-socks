#!/usr/bin/env python3
"""SBB's isolated Xray REALITY installer and local root-only manager."""
import base64
import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile

from port import choose

APP = Path('/opt/sbb-reality')
STATE = Path('/etc/sbb-reality')
HERE = Path(__file__).resolve().parent
PRIVATE = ['0.0.0.0/8','10.0.0.0/8','100.64.0.0/10','127.0.0.0/8','169.254.0.0/16','172.16.0.0/12','192.168.0.0/16','224.0.0.0/4','240.0.0.0/4','::/128','::1/128','fc00::/7','fe80::/10','ff00::/8']


def run(*args, capture=False, check=True):
    return subprocess.run([str(a) for a in args], check=check, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def prompt(label, default=''):
    print(f'{label}' + (f' [{default}]' if default else '') + '：', end='', flush=True)
    with open('/dev/tty') as tty:
        result = tty.readline()
    if result == '':
        raise RuntimeError('没有交互终端，请在SSH中执行')
    return result.strip() or default


def atomic(path, data, mode=0o600, gid=None):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix='.sbb-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data if isinstance(data, bytes) else data.encode())
            out.flush()
            os.fsync(out.fileno())
        os.chmod(name, mode)
        if gid is not None:
            os.chown(name, 0, gid)
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def server_config(s):
    return {'log': {'loglevel': 'warning', 'access': 'none'},
        'inbounds': [{'tag': 'entry', 'listen': '0.0.0.0', 'port': s['port'], 'protocol': 'vless',
            'settings': {'clients': [{'id': u['id'], 'flow': 'xtls-rprx-vision'} for u in s['users']], 'decryption': 'none'},
            'streamSettings': {'network': 'raw', 'security': 'reality', 'realitySettings': {
                'show': False, 'target': s['sni'] + ':443', 'xver': 0, 'serverNames': [s['sni']],
                'privateKey': s['private'], 'shortIds': [s['sid']]}}}],
        'outbounds': [{'tag': 'relay', 'protocol': 'freedom', 'settings': {'domainStrategy': 'UseIPv4'}},
                      {'tag': 'blocked', 'protocol': 'blackhole'}],
        'routing': {'domainStrategy': 'IPOnDemand', 'rules': [
            {'type': 'field', 'network': 'udp', 'outboundTag': 'blocked'},
            {'type': 'field', 'ip': PRIVATE, 'outboundTag': 'blocked'}]}}


def system():
    if Path('/run/systemd/system').is_dir() and shutil.which('systemctl'): return 'systemd'
    if shutil.which('rc-service'): return 'openrc'
    raise RuntimeError('只支持systemd或OpenRC')


def service(action, check=True):
    return run('systemctl', action, 'sbb-reality', check=check) if system() == 'systemd' else run('rc-service', 'sbb-reality', action, check=check)


def active():
    args = ['systemctl', 'is-active', '--quiet', 'sbb-reality'] if system() == 'systemd' else ['rc-service', 'sbb-reality', 'status']
    return run(*args, capture=True, check=False).returncode == 0


def current_pid():
    if system() == 'systemd':
        value = run('systemctl', 'show', 'sbb-reality', '-p', 'MainPID', '--value', capture=True, check=False).stdout.strip()
        return int(value) if value.isdigit() else 0
    value = run('pgrep', '-f', '^/opt/sbb-reality/xray run', capture=True, check=False).stdout.strip().splitlines()
    return int(value[0]) if value else 0


def config_test(binary, config):
    result = run(binary, 'run', '-test', '-config', config, capture=True, check=False)
    if result.returncode:
        # Do not dump configs/private keys into installer logs.
        raise RuntimeError('Xray配置校验失败，现有服务未启用新配置')


def healthy(port):
    for _ in range(15):
        if active():
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=1): return True
            except OSError: pass
        time.sleep(1)
    return False


def firewall(port):
    if shutil.which('ufw') and 'Status: active' in run('ufw', 'status', capture=True).stdout:
        run('ufw', 'allow', f'{port}/tcp')
    if shutil.which('firewall-cmd') and run('firewall-cmd', '--state', capture=True, check=False).returncode == 0:
        run('firewall-cmd', '--permanent', f'--add-port={port}/tcp')
        run('firewall-cmd', f'--add-port={port}/tcp')


def unit_text(init):
    if init == 'systemd':
        return '''[Unit]
Description=SBB isolated Xray REALITY relay
After=network-online.target
Wants=network-online.target
[Service]
User=sbb-reality
Group=sbb-reality
ExecStart=/opt/sbb-reality/xray run -config /etc/sbb-reality/config.json
Restart=on-failure
RestartSec=3
LimitNOFILE=131072
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
[Install]
WantedBy=multi-user.target
'''
    return '''#!/sbin/openrc-run
name="sbb-reality"
command="/opt/sbb-reality/xray"
command_args="run -config /etc/sbb-reality/config.json"
command_user="sbb-reality:sbb-reality"
supervisor="supervise-daemon"
respawn_delay=3
respawn_max=0
output_log="/var/log/sbb-reality.log"
error_log="/var/log/sbb-reality.log"
depend() { need net; }
'''


def install():
    import grp
    import pwd
    init = system()
    old = json.loads((STATE / 'state.json').read_text()) if (STATE / 'state.json').exists() else None
    s = copy.deepcopy(old) if old else {'users': [{'label': f'用户{i:02}', 'id': str(uuid.uuid4())} for i in range(1,11)]}
    detected = old['host'] if old else run('curl','--noproxy','*','-4fsS','--max-time','10','https://api.ipify.org', capture=True, check=False).stdout.strip()
    host = prompt('VPS公网IPv4（回车沿用自动识别值）', detected)
    if not ipaddress.IPv4Address(host).is_global: raise ValueError('请输入真实公网IPv4')
    s['host'] = host
    text = prompt('端口（回车自动检测）', str(s.get('port', 26443)))
    s['port'] = choose(int(text), current_pid())
    s['sni'] = prompt('REALITY目标域名（不是你自己的域名，通常回车即可）', s.get('sni','www.microsoft.com'))
    if not re.fullmatch(r'[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', s['sni']): raise ValueError('目标域名格式错误')
    print('检查目标站点TLS1.3和HTTP/2可达性…')
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.set_alpn_protocols(['h2'])
    with socket.create_connection((s['sni'],443), timeout=10) as tcp:
        with context.wrap_socket(tcp, server_hostname=s['sni']) as tls:
            if tls.selected_alpn_protocol() != 'h2': raise ValueError('目标不支持HTTP/2，请选择其他目标')
    manifest = json.loads((HERE / 'xray-release.json').read_text())
    machine = os.uname().machine
    arch = {'x86_64':'amd64','aarch64':'arm64'}.get(machine)
    if not arch: raise ValueError('仅支持amd64和arm64')
    asset = manifest['assets'][arch]
    print('下载固定版本官方Xray核心并核对SHA256：', manifest['version'])
    with tempfile.TemporaryDirectory(prefix='sbb-reality-') as directory:
        tmp = Path(directory)
        run('curl','-fL','--retry','3','--connect-timeout','10','--max-time','300',asset['url'],'-o',tmp/'xray.zip')
        if hashlib.sha256((tmp/'xray.zip').read_bytes()).hexdigest() != asset['sha256']: raise ValueError('官方核心SHA256不一致，停止安装')
        with zipfile.ZipFile(tmp/'xray.zip') as archive:
            (tmp/'xray').write_bytes(archive.read('xray'))
        (tmp/'xray').chmod(0o755)
        if not old:
            keys = run(tmp/'xray','x25519',capture=True).stdout
            s['private'] = re.search(r'PrivateKey:\s*(\S+)',keys)[1]
            s['public'] = re.search(r'(?:Password \(PublicKey\)|PublicKey):\s*(\S+)',keys)[1]
            s['sid'] = secrets.token_hex(8)
        s['version'] = manifest['version']
        (tmp/'config.json').write_text(json.dumps(server_config(s)))
        config_test(tmp/'xray',tmp/'config.json')
        try: pwd.getpwnam('sbb-reality')
        except KeyError:
            if shutil.which('useradd'):
                try: grp.getgrnam('sbb-reality')
                except KeyError: run('groupadd','--system','sbb-reality')
                run('useradd','--system','--gid','sbb-reality','--no-create-home','--shell','/usr/sbin/nologin','sbb-reality')
            else:
                run('addgroup','-S','sbb-reality')
                run('adduser','-S','-D','-H','-G','sbb-reality','-s','/sbin/nologin','sbb-reality')
        gid = grp.getgrnam('sbb-reality').gr_gid
        for directory in (APP,STATE):
            if directory.is_symlink(): raise RuntimeError('拒绝写入符号链接目录')
            directory.mkdir(exist_ok=True, parents=True)
            directory.chmod(0o750)
            os.chown(directory,0,gid)
        unit = Path('/etc/systemd/system/sbb-reality.service' if init == 'systemd' else '/etc/init.d/sbb-reality')
        files = {APP/'xray': (tmp/'xray').read_bytes(), STATE/'state.json': json.dumps(s).encode(),
                 STATE/'config.json': (tmp/'config.json').read_bytes(),unit:unit_text(init).encode()}
        for name in ('reality.py','port.py','client-helper.html','xray-release.json'):
            files[APP/name] = (HERE/name).read_bytes()
        files[Path('/usr/local/bin/sbb')] = (HERE/'sbb').read_bytes()
        # Migrate the legacy menu to the shared-entry-safe manager, without changing users or service.
        if Path('/opt/path-socks').is_dir() and not Path('/opt/path-socks').is_symlink():
            files[Path('/opt/path-socks/sbb-path')] = (HERE/'path-manager').read_bytes()
        backups = {p:p.read_bytes() if p.exists() else None for p in files}
        Path('/var/backups').mkdir(exist_ok=True)
        backup = Path(tempfile.mkdtemp(prefix='sbb-reality.',dir='/var/backups'))
        for p,data in backups.items():
            if data is not None:
                target = backup / str(p).lstrip('/')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        was_active = active()
        try:
            for p,data in files.items():
                mode = 0o755 if p.name in ('xray','sbb','sbb-path') or (p==unit and init=='openrc') else 0o640
                if p == STATE/'state.json': mode = 0o600
                atomic(p,data,mode,gid if p.is_relative_to(APP) or p.is_relative_to(STATE) else None)
            if init=='systemd': run('systemctl','daemon-reload'); run('systemctl','enable','sbb-reality')
            else: run('rc-update','add','sbb-reality','default')
            service('restart')
            if not healthy(s['port']): raise RuntimeError('新核心未正常监听')
            firewall(s['port'])
        except Exception:
            service('stop',check=False)
            for p,data in backups.items():
                if data is None: p.unlink(missing_ok=True)
                else:
                    mode=0o755 if p.name in ('xray','sbb','sbb-path') or (p==unit and init=='openrc') else 0o640
                    if p == STATE/'state.json': mode=0o600
                    atomic(p,data,mode,gid if p.is_relative_to(APP) or p.is_relative_to(STATE) else None)
            if init=='systemd': run('systemctl','daemon-reload')
            if was_active: service('start',check=False)
            print('启用失败，已恢复旧文件。备份：',backup)
            raise
    print(f'安装成功；请在云安全组放行TCP {s["port"]}。未更改Path或Nginx服务。')
    print('配置与密钥备份仅root可读：',backup)
    print('监听检查通过不等于公网链路已经验证；请在客户端验证住宅出口。')
    show(s)


def show(s):
    print('\nREALITY中转：',s['host'], '端口：',s['port'], '用户数：',len(s['users']))
    print('下面连接码含用户凭据，只交给对应用户，不要公开。连接码不是住宅出口配置。')
    for i,u in enumerate(s['users'],1):
        print(f'{i}. {u["label"]}')
    index = int(prompt('导出哪个用户的连接码', '1')) - 1
    if not 0 <= index < len(s['users']): raise ValueError('用户序号无效')
    client = {k:s[k] for k in ('host','port','sni','public','sid')}
    client['id'] = s['users'][index]['id']
    print('SBB1.' + base64.urlsafe_b64encode(json.dumps(client).encode()).decode())
    print('下载项目中的client-helper.html到电脑，双击打开，粘贴连接码并在本地填写住宅SOCKS5。')
    print('地址：https://github.com/youqishi1/path-socks/blob/main/client-helper.html')


def apply_state(s):
    import grp
    gid = grp.getgrnam('sbb-reality').gr_gid
    old_state = (STATE/'state.json').read_bytes()
    old_config = (STATE/'config.json').read_bytes()
    with tempfile.TemporaryDirectory() as directory:
        config = Path(directory)/'config.json'
        config.write_text(json.dumps(server_config(s)))
        config_test(APP/'xray',config)
        atomic(STATE/'config.json',config.read_bytes(),0o640,gid)
    try:
        service('restart')
        if not healthy(s['port']): raise RuntimeError('启动检查失败')
        atomic(STATE/'state.json',json.dumps(s),0o600,gid)
    except Exception:
        atomic(STATE/'config.json',old_config,0o640,gid)
        atomic(STATE/'state.json',old_state,0o600,gid)
        service('restart',check=False)
        raise


def menu():
    while True:
        s=json.loads((STATE/'state.json').read_text())
        print('\nREALITY：1连接码 2添加用户 3删除用户 4重置UUID 5改端口 6状态 7启用/重启 8停用(关闭自启) 9日志 0返回')
        choice=prompt('选择','0')
        if choice=='0': return
        try:
            if choice=='1': show(s)
            elif choice=='2':
                label=prompt('用户名称',f'用户{len(s["users"])+1}')
                s['users'].append({'label':label,'id':str(uuid.uuid4())}); apply_state(s)
                print('添加成功；服务已重启，已有连接会重连。')
            elif choice in ('3','4'):
                for i,u in enumerate(s['users'],1): print(i,u['label'])
                index=int(prompt('用户序号'))-1
                if not 0<=index<len(s['users']): raise ValueError('无效序号')
                if prompt('操作会重启服务、断开在线连接。输入yes确认')!='yes': continue
                if choice=='3':
                    if len(s['users'])==1: raise ValueError('至少保留一个用户')
                    s['users'].pop(index)
                else: s['users'][index]['id']=str(uuid.uuid4())
                apply_state(s)
            elif choice=='5':
                p=choose(int(prompt('新端口（10240—65535）')),explicit=True)
                if prompt(f'先放行安全组TCP {p}；客户端都要改端口。输入yes确认')!='yes': continue
                firewall(p); s['port']=p; apply_state(s)
            elif choice=='6': print('运行中' if active() else '未运行', '端口',s['port'])
            elif choice=='7':
                if system()=='systemd': run('systemctl','enable','sbb-reality')
                else: run('rc-update','add','sbb-reality','default')
                service('restart')
            elif choice=='8':
                if prompt('停用REALITY会断开此方案全部用户并关闭自启，输入yes确认')=='yes':
                    if system()=='systemd': run('systemctl','disable','--now','sbb-reality')
                    else: service('stop'); run('rc-update','del','sbb-reality','default')
            elif choice=='9':
                if system()=='systemd': run('journalctl','-u','sbb-reality','-n','50','--no-pager')
                else: run('tail','-n','50','/var/log/sbb-reality.log',check=False)
        except (ValueError,RuntimeError,subprocess.CalledProcessError) as error:
            print('操作未完成：', str(error) if not isinstance(error,subprocess.CalledProcessError) else '系统命令失败，请检查状态')


def main():
    import fcntl
    if sys.version_info < (3,9): raise RuntimeError('需要Python3.9或更新版本，请使用仍受支持的发行版')
    if os.geteuid()!=0: raise RuntimeError('需要root')
    os.umask(0o077)
    with open('/run/sbb-reality.lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        action=sys.argv[1] if len(sys.argv)>1 else 'menu'
        if action=='install': install()
        elif action=='menu': menu()
        elif action=='show': show(json.loads((STATE/'state.json').read_text()))
        elif action=='status': print('REALITY：运行中' if active() else 'REALITY：未运行')
        else: raise ValueError('未知操作')


if __name__=='__main__':
    try: main()
    except Exception as error:
        print('操作停止：',type(error).__name__,str(error) if isinstance(error,(ValueError,RuntimeError)) else '请检查网络、软件源、服务状态，或是否有其他管理操作正在运行')
        sys.exit(1)
