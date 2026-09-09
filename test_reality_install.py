"""Disposable Ubuntu CI only. Real service/binary; local network/download fixtures."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
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
                old_run('systemctl','stop','sbb-reality')


if __name__=='__main__': unittest.main()
