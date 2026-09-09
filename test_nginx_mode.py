"""Disposable GitHub VM: real Nginx/core, local HTTP-01 and certificate fixture."""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
from unittest.mock import patch

import nginx_mode as n


def main():
    assert os.geteuid()==0 and os.environ.get('GITHUB_ACTIONS')=='true'
    assert not n.EDGE.exists(), 'Refuse existing Nginx-mode installation'
    original=n.run
    original('sudo','apt-get','update')
    original('sudo','apt-get','install','-y','nginx')
    original('systemctl','disable','--now','nginx')  # Disposable runner only.
    previous=(n.STATE/'users.db').read_bytes()
    with socket.socket() as occupied:
        occupied.bind(('0.0.0.0',80));occupied.listen()
        try:n.check_ports()
        except RuntimeError:pass
        else:raise AssertionError('Occupied port accepted')
        assert (n.STATE/'users.db').read_bytes()==previous
    with tempfile.TemporaryDirectory() as directory:
        stage=Path(directory)
        for name in ('checksums.txt','path-manager','renew.sh','port.py','sbb','path-socks-tls.service','path-socks-tls.openrc','path-socks-renew.service','path-socks-renew.timer'):
            shutil.copy(n.HERE/name,stage/name)
        shutil.copy(n.HERE/'bin/path-socks-linux-amd64',stage/'path-socks')
        original('openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','2','-subj','/CN=gateway.test','-addext','subjectAltName=DNS:gateway.test','-keyout',stage/'key.pem','-out',stage/'cert.pem',capture=True)
        os.environ['CURL_CA_BUNDLE']=str(stage/'cert.pem')
        def fixture(*args,**kw):
            args=tuple(map(str,args))
            if 'https://api.ipify.org' in args:return subprocess.CompletedProcess(args,0,'8.8.8.8','')
            if args[0]=='certbot':
                assert '--webroot' in args and 'cloudflare' not in ' '.join(args)
                challenge=n.WEBROOT/'.well-known/acme-challenge/fixture'
                challenge.write_text('http01-ok');challenge.chmod(0o644)
                reply=original('curl','--noproxy','*','-fsS','--resolve','gateway.test:80:127.0.0.1','http://gateway.test/.well-known/acme-challenge/fixture',capture=True)
                assert reply.stdout=='http01-ok'
                cert=n.EDGE/'acme/live/path-socks-gateway.test';cert.mkdir(parents=True,exist_ok=True)
                shutil.copy(stage/'cert.pem',cert/'fullchain.pem');shutil.copy(stage/'key.pem',cert/'privkey.pem')
                return subprocess.CompletedProcess(args,0)
            return original(*args,**kw)
        with patch.object(n,'HERE',stage),patch.object(n,'packages',n.check_ports),patch.object(n,'run',fixture),patch.object(n.socket,'getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',80))]):
            n.install('gateway.test')
            assert (n.STATE/'transport').read_text().strip()=='nginx'
            assert (n.STATE/'port').read_text().strip()=='443'
            assert (n.STATE/'users.db').read_bytes()==previous
            n.check_ports()  # Own running master is allowed.
            n.install('gateway.test')
            assert (n.STATE/'users.db').read_bytes()==previous
            # Failed renewal/install restores the active original configuration.
            before=(n.EDGE/'nginx.conf').read_bytes()
            def failure(*args,**kw):
                if str(args[0])=='certbot':raise RuntimeError('Expected fixture failure')
                return fixture(*args,**kw)
            with patch.object(n,'run',failure):
                try:n.install('gateway.test')
                except RuntimeError as error:assert 'Expected fixture' in str(error)
                else:raise AssertionError('Failure not propagated')
            assert (n.EDGE/'nginx.conf').read_bytes()==before
            assert n.active('path-socks') and n.active('path-socks-nginx')
        mock=stage/'certbot'
        mock.write_text('#!/bin/sh\ncase "$*" in *"/etc/path-socks-nginx/acme"*) exit 0;; *) exit 9;; esac\n');mock.chmod(0o755)
        env=os.environ.copy();env['PATH']=str(stage)+':'+env['PATH']
        subprocess.run(['bash',str(n.APP/'renew.sh')],env=env,check=True)
        original('curl','--noproxy','*','-fsS','--resolve','gateway.test:443:127.0.0.1','https://gateway.test/health')
        original('systemctl','disable','--now','path-socks-nginx','path-socks','path-socks-renew.timer')
    print('PASS: conflict refusal, HTTP challenge, TLS health, repeat upgrade, UUID preservation, rollback, renewal')


if __name__=='__main__':main()
