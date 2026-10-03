import argparse
import asyncio
import base64
import configparser
import contextlib
from datetime import datetime
import hashlib
import io
import json
import os
import shlex
import sqlite3
import sys
import tarfile
import tempfile
import time
import unittest
import uuid
from pathlib import Path, PurePosixPath
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / '.local'
LOCAL.mkdir(exist_ok=True)
sys.path.insert(0, str(ROOT))

import httpx
from argon2 import PasswordHasher
from app import update_common as common
from tools.backup_fixture import export_document
from app.config import Config
from app.crypto import Vault
from app.db import SCHEMA_VERSION, Store
from app.errors import TaskError
from app.main import create_app
from app.manage import initialize
from app.router_service import quota_of
from scripts.update_runner import Updater


def account(store, username='synthetic', validity='valid', password='test-account-password'):
    identifier = uuid.uuid4().hex
    now = time.time()
    store.execute('INSERT INTO accounts(id,login_hash,login_enc,result_enc,validity,quota,created,updated) VALUES (?,?,?,?,?,?,?,?)',
                  (identifier, store.vault.fingerprint(username), store.vault.seal({'username': username, 'password': password}, f'login:{identifier}'),
                   store.vault.seal({'session': 'expired' if validity == 'invalid' else 'valid', 'api_user': '42'}, f'result:{identifier}'), validity, 999.0, now, now))
    return identifier


