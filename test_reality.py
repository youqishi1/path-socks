"""Pinned real Xray binary: config validation and REALITY -> SOCKS5 end-to-end."""
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
import zipfile

import reality

ROOT=Path(__file__).resolve().parent


def process(*args, **kw):
    if os.name=='nt': kw['creationflags']=subprocess.CREATE_NO_WINDOW
    return subprocess.Popen([str(x) for x in args], **kw)


def port():
    for _ in range(100):
        candidate=20000+secrets.randbelow(10000)
        try:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',candidate))
                return candidate
        except OSError: pass
    raise RuntimeError('no test port')


class RealityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        runtime=ROOT/'.test-runtime'
        runtime.mkdir(exist_ok=True)
        if os.name=='nt': cls.binary=runtime/'xray/xray.exe'
        else:
            asset=json.loads((ROOT/'xray-release.json').read_text())['assets']['amd64']
            zip_path=runtime/'xray-linux.zip'
            if not zip_path.exists(): urllib.request.urlretrieve(asset['url'],zip_path)
            assert hashlib.sha256(zip_path.read_bytes()).hexdigest()==asset['sha256']
            cls.binary=runtime/'xray-linux'
            with zipfile.ZipFile(zip_path) as archive: cls.binary.write_bytes(archive.read('xray'))
            cls.binary.chmod(0o755)
        result=subprocess.check_output([str(cls.binary),'x25519'],text=True)
        cls.private=re.search(r'PrivateKey:\s*(\S+)',result)[1]
        cls.public=re.search(r'Password \(PublicKey\):\s*(\S+)',result)[1]

    def test_real_chain_and_failure_closed(self):
        self.chain('xray')

    def test_mihomo_chain_and_failure_closed(self):
        from test_mihomo import MihomoTests
        MihomoTests().test_generated_config_with_real_core()
        self.chain('mihomo')

    def chain(self,kind):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            tmp=Path(directory)
            s={'host':'8.8.8.8','port':port(),'sni':'www.microsoft.com','private':self.private,'public':self.public,'sid':'0123456789abcdef','users':[{'label':'test','id':'00000000-0000-0000-0000-000000000001'}]}
            c={k:s[k] for k in ('host','port','sni','public','sid')};c['id']=s['users'][0]['id']
            r={'host':'9.9.9.9','port':port(),'user':'fixture-user','pass':'fixture-password'}
            result=subprocess.run(['node',str(ROOT/'test_clients.cjs'),'export'],input=json.dumps({'c':c,'r':r}),capture_output=True,text=True,encoding='utf-8',check=True)
            client=json.loads(result.stdout)['xray']; server=reality.server_config(s)
            # Exercise the same authenticated outbound used by the diagnostic probe.
            client['outbounds'][1]=dict(reality.probe_config(s,port())['outbounds'][0],tag='vps')
            # Only the disposable fixture is allowed loopback destinations.
            server['routing']['rules']=server['routing']['rules'][:1]
            client['outbounds'][0]['settings']['servers'][0]['address']='127.0.0.1'
            client['outbounds'][1]['settings']['vnext'][0]['address']='127.0.0.1'
            client['inbounds'][0]['port']=port(); client['inbounds'][1]['port']=port()
            http_port=client['inbounds'][1]['port']
            client_binary=self.binary
            if kind=='mihomo':
                client=json.loads(result.stdout)['mihomo']
                client['proxies'][0]['server']='127.0.0.1'
                client['proxies'][1]['server']='127.0.0.1'
                client['mixed-port']=http_port
                client['log-level']='debug'
                client_binary=ROOT/'.test-runtime'/('mihomo.exe' if os.name=='nt' else 'mihomo')
            # Local TLS1.3 target avoids reliance on a public destination's availability.
            tlsport=port()
            server['inbounds'][0]['streamSettings']['realitySettings']['target']=f'127.0.0.1:{tlsport}'
            openssl=os.environ.get('OPENSSL','openssl')
            subprocess.run([openssl,'req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=www.microsoft.com','-keyout',str(tmp/'key.pem'),'-out',str(tmp/'cert.pem')],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            target=socket.socket();target.bind(('127.0.0.1',tlsport));target.listen();target.settimeout(.2)
            target_stop=threading.Event();stack.callback(target.close);stack.callback(target_stop.set)
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version=ssl.TLSVersion.TLSv1_3
            context.load_cert_chain(tmp/'cert.pem',tmp/'key.pem')
            context.set_alpn_protocols(['h2','http/1.1'])
            def target_conn(conn):
                try:
                    conn.settimeout(3)
                    with context.wrap_socket(conn,server_side=True) as tls: tls.recv(4096)
                except (OSError,ssl.SSLError): conn.close()
            def target_loop():
                while not target_stop.is_set():
                    try:conn,_=target.accept()
                    except socket.timeout:continue
                    except OSError:return
                    threading.Thread(target=target_conn,args=(conn,),daemon=True).start()
            threading.Thread(target=target_loop,daemon=True).start()
            for filename,obj in [('server.json',server),('client.json',client)]:
                (tmp/filename).write_text(json.dumps(obj))
                args=[str(self.binary),'run','-test','-config',str(tmp/filename)]
                if kind=='mihomo' and filename=='client.json': args=[str(client_binary),'-t','-d',str(tmp),'-f',str(tmp/filename)]
                test=subprocess.run(args,capture_output=True,text=True)
                self.assertEqual(test.returncode,0,test.stdout+test.stderr)
            socks=socket.socket();socks.bind(('127.0.0.1',r['port']));socks.listen();socks.settimeout(.2)
            stack.callback(socks.close)
            stop=threading.Event(); good=threading.Event();good.set();hits=[]
            stack.callback(stop.set)
            def serve():
                while not stop.is_set():
                    try: conn,_=socks.accept()
                    except socket.timeout: continue
                    except OSError: return
                    with conn:
                        conn.settimeout(10)
                        try:
                            f=conn.makefile('rb',buffering=0)
                            def read(n):
                                data=b''
                                while len(data)<n:
                                    chunk=f.read(n-len(data))
                                    if not chunk: raise EOFError()
                                    data+=chunk
                                return data
                            head=read(2);read(head[1]);conn.sendall(b'\x05\x02')
                            read(1); user=read(read(1)[0]); password=read(read(1)[0])
                            if not good.is_set() or user!=r['user'].encode() or password!=r['pass'].encode():
                                conn.sendall(b'\x01\x01');continue
                            conn.sendall(b'\x01\x00')
                            head=read(4)
                            read(4 if head[3]==1 else 16 if head[3]==4 else read(1)[0]);read(2)
                            conn.sendall(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00')
                            read(1);hits.append(1)
                            conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\nConnection: close\r\n\r\nresidential')
                        except (OSError,EOFError): pass
            threading.Thread(target=serve,daemon=True).start()
            client_args=[client_binary,'run','-config',tmp/'client.json'] if kind=='xray' else [client_binary,'-d',tmp,'-f',tmp/'client.json']
            server_log=stack.enter_context(open(tmp/'server.log','wb'))
            client_log=stack.enter_context(open(tmp/'client.log','wb'))
            processes=[process(self.binary,'run','-config',tmp/'server.json',stdout=server_log,stderr=subprocess.STDOUT),process(*client_args,stdout=client_log,stderr=subprocess.STDOUT)]
            for p in processes:stack.callback(lambda p=p:self.stop(p))
            time.sleep(2)
            def request():
                with socket.create_connection(('127.0.0.1',http_port),timeout=15) as conn:
                    conn.sendall(b'GET http://example.test/ HTTP/1.1\r\nHost: example.test\r\nConnection: close\r\n\r\n')
                    response=b''
                    while True:
                        data=conn.recv(4096)
                        if not data: return response
                        response+=data
            response=request()
            if b'residential' not in response:
                self.stop(processes[1])
                self.stop(processes[0])
                self.fail(str(response)+'\n'+(tmp/'client.log').read_text(errors='replace')+'\nSERVER:\n'+(tmp/'server.log').read_text(errors='replace'))
            self.assertEqual(len(hits),1)
            good.clear()
            try: response=request()
            except (OSError,TimeoutError): response=b''
            self.assertNotIn(b'residential',response)
            self.assertEqual(len(hits),1)

    @staticmethod
    def stop(p):
        p.terminate()
        try:p.wait(timeout=5)
        except subprocess.TimeoutExpired:p.kill();p.wait()


if __name__=='__main__': unittest.main()
