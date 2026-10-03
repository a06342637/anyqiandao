"""Release extraction remains Docker-readable under the updater's private umask."""
import io
import os
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.update_common import PROJECT_ID, extract_source


@unittest.skipUnless(os.name == 'posix', 'POSIX permissions are verified on Linux')
class ReleasePermissionsTests(unittest.TestCase):
    def test_private_umask_does_not_hide_source_from_container_user(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / 'source.tar.gz'
            files = {'.project-id': PROJECT_ID, 'VERSION': '0.4.1', 'Dockerfile': 'FROM scratch',
                     'compose.yml': 'services: {}', 'requirements.txt': '', 'app/main.py': '',
                     'frontend/package.json': '{}', 'app/vendor/__init__.py': '',
                     'app/vendor/anyrouter_checkin.py': 'def checkin(): pass',
                     'frontend/src/nested/Example.tsx': 'export default 1;'}
            with tarfile.open(archive, 'w:gz') as package:
                for name, content in files.items():
                    info = tarfile.TarInfo(name)
                    body = content.encode()
                    info.size = len(body)
                    info.mode = 0o600
                    package.addfile(info, io.BytesIO(body))
            previous = os.umask(0o077)
            try:
                extract_source(archive, root / 'stage', '0.4.1')
            finally:
                os.umask(previous)
            self.assertEqual(stat.S_IMODE((root / 'stage').stat().st_mode), 0o700)
            for path in (root / 'stage').rglob('*'):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755 if path.is_dir() else 0o644, str(path))


if __name__ == '__main__':
    unittest.main(verbosity=2)
