import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import urllib.request
import zipfile

ROOT=Path(__file__).resolve().parent


class MihomoTests(unittest.TestCase):
    def test_generated_config_with_real_core(self):
        windows=os.name=='nt'
        filename='mihomo-windows-amd64-compatible-v1.19.30.zip' if windows else 'mihomo-linux-amd64-compatible-v1.19.30.gz'
        expected='289fde5e29d37a5b3326480590d8b3551c5bf7f8737290355c19bce74d57a563' if windows else 'db214c7a2517e63c150d123178d16d102e03a241ccdae4e5e07ffbe9cf56c6f9'
        runtime=ROOT/'.test-runtime';runtime.mkdir(exist_ok=True)
        archive=runtime/filename
        if not archive.exists():urllib.request.urlretrieve('https://github.com/MetaCubeX/mihomo/releases/download/v1.19.30/'+filename,archive)
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(),expected)
        binary=runtime/('mihomo.exe' if windows else 'mihomo')
        if windows:
            with zipfile.ZipFile(archive) as z:
                binary.write_bytes(z.read(next(x for x in z.namelist() if x.endswith('.exe'))))
        else: binary.write_bytes(gzip.decompress(archive.read_bytes()));binary.chmod(0o755)
        c={'host':'8.8.8.8','port':26443,'id':'00000000-0000-0000-0000-000000000001','sni':'www.microsoft.com','public':'A'*43,'sid':'0123456789abcdef'}
        r={'host':'9.9.9.9','port':1080,'user':'fixture','pass':'fixture'}
        result=subprocess.run(['node',str(ROOT/'test_clients.cjs'),'export'],input=json.dumps({'c':c,'r':r}),capture_output=True,text=True,check=True)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.yaml';path.write_text(json.dumps(json.loads(result.stdout)['mihomo']))
            result=subprocess.run([str(binary),'-t','-d',directory,'-f',str(path)],capture_output=True,text=True,timeout=60)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__=='__main__':unittest.main()
