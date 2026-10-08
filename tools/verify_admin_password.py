"""Administrator password changes with synthetic credentials and isolated files."""
import asyncio
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import redirect_stdout
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from argon2 import PasswordHasher
from app.config import Config
from app.crypto import Vault
from app.db import Store
from app.main import create_app
from app.manage import PasswordInputError, initialize, main, read_reset_password, reset_password
from app.passwords import ADMIN_HASH_CONTEXT, effective_admin_hash
from app.remote_backup import create_archive
from tools import verify_remote_backup as fixtures
from tools.verify_v3 import account


class WebPasswordTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.BackupTests.asyncSetUp
    asyncTearDown = fixtures.BackupTests.asyncTearDown

    async def test_five_digit_password_without_old_password_is_persistent(self):
        identifier = account(self.store)
        original = self.store.account(identifier)
        token = self.client.cookies.get('any_assistant_session')
        response = await self.client.put('/api/v1/settings/admin-password', json={'new_password': '86420'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('storage', response.headers['clear-site-data'])
        self.assertEqual(self.store.one('SELECT COUNT(*) AS count FROM sessions')['count'], 0)
        self.assertNotIn('86420', self.store.meta(ADMIN_HASH_CONTEXT))
        self.assertNotIn('86420', response.text)
        self.assertEqual(self.store.account(identifier)['login_enc'], original['login_enc'])
        self.assertEqual(self.store.account(identifier)['result_enc'], original['result_enc'])
        self.assertEqual(self.store.vault.key, self.config.key)
        self.client.cookies.set('any_assistant_session', token)
        self.assertEqual((await self.client.get('/api/v1/auth/me')).status_code, 401)
        self.assertEqual((await self.client.post('/api/v1/auth/login', json={'password': 'synthetic-admin-password'})).status_code, 401)
        restarted = create_app(self.config)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url='https://testserver') as client:
            self.assertEqual((await client.post('/api/v1/auth/login', json={'password': '86420'})).status_code, 200)
            settings = await client.get('/api/v1/settings')
            self.assertNotIn(ADMIN_HASH_CONTEXT, settings.text)

    async def test_auth_csrf_and_length_limits(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='https://testserver') as visitor:
            self.assertEqual((await visitor.put('/api/v1/settings/admin-password', json={'new_password': '86420'})).status_code, 401)
        for password in ('', '1234', 'x' * 1025):
            response = await self.client.put('/api/v1/settings/admin-password', json={'new_password': password})
            self.assertEqual(response.status_code, 422)
        token = self.client.headers.pop('X-CSRF-Token')
        self.assertEqual((await self.client.put('/api/v1/settings/admin-password', json={'new_password': '86420'})).status_code, 403)
        self.client.headers['X-CSRF-Token'] = token
        self.assertEqual((await self.client.put('/api/v1/settings/admin-password', json={'new_password': '86420'}, headers={'Origin': 'https://evil.invalid'})).status_code, 403)
        self.assertEqual(self.store.meta(ADMIN_HASH_CONTEXT), '')

    async def test_password_reset_clears_old_login_throttling(self):
        self.app.state.auth.attempts['127.0.0.1'].extend([time.monotonic()] * 10)
        response = await self.client.put('/api/v1/settings/admin-password', json={'new_password': '86420'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await self.client.post('/api/v1/auth/login', json={'password': '86420'})).status_code, 200)

    async def test_inflight_old_password_login_cannot_create_a_session_after_reset(self):
        auth = self.app.state.auth
        original = auth.hasher
        entered, release = threading.Event(), threading.Event()
        def verify(encoded, password):
            entered.set()
            release.wait(10)
            return original.verify(encoded, password)
        auth.hasher = SimpleNamespace(verify=verify, hash=original.hash)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='https://testserver') as visitor:
            pending = asyncio.create_task(visitor.post('/api/v1/auth/login', json={'password': 'synthetic-admin-password'}))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                response = await self.client.put('/api/v1/settings/admin-password', json={'new_password': '86420'})
                self.assertEqual(response.status_code, 200)
            finally:
                release.set()
                auth.hasher = original
            self.assertEqual((await pending).status_code, 401)
        self.assertEqual(self.store.one('SELECT COUNT(*) AS count FROM sessions')['count'], 0)

    async def test_revoked_session_cannot_finish_pending_password_change(self):
        auth = self.app.state.auth
        original = auth.hasher
        entered, release = threading.Event(), threading.Event()
        def hashing(password):
            entered.set()
            release.wait(10)
            return original.hash(password)
        auth.hasher = SimpleNamespace(verify=original.verify, hash=hashing)
        pending = asyncio.create_task(self.client.put('/api/v1/settings/admin-password', json={'new_password': '86420'}))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            self.store.execute('DELETE FROM sessions')
        finally:
            release.set()
            auth.hasher = original
        self.assertEqual((await pending).status_code, 401)
        self.assertEqual(self.store.meta(ADMIN_HASH_CONTEXT), '')

    async def test_full_backup_preserves_current_admin_password(self):
        import zipfile
        await self.client.put('/api/v1/settings/admin-password', json={'new_password': '86420'})
        archive = create_archive(self.store, self.config, 'full', self.root, 'full.zip')
        with zipfile.ZipFile(archive) as package:
            hashed = package.read('secrets/admin.hash').decode().strip()
            self.assertTrue(PasswordHasher().verify(hashed, '86420'))
            document = json.loads(package.read('backup.json'))
            self.assertNotIn(ADMIN_HASH_CONTEXT, document)


class CommandPasswordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hash_path = self.root / 'admin.hash'
        self.config = Config(bytes(range(32)), PasswordHasher().hash('original-synthetic-password'), self.root / 'data', start_worker=False)
        self.hash_path.write_text(self.config.admin_hash + '\n')
        self.store = Store(self.config.data_dir, Vault(self.config.key))
        self.store.execute('INSERT INTO sessions VALUES (?,?)', ('synthetic-session', 9999999999))

    def run_reset(self, password):
        with patch('app.manage.Config.from_env', return_value=self.config), patch.dict(os.environ, {'APP_ADMIN_PASSWORD_HASH_FILE': str(self.hash_path)}), patch('sys.stdin', io.StringIO(password)), redirect_stdout(io.StringIO()) as output:
            reset_password(SimpleNamespace(password_stdin=True))
        return output.getvalue()

    def test_cli_five_characters_clear_web_override_and_preserve_key(self):
        self.store.set_meta(ADMIN_HASH_CONTEXT, self.store.vault.seal(PasswordHasher().hash('web-synthetic-password'), ADMIN_HASH_CONTEXT))
        output = self.run_reset('86420')
        self.assertTrue(PasswordHasher().verify(self.hash_path.read_text().strip(), '86420'))
        self.assertEqual(self.store.meta(ADMIN_HASH_CONTEXT), '')
        self.assertEqual(self.store.one('SELECT COUNT(*) AS count FROM sessions')['count'], 0)
        self.assertNotIn('86420', output)
        self.assertEqual(self.store.vault.key, self.config.key)
        self.assertFalse(list(self.root.glob('.admin-reset-*')))
        if os.name != 'nt':
            self.assertEqual(self.hash_path.stat().st_mode & 0o777, 0o600)

    def test_invalid_length_leaves_hash_and_sessions_untouched(self):
        original = self.hash_path.read_bytes()
        for password in ('', '1234', 'x' * 1025):
            with self.assertRaises(PasswordInputError):
                self.run_reset(password)
            self.assertEqual(self.hash_path.read_bytes(), original)
            self.assertEqual(self.store.one('SELECT COUNT(*) AS count FROM sessions')['count'], 1)

    def test_mismatch_and_eof_are_controlled(self):
        with patch('app.manage.getpass.getpass', side_effect=['86420', '13579']), self.assertRaises(PasswordInputError):
            read_reset_password(SimpleNamespace(password_stdin=False))
        with patch('app.manage.getpass.getpass', side_effect=EOFError()), self.assertRaises(PasswordInputError):
            read_reset_password(SimpleNamespace(password_stdin=False))
        with patch('app.manage.Config.from_env', return_value=self.config), patch('app.manage.getpass.getpass', side_effect=EOFError()), patch.object(sys, 'argv', ['manage', 'reset-password']), self.assertRaises(SystemExit) as result:
            main()
        self.assertIn('未能安全读取', str(result.exception))

    def test_failed_atomic_replace_preserves_original(self):
        original = self.hash_path.read_bytes()
        with patch('app.manage.os.replace', side_effect=OSError('synthetic failure')), self.assertRaises(OSError):
            self.run_reset('86420')
        self.assertEqual(self.hash_path.read_bytes(), original)
        self.assertEqual(self.store.one('SELECT COUNT(*) AS count FROM sessions')['count'], 1)
        self.assertFalse(list(self.root.glob('.admin-reset-*')))

    def test_initial_setup_accepts_five_characters_and_rejects_four(self):
        for password, accepted in (('86420', True), ('五个中文字', True), ('1234', False)):
            root = self.root / ('init-' + str(len(password)) + '-' + str(password.isascii()))
            args = SimpleNamespace(root=root, public_url='http://127.0.0.1:8001', allow_http=True, port=8001,
                                   username='admin', owner=None, password_stdin=True, bind_host='127.0.0.1', https_proxy=False)
            with patch('sys.stdin', io.StringIO(password)), redirect_stdout(io.StringIO()):
                if accepted:
                    initialize(args)
                    self.assertTrue(PasswordHasher().verify((root / 'secrets/admin.hash').read_text().strip(), password))
                else:
                    with self.assertRaises(PasswordInputError):
                        initialize(args)
                    self.assertFalse(root.exists())


