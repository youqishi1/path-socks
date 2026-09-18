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
from urllib.parse import urlencode, quote
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
    show(s, backup=os.environ.get('SBB_BACKUP_HELPER')=='1')


def show(s, backup=False):
    print('\nREALITY中转：',s['host'], '端口：',s['port'], '用户数：',len(s['users']))
    print('下面内容含用户凭据，只交给对应用户。仅连接此节点，出口是VPS，不是住宅。')
    for i,u in enumerate(s['users'],1):
        print(f'{i}. {u["label"]}')
    index = int(prompt('查看哪个用户', '1')) - 1
    if not 0 <= index < len(s['users']): raise ValueError('用户序号无效')
    client = {k:s[k] for k in ('host','port','sni','public','sid')}
    client['id'] = s['users'][index]['id']
    if backup:
        print('备用：配置助手连接码（不支持直接粘贴进v2rayN）：')
        print('SBB1.' + base64.urlsafe_b64encode(json.dumps(client).encode()).decode())
        print('下载client-helper.html，在本地填写住宅SOCKS5，生成文件导入。')
        print('地址：https://github.com/youqishi1/path-socks/blob/main/client-helper.html')
        return
    alias=f'SBB-VPS-{s["host"]}-{s["port"]}-u{index+1}'
    query=urlencode({'encryption':'none','security':'reality','sni':s['sni'],'fp':'chrome','pbk':s['public'],'sid':s['sid'],'type':'tcp','flow':'xtls-rprx-vision'})
    print('\n复制下面vless://完整一行，在v2rayN按Ctrl+V导入：')
    print(f'vless://{client["id"]}@{s["host"]}:{s["port"]}?{query}#{quote(alias)}')
    print('\n也可手填：协议VLESS；加密none；传输TCP；TLS类型reality；Flow xtls-rprx-vision；指纹chrome')
    for label,key in [('地址','host'),('端口','port'),('UUID','id'),('SNI','sni'),('公钥/PublicKey','public'),('ShortId','sid')]:print(f'{label}：{client[key]}')
    print('\nv2rayN手动住宅链式设置（不需要生成文件）：')
    print('1. 将上面的VPS节点保留在单独分组，备注必须唯一：'+alias)
    print('2. 新建“住宅出口”分组，在分组设置的“前置代理别名”中填入上面的完整备注；落地代理别名留空。')
    print('3. 在“住宅出口”分组手动添加SOCKS节点，填写住宅IP、端口、账号、密码；选择Xray核心。')
    print('4. 启用该住宅节点。以后在这个分组添加不同住宅节点，点击切换即可；不需要Path。')
    print('5. 不要直接启用VPS节点当住宅使用；不要删除或改名VPS节点，否则前置链可能被跳过。')
    print('6. 核对实际出口为住宅；住宅密码填错时访问应失败。客户端规则/DNS仍需检查，不能保证全设备流量都经过代理。')
    print('官方设置说明：https://github.com/2dust/v2rayN/wiki/Description-of-proxy-chain')


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


def probe_config(s, port):
    """Local authenticated REALITY client; no direct fallback, no private key."""
    return {'log':{'loglevel':'none'},
        'inbounds':[{'listen':'127.0.0.1','port':port,'protocol':'socks','settings':{'auth':'noauth','udp':False}}],
        'outbounds':[{'protocol':'vless','settings':{'vnext':[{'address':'127.0.0.1','port':s['port'],'users':[{'id':s['users'][0]['id'],'encryption':'none','flow':'xtls-rprx-vision'}]}]},
            'streamSettings':{'network':'raw','security':'reality','realitySettings':{'serverName':s['sni'],'fingerprint':'chrome','publicKey':s['public'],'shortId':s['sid']}}}]}


