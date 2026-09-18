"""Project-only purge safety and disposable Linux integration."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import purge


class InventoryTests(unittest.TestCase):
    def test_allowlist_and_symlink_target_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            app=root/'opt/path-socks';app.mkdir(parents=True)
            (app/'secret').write_text('fixture')
            other=root/'opt/other-site';other.mkdir();(other/'keep').write_text('keep')
            backup=root/'var/backups/path-socks.fixture';backup.mkdir(parents=True);(backup/'secret').write_text('old fixture')
            unrelated=root/'var/backups/other-backup';unrelated.mkdir()
            try:(app/'outside').symlink_to(other,target_is_directory=True)
            except OSError:pass
            targets=purge.inventory(root)
            self.assertIn(app,targets);self.assertIn(backup,targets)
            self.assertNotIn(other,targets);self.assertNotIn(unrelated,targets)
            purge.remove(targets)
            self.assertEqual((other/'keep').read_text(),'keep')
            self.assertTrue(unrelated.exists());self.assertFalse(app.exists())


@unittest.skipUnless(os.environ.get('GITHUB_ACTIONS')=='true' and hasattr(os,'geteuid') and os.geteuid()==0,'Disposable root Linux CI only')
class InstalledPurgeTests(unittest.TestCase):
    def test_full_purge_and_cancel(self):
        self.assertTrue(Path('/etc/path-socks').is_dir())
        self.assertTrue(Path('/etc/path-socks-nginx').is_dir())
        before=purge.inventory()
        with patch.object(purge,'ask',return_value='no'):purge.main()
        self.assertEqual(before,purge.inventory())
        sentinel=Path('/var/backups/unrelated-site-backup');sentinel.mkdir(exist_ok=True)
        (sentinel/'keep').write_text('keep')
        nginx=Path('/etc/nginx/nginx.conf').read_bytes()
        with patch.object(purge,'ask',side_effect=['yes','PURGE-SBB']):purge.main()
        self.assertEqual(purge.inventory(),[])
        self.assertEqual((sentinel/'keep').read_text(),'keep')
        self.assertEqual(Path('/etc/nginx/nginx.conf').read_bytes(),nginx)
        self.assertFalse(Path('/usr/local/bin/sbb').exists())
        import pwd
        for name in ('path-socks','sbb-reality'):
            with self.assertRaises(KeyError):pwd.getpwnam(name)


if __name__=='__main__':unittest.main()
