import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.update_common import APP_ID, PROJECT_ID, UpdateError
from tools.export_source import build, project_files


class SourceExportTests(unittest.TestCase):
    def setUp(self):
        temporary_root = ROOT / '.local' / 'tmp'
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.write('.project-id', PROJECT_ID)
        self.write('VERSION', '0.3.6\n')
        self.write('app/main.py', 'value = 1\n')
        self.write('frontend/src/main.tsx', 'export default 1\n')
        self.write('frontend/package.json', '{}\n')
        self.write('frontend/pnpm-lock.yaml', 'lockfileVersion: 9\n')
        self.write('tools/verify_sample.py', 'synthetic = True\n')
        self.write('tools/dev/backend.cmd', '@echo off\n')
        self.write('README.md', '# Project\n')
        self.write('验收记录-v0.3.6.md', '# 验收\n')
        self.write('项目文件与清理说明.md', '# 源码\n')
        self.write('部署教程.txt', 'sudo bash scripts/deploy.sh\n')

    def write(self, name, value='private-or-generated'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding='utf-8')
        return path

    def names(self, archive):
        with zipfile.ZipFile(archive) as package:
            self.assertIsNone(package.testzip())
            return {name.removeprefix(f'{APP_ID}/') for name in package.namelist()}

    def test_keeps_source_lockfiles_tools_and_documentation(self):
        archive = build(self.root / '.release', self.root)
        self.assertTrue({'app/main.py', 'frontend/src/main.tsx', 'frontend/pnpm-lock.yaml',
                         'tools/verify_sample.py', 'tools/dev/backend.cmd', '验收记录-v0.3.6.md',
                         '项目文件与清理说明.md', '部署教程.txt'}.issubset(self.names(archive)))

    def test_excludes_dependencies_credentials_reports_and_local_data(self):
        excluded = ['.env', '部署信息.txt', '.venv/python.exe', '.local/dev/data.sqlite3',
                    '.git/config', 'data/assistant.sqlite3', 'secrets/app.key', 'backups/backup.zip',
                    'updates/request.json', 'frontend/node_modules/pkg/index.js', 'frontend/dist/index.html',
                    'frontend/tsconfig.tsbuildinfo', 'app/__pycache__/main.pyc', 'app/.env',
                    'tools/verification-backend.json', 'tools/verification-proxies.txt',
                    'tools/.env.py', 'tools/.venv/package.py', 'tools/.local/private.py',
                    'tools/secrets/passwords.py', 'tools/.pytest_cache/cache.md']
        for name in excluded:
            self.write(name)
        archive = build(self.root / '.release', self.root)
        self.assertTrue(self.names(archive).isdisjoint(excluded))
        with zipfile.ZipFile(archive) as package:
            self.assertFalse(any(b'private-or-generated' in package.read(name) for name in package.namelist()))

    def test_does_not_include_unrelated_root_files(self):
        self.write('notes.md')
        self.write('old-project.zip')
        self.assertNotIn('notes.md', dict((name, path) for path, name in project_files(self.root)))
        self.assertNotIn('old-project.zip', self.names(build(self.root / '.release', self.root)))

    def test_invalid_marker_does_not_create_export(self):
        self.write('.project-id', 'different-project')
        with self.assertRaises(UpdateError):
            build(self.root / '.release', self.root)
        self.assertFalse((self.root / '.release').exists())

    def test_invalid_version_is_rejected(self):
        self.write('VERSION', '../../private')
        with self.assertRaises(UpdateError):
            build(self.root / '.release', self.root)

    def test_output_inside_source_is_rejected(self):
        for directory in ('app', 'frontend', 'tools'):
            with self.subTest(directory=directory), self.assertRaises(UpdateError):
                build(self.root / directory / 'export', self.root)

    def test_outside_source_file_is_rejected(self):
        with patch('tools.export_source.source_files', return_value=[(ROOT / 'VERSION', 'app/outside.py')]):
            with self.assertRaises(UpdateError):
                build(self.root / '.release', self.root)

    def test_repeated_export_never_includes_old_archive(self):
        first = build(self.root / '.release', self.root)
        first_names = self.names(first)
        second = build(self.root / '.release', self.root)
        self.assertEqual(first, second)
        self.assertEqual(first_names, self.names(second))

    def test_failed_export_keeps_previous_archive_and_removes_temporary(self):
        archive = build(self.root / '.release', self.root)
        previous = archive.read_bytes()
        with patch('tools.export_source.zipfile.ZipFile.write', side_effect=OSError('Synthetic read failure')):
            with self.assertRaises(OSError):
                build(self.root / '.release', self.root)
        self.assertEqual(previous, archive.read_bytes())
        self.assertEqual([archive], list(archive.parent.iterdir()))


if __name__ == '__main__':
    unittest.main(verbosity=2)
