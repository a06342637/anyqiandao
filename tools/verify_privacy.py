"""Authentication, transport and backup confidentiality checks using synthetic data."""
import asyncio
import base64
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx

from app.backup_encryption import MAGIC, decrypt_bytes, encrypt_bytes, encrypt_file
from app.config import Config
from app.main import create_app
from tools.backup_fixture import PASSWORD, HEADERS
from tools import verify_remote_backup as backup_tests
from tools.verify_v3 import account


class PrivacyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = backup_tests.BackupTests.asyncSetUp
    asyncTearDown = backup_tests.BackupTests.asyncTearDown

    async def test_anonymous_gets_login_only_and_no_private_metadata(self):
        self.store.set_meta('settings', self.store.settings().model_copy(update={'site_name': 'private-fixture-site', 'site_icon_text': '私'}).model_dump_json())
        account(self.store, username='private-account-fixture')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, client=('198.51.100.25', 12345)), base_url='https://testserver') as visitor:
            for path in ('/', '/login', '/accounts', '/.env', '/data/assistant.sqlite3', '/secrets/app.key', '/docs'):
                response = await visitor.get(path)
                self.assertIn('管理员登录', response.text)
                for value in ('private-fixture-site', 'private-account-fixture', '/assets/', self.app.version):
                    self.assertNotIn(value, response.text)
                self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertNotIn('私', (await visitor.get('/favicon.svg')).text)
            self.assertEqual((await visitor.get('/healthz')).json(), {'status': 'ok'})
            for path in ('/api/v1/branding', '/api/v1/dashboard', '/api/v1/accounts', '/api/v1/logs', '/api/v1/updates',
                         '/api/v1/settings', '/api/v1/remote-backup', '/api/v1/backup', '/assets/private.js'):
                response = await visitor.get(path)
                self.assertEqual(response.status_code, 401, path)
                self.assertEqual(response.json(), {'detail': '请先登录管理页面'})
            response = await visitor.post('/api/v1/remote-backup', content=b'not JSON')
            self.assertEqual(response.status_code, 401)

    async def test_http_origin_cannot_be_upgraded_by_spoofed_forwarded_headers(self):
        config = Config(self.config.key, self.config.admin_hash, self.root / 'http-data', 'http://198.51.100.10:18780', start_worker=False)
        app = create_app(config)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('198.51.100.25', 12345)), base_url=config.public_url) as client:
            response = await client.get('/')
            self.assertEqual(response.status_code, 426)
            self.assertNotIn('<form', response.text)
            for headers in ({}, {'X-Forwarded-Proto': 'https'}, {'Host': 'localhost', 'Forwarded': 'proto=https'}):
                response = await client.post('/api/v1/auth/login', json={'password': 'synthetic-admin-password'}, headers=headers)
                self.assertEqual(response.status_code, 426)
            self.assertEqual(app.state.store.one('SELECT COUNT(*) AS count FROM sessions')['count'], 0)

    async def test_https_cookies_and_cross_site_requests(self):
        result = await self.client.post('/api/v1/auth/login', json={'password': 'synthetic-admin-password'})
        self.assertEqual(result.status_code, 200)
        cookie = result.headers['set-cookie'].lower()
        for attribute in ('secure', 'httponly', 'samesite=strict'):
            self.assertIn(attribute, cookie)
        self.assertIn('max-age=31536000', result.headers['strict-transport-security'])
        self.assertEqual((await self.client.get('/api/v1/accounts', headers={'Origin': 'https://evil.invalid'})).status_code, 403)
        self.assertEqual((await self.client.post('/api/v1/auth/login', json={'password': 'synthetic-admin-password'}, headers={'Sec-Fetch-Site': 'cross-site'})).status_code, 403)

    async def test_local_updater_probe_keeps_version_but_proxy_cannot_read_it(self):
        with patch('app.security.container_gateway', return_value='172.31.0.1'):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, client=('172.31.0.1', 12345)), base_url='http://127.0.0.1:18780') as client:
                self.assertEqual((await client.get('/healthz')).json()['version'], self.app.version)
                self.assertEqual((await client.get('/healthz', headers={'X-Forwarded-For': '198.51.100.25'})).json(), {'status': 'ok'})
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, client=('198.51.100.25', 12345)), base_url='http://127.0.0.1:18780') as visitor:
                self.assertEqual((await visitor.get('/healthz')).json(), {'status': 'ok'})

    async def test_logout_revokes_session_and_private_assets(self):
        old_cookie = self.client.cookies.get('any_assistant_session')
        response = await self.client.post('/api/v1/auth/logout')
        self.assertEqual(response.status_code, 200)
        self.assertIn('storage', response.headers['clear-site-data'])
        self.client.cookies.set('any_assistant_session', old_cookie)
        for path in ('/api/v1/accounts', '/api/v1/branding', '/assets/private.js'):
            self.assertEqual((await self.client.get(path)).status_code, 401)
        self.assertNotIn('/assets/', (await self.client.get('/')).text)

    async def test_encrypted_export_wrong_password_and_tampering(self):
        account(self.store, username='private-backup-account', password='private-fixture-password')
        self.assertEqual((await self.client.post('/api/v1/backup')).status_code, 422)
        response = await self.client.post('/api/v1/backup', json={'passphrase': PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(MAGIC))
        self.assertIn('.asb', response.headers['content-disposition'])
        for value in (b'private-backup-account', b'private-fixture-password', PASSWORD.encode()):
            self.assertNotIn(value, response.content)
        wrong = {'X-Backup-Password': base64.b64encode(b'wrong-test-password').decode()}
        self.assertEqual((await self.client.post('/api/v1/backup/restore', content=response.content, headers=wrong)).status_code, 400)
        tampered = response.content[:-1] + bytes([response.content[-1] ^ 1])
        self.assertEqual((await self.client.post('/api/v1/backup/restore', content=tampered, headers=HEADERS)).status_code, 400)
        self.assertEqual((await self.client.post('/api/v1/backup/restore', content=response.content, headers=HEADERS)).status_code, 200)
        stored = self.store.all('SELECT login_enc,result_enc FROM accounts')
        self.assertNotIn('private-fixture-password', json.dumps(stored))
        self.assertNotIn('private-backup-account', json.dumps(stored))

    async def test_encryption_is_random_and_streaming_format_is_compatible(self):
        body = b'synthetic confidential data' * 1000
        first, second = encrypt_bytes(body, PASSWORD), encrypt_bytes(body, PASSWORD)
        self.assertNotEqual(first, second)
        self.assertEqual(decrypt_bytes(first, PASSWORD), body)
        source, destination = self.root / 'plain.zip', self.root / 'encrypted.asb'
        source.write_bytes(body)
        encrypt_file(source, destination, PASSWORD)
        self.assertEqual(decrypt_bytes(destination.read_bytes(), PASSWORD), body)
        with self.assertRaises(ValueError):
            decrypt_bytes(b'not an encrypted archive', PASSWORD)

    async def test_old_automatic_backup_requires_encryption_before_uploading(self):
        self.settings.enabled = True
        self.settings.encryption_password = ''
        self.store.set_meta('remote_backup_config', self.store.vault.seal(self.settings.model_dump(), 'remote_backup_config'))
        await self.service.start()
        self.assertFalse(self.service.settings().enabled)
        self.assertIn('备份密码', self.service.state()['result'])
        self.assertIsNone(self.service.state()['next_run'])
        self.assertIsNone(self.service.task)

    async def test_saved_encryption_password_is_not_returned_or_exported(self):
        result = self.service.save(self.settings)
        self.assertTrue(result['settings']['has_encryption_password'])
        self.assertNotIn(PASSWORD, json.dumps(result))
        self.assertNotIn(PASSWORD, self.store.meta('remote_backup_config'))
        self.settings.encryption_password = ''
        self.service.save(self.settings)
        self.assertEqual(self.service.settings().encryption_password, PASSWORD)


if __name__ == '__main__':
    unittest.main(verbosity=2)