class FakeService:
    def __init__(self):
        self.extracts = 0
        self.checkins = 0
        self.always_invalid = False
        self.network_error = False
        self.result = {'code': 'signed', 'message': '签到成功', 'quota': 12.5, 'quota_before': 10.0, 'quota_after': 12.5, 'logs': []}

    async def extract(self, username, password, route, settings):
        self.extracts += 1
        return {'session': 'fresh', 'api_user': '42', 'quota': 10.0}

    async def validate(self, credentials, route, settings):
        return {'quota': 10.0}

    async def checkin(self, credentials, route, settings, *, before_submit=None):
        self.checkins += 1
        if self.network_error:
            raise TaskError('network_error', '模拟网络失败')
        if self.always_invalid or credentials['session'] == 'expired':
            raise TaskError('invalid', '模拟凭证失效')
        return self.result.copy()


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 10
        self.temporary = tempfile.TemporaryDirectory(prefix='v3-api-', dir=LOCAL)
        self.root = Path(self.temporary.name)
        for name in ('requests', 'status'):
            (self.root / 'updates' / name).mkdir(parents=True)
        self.config = Config(bytes(range(32)), PasswordHasher().hash('test-admin-password'), self.root / 'data',
                             'https://testserver', start_worker=False, admin_username='owner', update_dir=self.root / 'updates')
        self.service = FakeService()
        self.app = create_app(self.config, self.service)
        self.store, self.engine = self.app.state.store, self.app.state.engine
        self.store.set_meta('settings', self.store.settings().model_copy(update={'proxy_mode': 'direct', 'auto_checkin': False, 'account_gap': 0}).model_dump_json())
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='https://testserver')
        response = await self.client.post('/api/v1/auth/login', json={'username': 'owner', 'password': 'test-admin-password'})
        self.assertEqual(response.status_code, 200)
        self.client.headers['X-CSRF-Token'] = response.json()['csrf_token']

    async def asyncTearDown(self):
        await self.client.aclose()
        self.assertTrue(self.root.resolve().is_relative_to(LOCAL.resolve()))
        self.temporary.cleanup()

    async def perform(self, identifier, kind='checkin'):
        response = await self.client.post('/api/v1/accounts/actions', json={'ids': [identifier], 'action': kind})
        self.assertEqual(response.status_code, 200, response.text)
        job_id = response.json()['job_ids'][0]
        await self.engine.process(self.engine.claim())
        return self.store.one('SELECT * FROM jobs WHERE id=?', (job_id,))

    async def test_custom_username_and_csrf(self):
        response = await self.client.post('/api/v1/auth/login', json={'username': 'admin', 'password': 'test-admin-password'})
        self.assertEqual(response.status_code, 401)
        self.assertEqual((await self.client.get('/api/v1/auth/me')).json()['username'], 'owner')
        self.client.headers['X-CSRF-Token'] = 'wrong'
        self.assertEqual((await self.client.post('/api/v1/queue/pause')).status_code, 403)

    async def test_live_balance_not_cached_and_job_feedback(self):
        identifier = account(self.store)
        job = await self.perform(identifier)
        balance = self.store.checkin_balance(account_id=identifier)
        self.assertEqual((balance['quota_before'], balance['quota_after'], balance['earned']), (10, 12.5, 2.5))
        self.assertEqual(balance['balance_source'], 'live')
        self.assertIn('签到前 $10.0000', job['message'])
        self.assertNotIn('999', job['message'])
        status = (await self.client.get('/api/v1/jobs/status', params={'ids': job['id']})).json()['items'][0]
        self.assertEqual(status['balance']['earned'], 2.5)
        stats = (await self.client.get('/api/v1/stats')).json()
        self.assertEqual((stats['signed'], stats['earned']), (1, 2.5))
        self.assertEqual((await self.client.get('/api/v1/accounts')).json()['items'][0]['last_balance']['earned'], 2.5)

    async def test_branding_is_private_persistent_and_in_backup(self):
        branding = {'site_name': '我的签到工作空间', 'site_icon_text': '云签'}
        response = await self.client.put('/api/v1/settings/branding', json=branding)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.settings().proxy_mode, 'direct')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='https://testserver') as visitor:
            self.assertEqual((await visitor.get('/api/v1/branding')).status_code, 401)
            self.assertEqual((await visitor.put('/api/v1/settings/branding', json=branding)).status_code, 401)
            icon = await visitor.get('/favicon.svg')
            self.assertEqual(icon.status_code, 200)
            self.assertIn('image/svg+xml', icon.headers['content-type'])
            self.assertEqual(ElementTree.fromstring(icon.text).find('{http://www.w3.org/2000/svg}text').text, '·')
        self.assertEqual((await self.client.get('/api/v1/auth/me')).json()['name'], branding['site_name'])
        backup = await export_document(self.client)
        await self.client.put('/api/v1/settings/branding', json={'site_name': '暂时改名', 'site_icon_text': '<&'})
        icon = await self.client.get('/favicon.svg')
        self.assertEqual(ElementTree.fromstring(icon.text).find('{http://www.w3.org/2000/svg}text').text, '<&')
        restored = await self.client.post('/api/v1/backup/restore', json=backup)
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual((await self.client.get('/api/v1/branding')).json(), branding)
        reloaded = Store(self.config.data_dir, Vault(self.config.key))
        self.assertEqual(reloaded.settings().site_name, branding['site_name'])

    async def test_branding_rejects_blank_control_and_oversized_values(self):
        for invalid in ({'site_name': ' '}, {'site_name': 'x' * 41}, {'site_name': 'bad\nname'}, {'site_icon_text': '三个字'}, {'site_icon_text': '\u202e'}):
            with self.subTest(invalid=invalid):
                response = await self.client.put('/api/v1/settings/branding', json=invalid)
                self.assertEqual(response.status_code, 422)
        self.assertEqual(self.store.settings().site_name, 'any签到助手')

    async def test_proxy_creation_and_partial_import_return_trackable_results(self):
        response = await self.client.post('/api/v1/proxies', json={'host': '127.0.0.1', 'port': 9, 'scheme': 'http', 'name': 'fixture'})
        self.assertEqual(response.status_code, 200, response.text)
        created = response.json()
        self.assertEqual(len(created['job_ids']), 1)
        imported = await self.client.post('/api/v1/proxies/import', json={'lines': ['http://127.0.0.1:9', 'not-a-proxy']})
        self.assertEqual(imported.status_code, 200, imported.text)
        self.assertEqual((imported.json()['inserted'], len(imported.json()['errors']), len(imported.json()['job_ids'])), (1, 1, 1))
        jobs = (await self.client.get('/api/v1/jobs/status', params={'ids': ','.join(created['job_ids'] + imported.json()['job_ids'])})).json()['items']
        self.assertTrue(all(job['kind'] == 'proxy_test' and job['status'] == 'pending' for job in jobs))

    async def test_statistics_timezone_future_precision_and_reconciliation(self):
        identifier = account(self.store)
        moment = datetime(2026, 9, 13, 0, 5, tzinfo=ZoneInfo('Asia/Shanghai'))
        midnight = moment.replace(minute=0).timestamp()
        records = [('signed', 100.00004, 100.00006, midnight + offset, 'live') for offset in (1, 2, 3)]
        records += [('already_signed', 1, 90, midnight + 4, 'live'), ('uncertain', None, None, midnight + 5, 'live'),
                    ('error', None, None, midnight + 6, 'live'), ('signed', 1, 999, midnight + 7, 'legacy'),
                    ('signed', 1, 6, midnight - 1, 'live'), ('signed', 1, 1001, moment.timestamp() + 60, 'live')]
        for code, before, after, created, source in records:
            self.store.execute('INSERT INTO checkins(account_id,code,quota_before,quota_after,created,balance_source) VALUES (?,?,?,?,?,?)',
                               (identifier, code, before, after, created, source))
        with patch('app.statistics.datetime') as clock:
            clock.now.side_effect = lambda zone: moment.astimezone(zone)
            clock.fromtimestamp.side_effect = datetime.fromtimestamp
            for span, expected_attempts, expected_earned in [('day', 7, 0.0001), ('week', 8, 5.0001), ('month', 8, 5.0001)]:
                response = await self.client.get('/api/v1/stats', params={'range': span})
                self.assertEqual(response.status_code, 200, response.text)
                stats = response.json()
                self.assertEqual(stats['attempts'], expected_attempts)
                self.assertEqual(stats['earned'], expected_earned)
                self.assertEqual((stats['failed'], stats['uncertain'], stats['legacy_records']), (1, 1, 1))
                for field in ('signed', 'already', 'failed', 'uncertain', 'earned'):
                    self.assertAlmostEqual(stats[field], sum(point[field] for point in stats['series']), places=4)
                    self.assertAlmostEqual(stats[field], sum(row[field] for row in stats['accounts']), places=4)
                self.assertLessEqual(stats['accounts'][0]['last_balance']['created'], moment.timestamp())
            self.store.set_meta('settings', self.store.settings().model_copy(update={'timezone': 'UTC'}).model_dump_json())
            self.assertEqual((await self.client.get('/api/v1/stats')).json()['attempts'], 7)
        self.assertEqual(quota_of({'quota': 50000020}), 100.00004)
        for invalid in (float('nan'), float('inf'), True, '100', 10 ** 1000):
            self.assertIsNone(quota_of({'quota': invalid}))

    async def test_checkin_errors_and_interrupted_jobs_are_counted_once(self):
        identifier = account(self.store)
        self.service.checkin = AsyncMock(side_effect=RuntimeError('synthetic failure'))
        job = await self.perform(identifier)
        self.assertEqual(job['status'], 'error')
        self.engine.finish(job, 'error', 'duplicate completion')
        self.store.record_checkin(identifier, job['id'], 'signed', 1, 999)
        self.assertEqual(self.store.one('SELECT COUNT(*) AS count FROM checkins')['count'], 1)
        self.store.enqueue('checkin', identifier)
        pending = self.engine.claim()
        self.store.recover()
        self.store.recover()
        self.assertEqual(self.store.checkin_balance(job_id=pending['id'])['code'], 'uncertain')
        stats = (await self.client.get('/api/v1/stats')).json()
        self.assertEqual((stats['attempts'], stats['failed'], stats['uncertain'], stats['earned']), (2, 1, 1, 0))

    async def test_restore_old_backup_keeps_newer_credentials_balance_and_notes(self):
        identifier = account(self.store)
        now = time.time()
        self.store.execute("UPDATE accounts SET note='最新备注',quota=88,last_validated=?,last_extracted=?,updated=? WHERE id=?", (now, now, now, identifier))
        backup = await export_document(self.client)
        old = backup['accounts'][0]
        old.update(password='old-password', note='旧备注', quota=1, validity='invalid', result={'session': 'old-cookie', 'api_user': '42'})
        for field in ('created', 'updated', 'last_extracted', 'last_validated'):
            old[field] = now - 3600
        for missing_password in (False, True):
            if missing_password:
                old.pop('password')
            response = await self.client.post('/api/v1/backup/restore', json=backup)
            self.assertEqual(response.status_code, 200, response.text)
            saved = self.store.account(identifier)
            self.assertEqual(saved['login']['password'], 'test-account-password')
            self.assertEqual((saved['quota'], saved['note'], saved['validity'], saved['result']['session']), (88, '最新备注', 'valid', 'valid'))
        await self.client.put('/api/v1/accounts/' + identifier, json={'password': 'new-password-after-backup'})
        self.store.execute('UPDATE accounts SET result_enc=NULL,last_extracted=NULL,updated=? WHERE id=?', (time.time(), identifier))
        self.assertIsNone(self.store.account(identifier)['result'])
        self.assertEqual((await self.client.post('/api/v1/backup/restore', json=backup)).status_code, 200)
        self.assertIsNone(self.store.account(identifier)['result'])

    async def test_restore_rejects_bad_documents_without_partial_changes(self):
        identifier = account(self.store)
        original = await export_document(self.client)
        variants = []
        for bad_balance in ('not-a-number', float('inf'), float('nan')):
            document = json.loads(json.dumps(original))
            document['accounts'][0]['quota'] = bad_balance
            variants.append(document)
        variants += [original | {'accounts': [42]}, original | {'schema_version': SCHEMA_VERSION + 1},
                     original | {'settings': {'site_name': ''}},
                     original | {'checkins': [{'username': 'synthetic', 'code': 'signed', 'created': 1e300}]}]
        for document in variants:
            response = await self.client.post('/api/v1/backup/restore', content=json.dumps(document), headers={'Content-Type': 'application/json'})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(self.store.account(identifier)['quota'], 999)
            self.assertEqual(self.store.one('SELECT COUNT(*) AS count FROM accounts')['count'], 1)

    async def test_already_signed_unknown_and_negative_balances(self):
        for index, (code, before, after, expected) in enumerate([('already_signed', 10, 80, None), ('signed', 10, None, None), ('signed', 20, 18, 0)]):
            with self.subTest(code=code, after=after):
                identifier = account(self.store, str(index))
                self.service.result.update(code=code, quota_before=before, quota_after=after, quota=after if after is not None else before)
                await self.perform(identifier)
                self.assertEqual(self.store.checkin_balance(account_id=identifier)['earned'], expected)
        stats = (await self.client.get('/api/v1/stats')).json()
        self.assertEqual(stats['earned'], 0)
        self.assertEqual(stats['unmeasured'], 1)

    async def test_expired_cookie_recovers_in_original_job_without_auto_checkin_setting(self):
        identifier = account(self.store, validity='invalid')
        job = await self.perform(identifier)
        self.assertEqual(job['status'], 'signed')
        self.assertEqual((self.service.extracts, self.service.checkins), (1, 1))
        self.assertEqual(self.store.one('SELECT COUNT(*) AS count FROM jobs')['count'], 1)
        self.assertEqual(self.store.one('SELECT COUNT(*) AS count FROM checkins')['count'], 1)
        self.assertGreaterEqual(self.store.one("SELECT COUNT(*) AS count FROM logs WHERE category='invalid'")['count'], 3)
        self.assertEqual(self.store.account(identifier)['result']['session'], 'fresh')

    async def test_invalid_detected_during_precheck_recovers_once(self):
        identifier = account(self.store)
        self.store.execute('UPDATE accounts SET result_enc=? WHERE id=?', (self.store.vault.seal({'session': 'expired', 'api_user': '42'}, f'result:{identifier}'), identifier))
        job = await self.perform(identifier)
        self.assertEqual((job['status'], self.service.extracts, self.service.checkins), ('signed', 1, 2))

    async def test_no_relogin_loops_and_network_failure_does_not_invalidate_cookie(self):
        identifier = account(self.store)
        self.service.always_invalid = True
        job = await self.perform(identifier)
        self.assertEqual((job['status'], self.service.extracts, self.service.checkins), ('invalid', 1, 2))
        self.assertEqual(self.store.one("SELECT COUNT(*) AS count FROM jobs WHERE status='pending'")['count'], 0)
        other = account(self.store, 'network')
        self.service.always_invalid = False
        self.service.network_error = True
        self.assertEqual((await self.perform(other))['status'], 'network_error')
        self.assertEqual(self.service.extracts, 1)
        self.assertNotEqual(self.store.account(other)['validity'], 'invalid')
        self.assertTrue((await self.client.get('/api/v1/logs?category=error')).json()['items'])

    async def test_missing_password_or_disabled_recovery_is_explicit(self):
        identifier = account(self.store, validity='invalid', password=None)
        self.assertEqual((await self.perform(identifier))['status'], 'invalid')
        self.assertEqual(self.service.extracts, 0)
        other = account(self.store, 'disabled', validity='invalid')
        self.store.set_meta('settings', self.store.settings().model_copy(update={'auto_reextract': False}).model_dump_json())
        self.assertIn('未启用', (await self.perform(other))['message'])

    async def test_pagination_sort_notes_and_full_stats(self):
        identifiers = [account(self.store, f'fixture-{index}') for index in range(205)]
        for size in (5, 10, 15, 20, 30, 50):
            response = (await self.client.get(f'/api/v1/accounts?page=2&limit={size}')).json()
            self.assertEqual(len(response['items']), size)
            self.assertEqual(response['total'], 205)
        response = await self.client.post('/api/v1/accounts/reorder', json={'ids': list(reversed(identifiers[:5]))})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item['id'] for item in (await self.client.get('/api/v1/accounts?limit=5')).json()['items']], list(reversed(identifiers[:5])))
        self.assertEqual((await self.client.put('/api/v1/accounts/' + identifiers[0], json={'note': '保留备注', 'password': 'new-test-password'})).status_code, 200)
        self.assertEqual(self.store.stored_password(identifiers[0]), 'new-test-password')
        self.assertEqual(len((await self.client.get('/api/v1/stats?range=month')).json()['accounts']), 205)

    async def test_import_returns_ids_even_after_completion(self):
        payload = {'import_id': 'same-import', 'accounts': [{'username': 'import-fixture', 'password': 'test-pass', 'row_id': '1'}]}
        response = (await self.client.post('/api/v1/accounts/import', json=payload)).json()
        self.assertEqual(len(response['job_ids']), 1)
        await self.engine.process(self.engine.claim())
        repeated = (await self.client.post('/api/v1/accounts/import', json=payload)).json()
        self.assertEqual(repeated['job_ids'], response['job_ids'])
        self.assertEqual(repeated['queued'], 0)

    async def test_editing_login_invalidates_old_credentials_and_rejects_running_jobs(self):
        identifier = account(self.store)
        pending, _ = self.store.enqueue('validate', identifier)
        response = await self.client.put('/api/v1/accounts/' + identifier, json={'username': 'changed'})
        self.assertEqual(response.status_code, 409)
        self.engine.cancel(pending)
        response = await self.client.put('/api/v1/accounts/' + identifier, json={'username': 'changed'})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.store.account(identifier)['result'])
        self.assertEqual(self.store.account(identifier)['validity'], 'unknown')

    async def test_backup_restore_keeps_measured_balances_and_is_idempotent(self):
        identifier = account(self.store)
        await self.perform(identifier)
        backup = await export_document(self.client)
        self.assertEqual(backup['checkins'][0]['balance_source'], 'live')
        self.store.set_meta('queue_paused', '1')
        for attempt in range(2):
            response = await self.client.post('/api/v1/backup/restore', json=backup)
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.one('SELECT COUNT(*) AS count FROM checkins')['count'], 1)
        self.assertEqual(self.store.checkin_balance(account_id=identifier)['earned'], 2.5)

    async def test_legacy_migration_does_not_claim_cached_amounts_as_live(self):
        identifier = account(self.store)
        self.store.record_checkin(identifier, None, 'signed', 1, 999)
        self.store.execute('ALTER TABLE checkins DROP COLUMN balance_source')
        self.store.execute('ALTER TABLE jobs DROP COLUMN hidden')
        self.store.execute('PRAGMA user_version=3')
        migrated = Store(self.config.data_dir, Vault(self.config.key))
        self.assertEqual(migrated.one('PRAGMA user_version')['user_version'], SCHEMA_VERSION)
        self.assertEqual(migrated.checkin_balance(account_id=identifier)['balance_source'], 'legacy')
        stats = (await self.client.get('/api/v1/stats')).json()
        self.assertEqual((stats['legacy_records'], stats['earned']), (1, 0))
        self.assertEqual(migrated.stored_password(identifier), 'test-account-password')

    async def test_backup_keeps_account_identity_when_order_changes_during_export(self):
        first = account(self.store, 'first')
        second = account(self.store, 'second')
        self.store.record_checkin(first, None, 'signed', 1, 2)
        self.store.record_checkin(second, None, 'signed', 10, 20)
        original_open = self.store.vault.open
        changed = False
        def open_and_reorder(value, context):
            nonlocal changed
            if not changed and context.startswith('login:'):
                changed = True
                self.store.execute('UPDATE accounts SET position=CASE WHEN id=? THEN 2 ELSE 1 END', (first,))
            return original_open(value, context)
        with patch.object(self.store.vault, 'open', side_effect=open_and_reorder):
            document = await export_document(self.client)
        self.assertEqual({item['username']: item['quota_after'] for item in document['checkins']}, {'first': 2, 'second': 20})

    async def test_maintenance_blocks_mutations_and_all_workers(self):
        identifier = account(self.store)
        self.store.enqueue('checkin', identifier)
        self.store.set_meta('maintenance', '1')
        self.assertIsNone(self.engine.claim())
        self.assertEqual((await self.client.post('/api/v1/queue/resume')).status_code, 503)
        self.assertEqual((await self.client.get('/api/v1/updates')).status_code, 200)
        self.assertEqual((await self.client.get('/healthz')).status_code, 200)

    async def test_update_source_version_check_and_duplicate_install(self):
        self.assertFalse((await self.client.get('/api/v1/updates')).json()['updater_available'])
        self.assertEqual((await self.client.put('/api/v1/updates/source', json={'repository': 'https://evil.invalid/project'})).status_code, 400)
        self.assertEqual((await self.client.put('/api/v1/updates/source', json={'repository': 'owner/assistant'})).status_code, 200)
        major, minor, patch_version = common.version_tuple((ROOT / 'VERSION').read_text().strip())
        next_version = f'{major}.{minor}.{patch_version + 1}'
        release = {'version': next_version, 'repository': 'owner/assistant', 'release_url': f'https://github.com/owner/assistant/releases/tag/v{next_version}'}
        with patch('app.update_api.latest_release', return_value=release) as fetch:
            for attempt in range(2):
                self.assertEqual((await self.client.post('/api/v1/updates/check')).status_code, 200)
            self.assertEqual(fetch.call_count, 1)
        with patch('app.update_api.VERSION', next_version):
            self.assertFalse((await self.client.get('/api/v1/updates')).json()['check']['update_available'])
        payload = {'version': next_version, 'force': False, 'acknowledged': True}
        self.assertEqual((await self.client.post('/api/v1/updates/install', json=payload)).status_code, 503)
        common.atomic_json(self.root / 'updates/status/heartbeat.json', {'protocol': 1, 'at': time.time()})
        response = await self.client.post('/api/v1/updates/install', json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual((await self.client.post('/api/v1/updates/install', json=payload)).status_code, 409)
        self.assertEqual((await self.client.get('/api/v1/updates')).json()['operation']['id'], response.json()['id'])


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='v3-release-', dir=LOCAL)
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.assertTrue(self.root.resolve().is_relative_to(LOCAL.resolve()))
        self.temporary.cleanup()

    def test_sources_versions_and_official_download_hosts(self):
        self.assertEqual(common.repository_name('https://github.com/owner/project.git/'), 'owner/project')
        self.assertGreater(common.version_tuple('v0.10.0'), common.version_tuple('0.9.99'))
        for value in ('http://127.0.0.1/file', 'https://github.com.evil.invalid/file', 'https://user:secret@github.com/file', 'https://github.com:444/file'):
            with self.subTest(value=value), self.assertRaises(common.UpdateError):
                common.safe_release_url(value)
        for value in ('v0.2', '0.3.0;touch /tmp/x', '01.2.3', '0.3.1-beta'):
            with self.subTest(value=value), self.assertRaises(common.UpdateError):
                common.version_tuple(value)

    def test_installer_generates_valid_systemd_paths(self):
        installer = (ROOT / 'scripts/install-updater.sh').read_text(encoding='utf-8')
        template = installer.split('cat > /etc/systemd/system/any-signin-assistant-updater.service <<EOF\n', 1)[1].split('\nEOF\n', 1)[0]
        destination = '/usr/local/lib/any-signin-assistant-updater'
        for root in ('/opt/any-signin-assistant', '/opt/custom workspace'):
            with self.subTest(root=root):
                unit = configparser.ConfigParser(interpolation=None)
                unit.read_string(template.replace('$ROOT', root).replace('$DEST', destination))
                service = unit['Service']
                self.assertTrue(PurePosixPath(service['WorkingDirectory']).is_absolute())
                self.assertEqual(service['WorkingDirectory'], root)
                self.assertEqual(shlex.split(service['ExecStart']), ['/usr/bin/python3', destination + '/update_runner.py', '--root', root])
                self.assertIn(root, shlex.split(service['ReadWritePaths']))
        self.assertIn('systemd-analyze verify /etc/systemd/system/any-signin-assistant-updater.service', installer)

    @unittest.skipIf(os.name == 'nt', 'POSIX permissions verified on deployment host')
    def test_status_files_readable_under_service_private_umask(self):
        previous = os.umask(0o077)
        try:
            target = self.root / 'heartbeat.json'
            common.atomic_json(target, {'protocol': 1}, 0o644)
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)
        finally:
            os.umask(previous)

    def test_remote_manifest_binds_project_version_checksum_and_asset(self):
        base = 'https://github.com/owner/project/releases/download/v0.3.1/'
        name = 'any-signin-assistant-v0.3.1.tar.gz'
        release = {'tag_name': 'v0.3.1', 'assets': [{'name': filename, 'browser_download_url': base + filename} for filename in ('release.json', name)]}
        manifest = {'app': common.APP_ID, 'version': '0.3.1', 'archive': name, 'sha256': 'a' * 64, 'size': 100}
        with patch.object(common, 'fetch_bytes', side_effect=[json.dumps(release).encode(), json.dumps(manifest).encode()]):
            self.assertEqual(common.latest_release('owner/project')['version'], '0.3.1')
        manifest['app'] = 'different-project'
        with patch.object(common, 'fetch_bytes', side_effect=[json.dumps(release).encode(), json.dumps(manifest).encode()]), self.assertRaises(common.UpdateError):
            common.latest_release('owner/project')

    def test_archive_rejects_traversal_symlinks_and_runtime_data(self):
        for index, name in enumerate(('../escape', '/etc/shadow', 'app/../../escape', 'app\\escape', 'data/assistant.sqlite3', 'app/.env')):
            with self.subTest(name=name):
                archive = self.root / f'bad-{index}.tar.gz'
                with tarfile.open(archive, 'w:gz') as package:
                    member = tarfile.TarInfo(name)
                    member.size = 1
                    package.addfile(member, io.BytesIO(b'x'))
                with self.assertRaises(common.UpdateError):
                    common.extract_source(archive, self.root / f'unpack-{index}', '0.3.1')
        archive = self.root / 'symlink.tar.gz'
        with tarfile.open(archive, 'w:gz') as package:
            member = tarfile.TarInfo('app/link')
            member.type, member.linkname = tarfile.SYMTYPE, '/etc'
            package.addfile(member)
        with self.assertRaises(common.UpdateError):
            common.extract_source(archive, self.root / 'unpack-link', '0.3.1')

    def test_first_install_custom_and_random_password(self):
        for index, password in enumerate(('a-custom-password!#', '')):
            root = self.root / str(index)
            args = argparse.Namespace(root=root, public_url='http://192.0.2.15:23000', allow_http=True, port=23000, username='my-admin',
                                      owner=None, password_stdin=True, bind_host='0.0.0.0', https_proxy=False)
            with patch('sys.stdin', io.StringIO(password)), contextlib.redirect_stdout(io.StringIO()):
                initialize(args)
            contents = (root / '.env').read_text()
            self.assertIn('APP_ADMIN_USERNAME=my-admin', contents)
            self.assertIn('APP_BIND_HOST=0.0.0.0', contents)
            self.assertIn('APP_ALLOW_INSECURE_HTTP=1', contents)
            stored = next(line.split('：', 1)[1] for line in (root / '部署信息.txt').read_text(encoding='utf-8').splitlines() if line.startswith('管理员密码'))
            self.assertEqual(stored, password) if password else self.assertGreaterEqual(len(stored), 24)
            self.assertTrue(PasswordHasher().verify((root / 'secrets/admin.hash').read_text().strip(), stored))

    def test_public_http_requires_explicit_opt_in(self):
        environment = {'APP_SECRET_KEY': base64.urlsafe_b64encode(bytes(range(32))).decode(), 'APP_ADMIN_PASSWORD_HASH': PasswordHasher().hash('test-password'),
                       'APP_PUBLIC_URL': 'http://192.0.2.15:23000', 'APP_DATA_DIR': str(self.root), 'APP_ADMIN_USERNAME': 'my-admin'}
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(RuntimeError):
                Config.from_env()
            os.environ['APP_ALLOW_INSECURE_HTTP'] = '1'
            self.assertEqual(Config.from_env().admin_username, 'my-admin')
            self.assertFalse(Config.from_env().cookie_secure)


