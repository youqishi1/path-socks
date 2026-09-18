"""Disposable Ubuntu CI only. Real service/binary; local network/download fixtures."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import reality


@unittest.skipUnless(os.environ.get('GITHUB_ACTIONS')=='true' and hasattr(os,'geteuid') and os.geteuid()==0,'Disposable Linux CI only')
class InstallTests(unittest.TestCase):
    def test_install_upgrade_preserve_other_scheme(self):
        self.assertFalse(reality.STATE.exists(),'Refuse to alter existing REALITY installation')
        before=Path('/etc/path-socks/users.db').read_bytes()
        old_run=reality.run
        old_connect=socket.create_connection
        class TLS:
            minimum_version=None
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def set_alpn_protocols(self,value): pass
            def wrap_socket(self,*args,**kw): return self
            def selected_alpn_protocol(self): return 'h2'
        def connect(address,*args,**kw):
            return TLS() if address==('www.microsoft.com',443) else old_connect(address,*args,**kw)
        def command(*args,**kwargs):
            if args[0]=='curl':
                if 'https://api.ipify.org' in args:
                    return subprocess.CompletedProcess(args,0,stdout='8.8.8.8',stderr='')
                shutil.copyfile(reality.HERE/'.test-runtime/xray-linux.zip',args[-1])
                return subprocess.CompletedProcess(args,0)
            return old_run(*args,**kwargs)
        with socket.socket() as occupied:
            occupied.bind(('0.0.0.0',26443));occupied.listen()
            with patch.object(reality,'run',command),patch.object(reality,'prompt',side_effect=lambda label,default='':default),patch.object(reality.socket,'create_connection',connect),patch.object(reality.ssl,'create_default_context',return_value=TLS()):
                output=io.StringIO()
                with contextlib.redirect_stdout(output): reality.install()
                first=json.loads((reality.STATE/'state.json').read_text())
                self.assertNotEqual(first['port'],26443)
                self.assertNotIn(first['private'],output.getvalue())
                with contextlib.redirect_stdout(io.StringIO()): reality.install()
                second=json.loads((reality.STATE/'state.json').read_text())
                self.assertEqual(first,second)
                self.assertEqual(before,Path('/etc/path-socks/users.db').read_bytes())
                self.assertEqual((reality.STATE/'state.json').stat().st_mode & 0o777,0o600)
                self.assertTrue(reality.active())
                old_run('bash','/usr/local/bin/sbb','status')
        # Updating the manager must neither restart nor change any credentials.
        saved=(reality.STATE/'state.json').read_bytes()
        pid=reality.current_pid()
        with tempfile.TemporaryDirectory() as directory:
            mock=Path(directory)/'curl'
            mock.write_text('''#!/usr/bin/env python3
import json,os,shutil,sys
from pathlib import Path
args=sys.argv[1:]
if any('api.github.com' in a for a in args):print(json.dumps({'sha':'a'*40}))
else:
    url=next(a for a in args if a.startswith('https://'))
    shutil.copyfile(Path(os.environ['SBB_TEST_ROOT'])/url.rsplit('/',1)[-1],args[args.index('-o')+1])
''')
            mock.chmod(0o755)
            env=os.environ.copy();env['PATH']=directory+':'+env['PATH'];env['SBB_TEST_ROOT']=str(reality.HERE)
            subprocess.run(['bash',str(reality.HERE/'update-manager.sh')],env=env,check=True)
        self.assertEqual(reality.current_pid(),pid)
        self.assertEqual((reality.STATE/'state.json').read_bytes(),saved)
        # Deliberately broken config is rebuilt from saved state, not new keys.
        (reality.STATE/'config.json').write_text('{}')
        with patch.object(reality,'prompt',return_value='yes'),patch.object(reality,'diagnose',return_value=True):
            reality.repair()
        self.assertEqual((reality.STATE/'state.json').read_bytes(),saved)
        self.assertEqual(json.loads((reality.STATE/'config.json').read_text()),reality.server_config(second))
        with patch.object(reality,'prompt',return_value='no'):
            self.assertFalse(reality.uninstall())
        self.assertTrue(reality.active())
        with patch.object(reality,'prompt',return_value='DELETE'):
            self.assertTrue(reality.uninstall())
        self.assertFalse(reality.active())
        self.assertFalse(reality.APP.exists())
        self.assertFalse(reality.STATE.exists())
        self.assertFalse(Path('/etc/systemd/system/sbb-reality.service').exists())
        self.assertTrue(Path('/usr/local/bin/sbb').exists())
        self.assertEqual(before,Path('/etc/path-socks/users.db').read_bytes())
        backup=max(Path('/var/backups').glob('sbb-reality-uninstall.*'),key=lambda p:p.stat().st_mtime)
        self.assertEqual((backup/'1/state.json').read_bytes(),saved)
        self.assertEqual(backup.stat().st_mode&0o777,0o700)


if __name__=='__main__': unittest.main()
