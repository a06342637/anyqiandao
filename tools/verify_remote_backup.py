"""Synthetic backup checks, including a real local SSH/SFTP server; no cloud credentials."""
import asyncio
import io
import json
import sqlite3
import sys
import tempfile
import time
import unittest
import zipfile
from contextlib import asynccontextmanager, closing
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import asyncssh
import httpx
from argon2 import PasswordHasher

from tools.backup_fixture import PASSWORD, HEADERS
from app.backup_targets import BackupError, OSSTarget, open_target, owned_backup
from app.config import Config
from app.main import create_app
from app.remote_backup import RemoteBackup, create_archive, next_run
from app.remote_backup_schema import OSSSettings, RemoteBackupSettings, SFTPSettings


class BackupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 10
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = Config(bytes(range(32)), PasswordHasher().hash('synthetic-admin-password'), self.root / 'data',
                             'https://testserver', start_worker=False)
        self.app = create_app(self.config)
        self.service = self.app.state.remote_backup
        self.store = self.app.state.store
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='https://testserver')
        response = await self.client.post('/api/v1/auth/login', json={'password': 'synthetic-admin-password'})
        self.client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        self.settings = RemoteBackupSettings(encryption_password=PASSWORD, oss=OSSSettings(enabled=True, bucket='synthetic-bucket',
                                             access_key_id='synthetic-id', access_key_secret='synthetic-oss-secret'))

    async def asyncTearDown(self):
        await self.service.stop()
        await self.client.aclose()
        self.temporary.cleanup()

    async def test_default_and_custom_update_repository(self):
        self.assertEqual((await self.client.get('/api/v1/updates')).json()['repository'], 'a06342637/anyqiandao')
        result = await self.client.put('/api/v1/updates/source', json={'repository': 'https://github.com/example/custom.git'})
        self.assertEqual(result.json()['repository'], 'example/custom')
        self.store.set_meta('update_repository', '')
        self.assertEqual((await self.client.get('/api/v1/updates')).json()['repository'], 'a06342637/anyqiandao')
        result = await self.client.put('/api/v1/updates/source', json={'repository': ''})
        self.assertEqual(result.json()['repository'], 'a06342637/anyqiandao')

    async def test_secrets_are_encrypted_masked_and_preserved(self):
        self.settings.sftp = SFTPSettings(password='synthetic-ssh-secret', private_key='synthetic-private-key', passphrase='synthetic-passphrase')
        response = await self.client.put('/api/v1/remote-backup', json=self.settings.model_dump())
        self.assertEqual(response.status_code, 200, response.text)
        for secret in ('synthetic-oss-secret', 'synthetic-ssh-secret', 'synthetic-private-key', 'synthetic-passphrase'):
            self.assertNotIn(secret, response.text)
            self.assertNotIn(secret, self.store.meta('remote_backup_config'))
            self.assertNotIn(secret, json.dumps(self.store.all('SELECT * FROM logs')))
        self.assertTrue(response.json()['settings']['oss']['has_access_key_secret'])
        self.settings.oss.access_key_secret = ''
        self.settings.sftp.password = ''
        self.service.save(self.settings)
        self.assertEqual(self.service.settings().oss.access_key_secret, 'synthetic-oss-secret')
        self.assertEqual(self.service.settings().sftp.password, 'synthetic-ssh-secret')

    async def test_auth_csrf_and_validation(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='https://testserver') as anonymous:
            self.assertEqual((await anonymous.get('/api/v1/remote-backup')).status_code, 401)
        token = self.client.headers.pop('X-CSRF-Token')
        self.assertEqual((await self.client.post('/api/v1/remote-backup/run')).status_code, 403)
        self.client.headers['X-CSRF-Token'] = token
        bad = self.settings.model_dump()
        bad['oss']['keep'] = 0
        self.assertEqual((await self.client.put('/api/v1/remote-backup', json=bad)).status_code, 422)
        self.assertEqual((await self.client.put('/api/v1/remote-backup', json={'enabled': True})).status_code, 409)
        with self.assertRaises(ValueError):
            OSSSettings(endpoint='https://user:secret@example.com/path')
        with self.assertRaises(ValueError):
            SFTPSettings(directory='/backups/../etc')

    async def test_manual_backup_roundtrip_zip_and_cleanup(self):
        self.service.save(self.settings)
        response = await self.client.post('/api/v1/accounts/import', json={'import_id': 'synthetic-backup-import', 'accounts': [{'username': 'backup-fixture', 'password': 'fixture-password', 'row_id': 'backup-test'}]})
        self.assertEqual(response.status_code, 200, response.text)
        captured = []
        gate = asyncio.Event()
        class Target:
            async def upload(inner, path, name):
                await gate.wait()
                captured.append(path.read_bytes())
                self.assertTrue(owned_backup(name, self.service.instance))
            async def prune(inner, instance):
                return 2
        @asynccontextmanager
        async def target(*args):
            yield Target()
        with patch.object(self.service, 'target', target):
            response = await self.client.post('/api/v1/remote-backup/run')
            self.assertEqual(response.status_code, 202)
            self.assertEqual((await self.client.post('/api/v1/remote-backup/run')).status_code, 409)
            self.assertEqual((await self.client.put('/api/v1/remote-backup', json=self.settings.model_dump())).status_code, 409)
            gate.set()
            await self.service.task
        self.assertEqual(self.service.state()['status'], 'success')
        self.assertFalse(list(self.config.data_dir.glob('.backup-*')))
        response = await self.client.post('/api/v1/backup/restore', content=captured[0], headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['accounts_updated'], 1)
        logs = (await self.client.get('/api/v1/logs?category=backup')).json()
        self.assertGreater(logs['total'], 2)
        self.assertTrue(all(item['category'] == 'backup' for item in logs['items']))
        self.store.execute("UPDATE logs SET created=? WHERE category='backup'", (time.time() - 4000 * 86400,))
        self.store.cleanup()
        self.assertEqual((await self.client.get('/api/v1/logs?category=backup')).json()['total'], 0)

    async def test_failed_upload_never_prunes_and_does_not_leak_exception(self):
        self.service.save(self.settings)
        target = AsyncMock()
        target.upload.side_effect = RuntimeError('synthetic-oss-secret in provider response')
        @asynccontextmanager
        async def connect(*args):
            yield target
        with patch.object(self.service, 'target', connect):
            self.service.launch()
            await self.service.task
        target.prune.assert_not_called()
        self.assertEqual(self.service.state()['status'], 'error')
        self.assertNotIn('synthetic-oss-secret', json.dumps(self.service.public()))
        self.assertNotIn('synthetic-oss-secret', json.dumps(self.store.all('SELECT * FROM logs')))

    async def test_one_failed_target_does_not_prevent_the_other(self):
        self.settings.sftp = SFTPSettings(enabled=True, host='localhost', password='synthetic')
        self.service.save(self.settings)
        oss, sftp = AsyncMock(), AsyncMock()
        oss.upload.side_effect = OSError('synthetic network failure')
        sftp.prune.return_value = 0
        @asynccontextmanager
        async def connect(kind, settings):
            yield oss if kind == 'oss' else sftp
        with patch.object(self.service, 'target', connect):
            self.service.launch()
            await self.service.task
        sftp.upload.assert_awaited_once()
        self.assertEqual(self.service.state()['status'], 'warning')

    async def test_retention_failure_reports_successful_upload_as_warning(self):
        self.service.save(self.settings)
        target = AsyncMock()
        target.prune.side_effect = OSError('denied')
        @asynccontextmanager
        async def connect(*args):
            yield target
        with patch.object(self.service, 'target', connect):
            self.service.launch()
            await self.service.task
        self.assertEqual(self.service.state()['status'], 'warning')
        self.assertIn('已上传', self.service.state()['targets']['oss']['message'])

    async def test_schedule_restart_catchup_once_and_disable(self):
        self.settings.enabled = True
        self.service.save(self.settings)
        state = self.service.state()
        state.update(next_run=time.time() - 10 * 86400, running=True)
        self.service.write_state(state)
        restarted = RemoteBackup(self.store, self.config)
        target = AsyncMock()
        target.prune.return_value = 0
        @asynccontextmanager
        async def connect(*args):
            yield target
        with patch.object(restarted, 'target', connect):
            await restarted.start()
            await asyncio.sleep(0)
            await restarted.task
            await restarted.stop()
        self.assertEqual(target.upload.await_count, 1)
        self.assertGreater(restarted.state()['next_run'], time.time())
        self.assertEqual(restarted.state()['source'], 'scheduled')
        self.settings.enabled = False
        restarted.save(self.settings)
        self.assertIsNone(restarted.state()['next_run'])
        self.assertEqual(restarted.settings().oss.access_key_secret, 'synthetic-oss-secret')

    async def test_manual_backup_keeps_next_schedule(self):
        self.settings.enabled = True
        self.service.save(self.settings)
        planned = self.service.state()['next_run']
        target = AsyncMock()
        target.prune.return_value = 0
        @asynccontextmanager
        async def connect(*args):
            yield target
        with patch.object(self.service, 'target', connect):
            self.service.launch()
            await self.service.task
        self.assertEqual(self.service.state()['next_run'], planned)

    async def test_full_archive_excludes_remote_credentials_and_logs(self):
        self.service.save(self.settings)
        self.store.log('system', 'log-must-not-be-exported')
        archive_path = create_archive(self.store, self.config, 'full', self.root, 'full.zip')
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            for name in ('backup.json', 'data/assistant.sqlite3', 'secrets/app.key', 'secrets/admin.hash', 'Dockerfile', 'scripts/deploy.sh'):
                self.assertIn(name, names)
            for name in names:
                if not name.endswith('.png'):
                    self.assertNotIn(b'synthetic-oss-secret', archive.read(name))
            db = self.root / 'restored.sqlite3'
            db.write_bytes(archive.read('data/assistant.sqlite3'))
        with closing(sqlite3.connect(db)) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM logs').fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'remote_backup_%'").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT value FROM meta WHERE key='queue_paused'").fetchone()[0], '1')
            self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    async def test_invalid_zip_does_not_change_data(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('../../backup.json', '{}')
        response = await self.client.post('/api/v1/backup/restore', content=data.getvalue())
        self.assertEqual(response.status_code, 400)

    async def test_oss_v4_endpoint_probe_and_retention_scope(self):
        settings = self.settings.oss.model_copy(update={'internal': True, 'keep': 1})
        target = OSSTarget(settings)
        self.assertIn('-internal.aliyuncs.com', target.bucket.endpoint)
        instance = self.service.instance
        names = [f'any-signin-{instance}-20261003T04300{i}000000Z-123456ab-app.zip' for i in range(3)]
        from types import SimpleNamespace
        objects = [SimpleNamespace(key=settings.prefix + name) for name in names + ['unrelated.zip', 'any-signin-other-20261003T043000000000Z-123456ab-app.zip']]
        with patch('app.backup_targets.oss2.ObjectIterator', return_value=objects), patch.object(target.bucket, 'delete_object') as delete:
            self.assertEqual(await target.prune(instance), 2)
            self.assertEqual({call.args[0] for call in delete.call_args_list}, {settings.prefix + name for name in names[:2]})
        with patch.object(target.bucket, 'put_object'), patch.object(target.bucket, 'list_objects'), patch.object(target.bucket, 'delete_object') as delete:
            await target.probe()
            self.assertTrue(delete.call_args.args[0].startswith(settings.prefix + '.any-signin-probe-'))

    async def test_local_sftp_password_key_upload_browse_retention_and_pinning(self):
        remote = self.root / 'remote'
        remote.mkdir()
        client_key = asyncssh.generate_private_key('ssh-ed25519')
        class Server(asyncssh.SSHServer):
            def begin_auth(inner, username): return True
            def password_auth_supported(inner): return True
            def validate_password(inner, username, password): return username == 'fixture' and password == 'fixture-password'
            def public_key_auth_supported(inner): return True
            def validate_public_key(inner, username, key): return key == client_key.convert_to_public()
        host_key = asyncssh.generate_private_key('ssh-ed25519')
        server = await asyncssh.create_server(Server, '127.0.0.1', 0, server_host_keys=[host_key],
                                              sftp_factory=lambda channel: asyncssh.SFTPServer(channel, chroot=str(remote)))
        settings = SFTPSettings(host='127.0.0.1', port=server.get_port(), username='fixture', password='fixture-password', directory='/backups', keep=1)
        pins = []
        path = self.root / 'payload.zip'
        path.write_bytes(b'synthetic archive')
        instance = self.service.instance
        names = [f'any-signin-{instance}-20261003T04300{i}000000Z-123456ab-app.zip' for i in range(3)]
        try:
            async with open_target('sftp', settings, remember=pins.append) as target:
                await target.probe()
                for name in names:
                    await target.upload(path, name)
                (remote / 'backups' / 'other.zip').write_bytes(b'keep')
                self.assertEqual(await target.prune(instance), 2)
                self.assertEqual(set(p.name for p in (remote / 'backups').iterdir()), {names[-1], 'other.zip'})
                self.assertIn('/backups', (await target.browse('/'))['directories'])
            self.assertEqual(pins, [host_key.export_public_key().decode().strip()])
            key_settings = settings.model_copy(update={'auth': 'private_key', 'private_key': client_key.export_private_key().decode(), 'password': ''})
            async with open_target('sftp', key_settings, trusted=pins[0]) as target:
                await target.probe()
            wrong_key = asyncssh.generate_private_key('ssh-ed25519').export_public_key().decode().strip()
            with self.assertRaises(asyncssh.HostKeyNotVerifiable):
                async with open_target('sftp', settings, trusted=wrong_key):
                    self.fail('changed host key accepted')
            failed_pins = []
            with self.assertRaises(asyncssh.PermissionDenied):
                async with open_target('sftp', settings.model_copy(update={'password': 'wrong'}), remember=failed_pins.append):
                    pass
            self.assertFalse(failed_pins)
        finally:
            server.close()
            await server.wait_closed()


class ScheduleTests(unittest.TestCase):
    def stamp(self, value, zone='Asia/Shanghai'):
        return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(zone)).timestamp()

    def test_daily_and_interval_anchor(self):
        settings = RemoteBackupSettings(every=2)
        self.assertEqual(next_run(settings, self.stamp('2026-10-03T04:29')), self.stamp('2026-10-03T04:30'))
        self.assertEqual(next_run(settings, self.stamp('2026-10-03T04:31')), self.stamp('2026-10-05T04:30'))
        self.assertEqual(next_run(settings, self.stamp('2026-10-10T12:00'), self.stamp('2026-10-03T04:30')), self.stamp('2026-10-11T04:30'))

    def test_hourly_schedule(self):
        settings = RemoteBackupSettings(every=6, unit='hours')
        self.assertEqual(next_run(settings, self.stamp('2026-10-03T05:00')), self.stamp('2026-10-03T10:30'))

    def test_dst_daily_keeps_wall_clock(self):
        zone = 'America/Los_Angeles'
        settings = RemoteBackupSettings(timezone=zone)
        self.assertEqual(next_run(settings, self.stamp('2026-03-07T05:00', zone)), self.stamp('2026-03-08T04:30', zone))


if __name__ == '__main__':
    unittest.main(verbosity=2)