class SimulatedUpdater(Updater):
    def __init__(self, root, fail_health=False):
        super().__init__(root)
        self.commands = []
        self.fail_health = fail_health
        self.stops = 0

    def execute(self, command, timeout=300):
        self.commands.append(command)

    def stop_app(self):
        self.stops += 1

    def healthy(self, version, timeout=120):
        if self.fail_health and version == '0.3.1':
            raise common.UpdateError('模拟新版本健康检查失败')


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='v3-updater-', dir=LOCAL)
        self.workspace = Path(self.temporary.name)
        self.root = self.workspace / 'project'
        self.next_source = self.workspace / 'next'
        for directory, version in ((self.root, '0.3.0'), (self.next_source, '0.3.1')):
            directory.mkdir()
            for name, value in {'.project-id': common.PROJECT_ID, 'VERSION': version, 'Dockerfile': 'FROM scratch\n',
                                'compose.yml': 'name: any-signin-assistant\n', 'requirements.txt': '', 'app/main.py': f'VERSION = "{version}"\n',
                                'frontend/package.json': '{}', 'scripts/install-updater.sh': 'exit 0\n'}.items():
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value, encoding='utf-8')
        for name in ('updates/status', 'updates/requests', 'updates/work', 'backups', 'secrets'):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        (self.root / '.env').write_text('APP_IMAGE=any-signin-assistant:0.3.0\nAPP_PORT=18780\nAPP_ADMIN_USERNAME=owner\n')
        for name in ('app.key', 'admin.hash'):
            (self.root / 'secrets' / name).write_text('synthetic-secret-' + name)
        self.store = Store(self.root / 'data', Vault(bytes(range(32))))
        self.identifier = account(self.store)
        self.store.record_checkin(self.identifier, None, 'signed', 10, 12.5)
        self.baseline = common.source_manifest(self.root)
        common.atomic_json(self.root / '.release-files.json', self.baseline)
        self.archive = self.workspace / 'next.tar.gz'
        with tarfile.open(self.archive, 'w:gz') as package:
            for path, name in common.source_files(self.next_source):
                package.add(path, arcname=name, recursive=False)
        self.content = self.archive.read_bytes()
        self.release = {'version': '0.3.1', 'archive_url': 'https://github.com/owner/project/releases/download/v0.3.1/any-signin-assistant-v0.3.1.tar.gz',
                        'size': len(self.content), 'sha256': hashlib.sha256(self.content).hexdigest()}

    def tearDown(self):
        self.assertTrue(self.workspace.resolve().is_relative_to(LOCAL.resolve()))
        self.temporary.cleanup()

    def install(self, updater, force=False, content=None):
        request = {'id': uuid.uuid4().hex, 'version': '0.3.1', 'repository': 'owner/project', 'force': force, 'requested_at': time.time()}
        with patch.object(common, 'latest_release', return_value=self.release), patch.object(common, 'fetch_bytes', return_value=self.content if content is None else content), patch('scripts.update_runner.os.chown', create=True):
            return updater.install(request)

    def test_success_preserves_data_credentials_configuration_and_previous_pause(self):
        self.store.set_meta('queue_paused', '1')
        self.store.set_meta('pause_reason', '原先由管理员暂停')
        updater = SimulatedUpdater(self.root)
        self.assertTrue(self.install(updater))
        self.assertEqual(updater.state['stage'], 'complete')
        self.assertEqual((self.root / 'VERSION').read_text(), '0.3.1')
        self.assertIn('APP_ADMIN_USERNAME=owner', (self.root / '.env').read_text())
        self.assertEqual((self.root / 'secrets/app.key').read_text(), 'synthetic-secret-app.key')
        self.assertEqual(self.store.stored_password(self.identifier), 'test-account-password')
        self.assertEqual(self.store.meta('queue_paused'), '1')
        self.assertEqual(self.store.meta('pause_reason'), '原先由管理员暂停')
        self.assertEqual(self.store.meta('maintenance'), '0')
        self.assertTrue((Path(updater.state['backup_path']) / 'assistant.sqlite3').exists())
        self.assertEqual(common.local_changes(self.root), [])
        self.assertTrue(any(command[:2] == ['docker', 'build'] for command in updater.commands))
        self.assertFalse(any('caddy' in command or 'prune' in command or 'down' in command for command in updater.commands))

    def test_failed_health_restores_source_database_and_queue(self):
        updater = SimulatedUpdater(self.root, fail_health=True)
        self.assertFalse(self.install(updater))
        self.assertEqual(updater.state['stage'], 'rolled_back')
        self.assertEqual((self.root / 'VERSION').read_text(), '0.3.0')
        self.assertEqual(self.store.meta('maintenance'), '0')
        self.assertEqual(self.store.meta('queue_paused'), '0')
        self.assertEqual(self.store.checkin_balance(account_id=self.identifier)['earned'], 2.5)
        self.assertEqual(common.source_manifest(self.root), self.baseline)

    def test_checksum_failure_never_executes_or_stops_service(self):
        updater = SimulatedUpdater(self.root)
        self.assertFalse(self.install(updater, content=b'corrupt'))
        self.assertEqual(updater.commands, [])
        self.assertEqual(updater.stops, 0)
        self.assertIn('SHA-256', updater.state['message'])
        self.assertEqual((self.root / 'VERSION').read_text(), '0.3.0')

    def test_local_release_uses_same_backup_health_and_integrity_checks(self):
        manifest = self.workspace / 'release.json'
        common.atomic_json(manifest, {'app': common.APP_ID, 'archive': 'any-signin-assistant-v0.3.1.tar.gz', **self.release})
        request = {'id': uuid.uuid4().hex, 'version': '0.3.1', 'repository': '', 'force': False, 'requested_at': time.time()}
        updater = SimulatedUpdater(self.root)
        with patch.object(common, 'latest_release', side_effect=AssertionError('local install must not use GitHub')), patch('scripts.update_runner.os.chown', create=True):
            self.assertTrue(updater.install(request, local_archive=self.archive, manifest_path=manifest))
        self.assertEqual(updater.state['stage'], 'complete')
        self.assertEqual(self.store.checkin_balance(account_id=self.identifier)['earned'], 2.5)
        self.assertTrue((Path(updater.state['backup_path']) / 'assistant.sqlite3').is_file())

    def test_local_release_checksum_failure_never_stops_current_app(self):
        manifest = self.workspace / 'release.json'
        common.atomic_json(manifest, {'app': common.APP_ID, 'archive': 'any-signin-assistant-v0.3.1.tar.gz', **self.release, 'sha256': '0' * 64})
        request = {'id': uuid.uuid4().hex, 'version': '0.3.1', 'repository': '', 'force': False, 'requested_at': time.time()}
        updater = SimulatedUpdater(self.root)
        self.assertFalse(updater.install(request, local_archive=self.archive, manifest_path=manifest))
        self.assertEqual((updater.commands, updater.stops), ([], 0))
        self.assertIn('SHA-256', updater.state['message'])

    def test_local_changes_require_force_and_are_backed_up(self):
        source = self.root / 'app/main.py'
        source.write_text('local_change = True\n')
        original_bytes = source.read_bytes()
        updater = SimulatedUpdater(self.root)
        self.assertFalse(self.install(updater))
        self.assertEqual(updater.commands, [])
        self.assertIn('本地源码改动', updater.state['message'])
        forced = SimulatedUpdater(self.root)
        self.assertTrue(self.install(forced, force=True))
        with tarfile.open(Path(forced.state['backup_path']) / 'source.tar.gz') as backup:
            self.assertEqual(backup.extractfile('app/main.py').read(), original_bytes)

    def test_interrupted_upgrade_rolls_back_on_restart(self):
        updater = SimulatedUpdater(self.root)
        self.assertTrue(self.install(updater))
        updater.context['committed'] = False
        updater.save_context()
        updater.transition('restarting', '模拟主机在切换时断电', 80)
        resumed = SimulatedUpdater(self.root)
        with patch('scripts.update_runner.os.chown', create=True):
            resumed.recover()
        self.assertEqual(resumed.state['stage'], 'rolled_back')
        self.assertEqual((self.root / 'VERSION').read_text(), '0.3.0')
        self.assertEqual(self.store.meta('maintenance'), '0')


if __name__ == '__main__':
    unittest.main(verbosity=2)