@unittest.skipUnless(os.name == 'posix' and getattr(os, 'geteuid', lambda: -1)() == 0, 'Shell reset tested as root in Linux CI')
class ShellResetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('scripts', 'bin', 'secrets', 'data'):
            (self.root / name).mkdir()
        shutil.copy2(ROOT / 'scripts/reset-password.sh', self.root / 'scripts/reset-password.sh')
        (self.root / '.project-id').write_text('any-signin-assistant-managed-v1')
        for name in ('.env', 'secrets/app.key', 'secrets/admin.hash', 'data/assistant.sqlite3'):
            (self.root / name).write_text('synthetic')
        stub = self.root / 'bin/docker'
        stub.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
root=pathlib.Path.cwd()
args=sys.argv[1:]
with (root/'commands.jsonl').open('a') as f: f.write(json.dumps(args)+'\\n')
if args[0]=='inspect':
    field=args[args.index('--format')+1]
    print(str(root) if 'working_dir' in field else 'sha256:synthetic-image' if field=='{{.Image}}' else 'any-signin-assistant:0.5.3-fixture')
elif args[0]=='compose' and 'ps' in args: print('123456abcdef')
elif args[0]=='run':
    (root/'received-password').write_text(sys.stdin.read())
    if os.environ.get('RESET_FAIL'): sys.exit(9)
''')
        stub.chmod(0o755)
        self.environment = os.environ.copy()
        self.environment['PATH'] = str(self.root / 'bin') + os.pathsep + self.environment['PATH']

    def interactive(self):
        from tools.verify_deploy import BashDeploymentTests
        # Its PTY driver is generic except for the entrypoint path.
        shutil.copy2(self.root / 'scripts/reset-password.sh', self.root / 'scripts/deploy.sh')
        return BashDeploymentTests.interactive(self, [('不回显）：', '1234'), ('不回显）：', '86420'), ('再次输入新密码：', '86420')])

    def test_shell_uses_current_image_without_docker_tty_and_recreates(self):
        code, output = self.interactive()
        self.assertEqual(code, 0, output)
        self.assertEqual((self.root / 'received-password').read_text(), '86420')
        commands = [json.loads(line) for line in (self.root / 'commands.jsonl').read_text().splitlines()]
        run = next(command for command in commands if command[0] == 'run')
        self.assertIn('sha256:synthetic-image', run)
        self.assertIn('--password-stdin', run)
        self.assertNotIn('-it', run)
        self.assertNotIn('86420', output)
        self.assertNotIn('86420', json.dumps(commands))
        self.assertTrue(any('--force-recreate' in command for command in commands))

    def test_helper_failure_restores_app(self):
        self.environment['RESET_FAIL'] = '1'
        code, output = self.interactive()
        self.assertNotEqual(code, 0)
        commands = [json.loads(line) for line in (self.root / 'commands.jsonl').read_text().splitlines()]
        self.assertIn('--force-recreate', commands[-1])
        self.assertNotIn('86420', output)

    def test_noninteractive_input_fails_before_stopping_app(self):
        result = subprocess.run(['bash', 'scripts/reset-password.sh'], cwd=self.root, env=self.environment, input='86420', text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('交互式', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertFalse((self.root / 'commands.jsonl').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