def diagnose():
    s=json.loads((STATE/'state.json').read_text())
    print('REALITY诊断：不修改配置，不显示密钥；会发起一次经本机REALITY的HTTPS请求。')
    print('服务：', '运行中' if active() else '未运行', '端口：',s['port'])
    config_test(APP/'xray',STATE/'config.json')
    same=json.loads((STATE/'config.json').read_text())==server_config(s)
    print('运行配置与已保存参数：','一致' if same else '不一致，可选修复恢复已保存参数')
    if not active():return False
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='sbb-probe-') as directory:
        config=Path(directory)/'client.json'
        atomic(config,json.dumps(probe_config(s,port)),0o600)
        config_test(APP/'xray',config)
        child=subprocess.Popen([str(APP/'xray'),'run','-config',str(config)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            for _ in range(30):
                if child.poll() is not None:raise RuntimeError('诊断客户端未启动，可能发生本机端口冲突')
                try:
                    with socket.create_connection(('127.0.0.1',port),timeout=0.2):break
                except OSError:time.sleep(0.1)
            result=run('curl','--noproxy','','--proxy',f'socks5h://127.0.0.1:{port}','-sS','--connect-timeout','10','--max-time','25','-o','/dev/null','-w','%{http_code}','https://'+s['sni']+'/',capture=True,check=False)
            ok=result.returncode==0 and result.stdout.strip().isdigit() and result.stdout.strip()!='000'
            print('本机REALITY认证握手及HTTPS转发：','通过，HTTP '+result.stdout.strip() if ok else '失败，curl错误码 '+str(result.returncode))
            print('通过仅代表VPS本机链路，不能证明国内线路或v2rayN配置正常。' if ok else '请核对REALITY目标可达性、配置/密钥和服务日志；不能仅凭此结果认定端口被封。')
            return ok
        finally:
            child.terminate()
            try:child.wait(timeout=5)
            except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)


def repair():
    s=json.loads((STATE/'state.json').read_text())
    print('按已保存参数重建REALITY运行配置并重启；保留UUID、密钥和端口。')
    print('这能修复配置被改坏/服务停止，不能修复客户端错误、被阻断线路或不可用目标。')
    if prompt('会断开REALITY连接，输入yes执行')!='yes':return
    Path('/var/backups').mkdir(exist_ok=True)
    backup=Path(tempfile.mkdtemp(prefix='sbb-reality-repair.',dir='/var/backups'));backup.chmod(0o700)
    for name in ('state.json','config.json'):
        atomic(backup/name,(STATE/name).read_bytes(),0o600)
    apply_state(s)
    print('运行配置已恢复并重启，备份：',backup)
    diagnose()


def uninstall():
    init=system()
    unit=Path('/etc/systemd/system/sbb-reality.service' if init=='systemd' else '/etc/init.d/sbb-reality')
    targets=(APP,STATE,unit)
    expected=('/opt/sbb-reality','/etc/sbb-reality',str(unit))
    for target,literal in zip(targets,expected):
        if str(target)!=literal or target.is_symlink() or target.resolve()!=Path(literal):
            raise RuntimeError('目录或服务文件异常，拒绝卸载')
    print('只卸载本项目REALITY，保留Path、Nginx、共享sbb、防火墙规则和系统账号。')
    print('程序、配置和密钥会移入root私有备份，不会彻底删除。')
    if prompt('确认输入DELETE')!='DELETE':return False
    Path('/var/backups').mkdir(exist_ok=True)
    backup=Path(tempfile.mkdtemp(prefix='sbb-reality-uninstall.',dir='/var/backups'));backup.chmod(0o700)
    if init=='systemd':run('systemctl','disable','--now','sbb-reality')
    else:service('stop');run('rc-update','del','sbb-reality','default',check=False)
    if active():raise RuntimeError('服务仍在运行，未移除文件')
    moved=[]
    try:
        for index,target in enumerate(targets):
            if target.exists():
                dest=backup/str(index);shutil.move(str(target),str(dest));moved.append((target,dest))
        if init=='systemd':run('systemctl','daemon-reload')
    except Exception:
        for target,dest in reversed(moved):shutil.move(str(dest),str(target))
        print('卸载未完成，已尝试恢复文件；服务保持停用，请检查。')
        raise
    print('REALITY已卸载；可恢复的私密备份：',backup)
    return True


def menu():
    while True:
        s=json.loads((STATE/'state.json').read_text())
        print('\n直连VPS IP：1节点链接/手填参数 2添加用户 3删除用户 4重置UUID 5改端口 6状态 7启用/重启 8停用(关闭自启) 9日志 10备用配置助手连接码 11连接诊断 12恢复配置/修复 13卸载REALITY 0返回')
        choice=prompt('选择','0')
        if choice=='0': return
        try:
            if choice=='1': show(s)
            elif choice=='10': show(s,backup=True)
            elif choice=='11': diagnose()
            elif choice=='12': repair()
            elif choice=='13':
                if uninstall():return
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
        elif action=='helper': show(json.loads((STATE/'state.json').read_text()),backup=True)
        elif action=='status': print('REALITY：运行中' if active() else 'REALITY：未运行')
        elif action=='diagnose': diagnose()
        elif action=='repair': repair()
        elif action=='uninstall': uninstall()
        else: raise ValueError('未知操作')


if __name__=='__main__':
    try: main()
    except Exception as error:
        print('操作停止：',type(error).__name__,str(error) if isinstance(error,(ValueError,RuntimeError)) else '请检查网络、软件源、服务状态，或是否有其他管理操作正在运行')
        sys.exit(1)
