import asyncio
import json
import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.crypto import Vault
from app.db import SCHEMA_VERSION, Store
from app.errors import TaskError
from app.responses import parse_response
from tools import verify_v3 as fixtures


class QueueTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ApiTests.asyncSetUp

    async def asyncTearDown(self):
        await self.engine.stop()
        await fixtures.ApiTests.asyncTearDown(self)

    async def until(self, predicate):
        async with asyncio.timeout(8):
            while not predicate():
                await asyncio.sleep(0.01)

    async def limits(self, queue, checkin):
        response = await self.client.put('/api/v1/queue/settings', json={'max_concurrency': queue, 'checkin_concurrency': checkin})
        self.assertEqual(response.status_code, 200, response.text)

    def enqueue(self, kind='validate', **options):
        identifier = fixtures.account(self.store, uuid.uuid4().hex, **options)
        job_id, _ = self.store.enqueue(kind, identifier)
        return job_id, identifier

    async def run_next(self):
        job = self.engine.claim()
        self.assertIsNotNone(job)
        await self.engine.process(job)
        return self.store.one('SELECT * FROM jobs WHERE id=?', (job['id'],))

    async def test_duplicate_import_preserves_password_and_never_requeues(self):
        original = {'import_id': 'first', 'accounts': [{'username': 'existing', 'password': 'original-secret', 'row_id': '1'}]}
        first = (await self.client.post('/api/v1/accounts/import', json=original)).json()
        await self.run_next()
        duplicate = {'import_id': 'second', 'accounts': [{'username': ' existing ', 'password': 'replacement-secret', 'row_id': '1'}]}
        result = (await self.client.post('/api/v1/accounts/import', json=duplicate)).json()
        self.assertEqual((result['queued'], result['duplicate_count'], result['job_ids']), (0, 1, []))
        self.assertEqual(self.store.stored_password(first['account_ids'][0]), 'original-secret')
        replay = (await self.client.post('/api/v1/accounts/import', json=original)).json()
        self.assertEqual((replay['queued'], replay['job_ids']), (0, first['job_ids']))

    async def test_duplicates_within_batch_are_skipped(self):
        payload = {'import_id': 'batch', 'accounts': [{'username': 'same', 'password': 'secret', 'row_id': str(index)} for index in range(3)]}
        result = (await self.client.post('/api/v1/accounts/import', json=payload)).json()
        self.assertEqual((result['queued'], result['duplicate_count']), (1, 2))

    async def test_two_lanes_and_same_account_exclusion(self):
        first, identifier = self.enqueue()
        self.store.enqueue('checkin', identifier)
        second, _ = self.enqueue()
        signin, _ = self.enqueue('checkin')
        self.assertEqual(self.engine.claim()['id'], first)
        self.assertEqual(self.engine.claim()['id'], signin)
        self.assertIsNone(self.engine.claim())
        await self.limits(2, 3)
        self.assertEqual(self.engine.claim()['id'], second)
        self.assertIsNone(self.engine.claim())

    async def test_live_raise_and_lower_does_not_interrupt_existing_jobs(self):
        slots = []

        async def validate(*args):
            gate = asyncio.Event()
            slots.append(gate)
            await gate.wait()
            return {'quota': 10}

        self.service.validate = AsyncMock(side_effect=validate)
        for index in range(8):
            self.enqueue()
        await self.engine.start()
        await self.until(lambda: len(slots) == 1)
        await self.limits(3, 1)
        await self.until(lambda: len(slots) == 3)
        await self.limits(1, 1)
        slots[0].set()
        await self.until(lambda: len(self.engine.running) == 2)
        self.assertEqual(len(slots), 3)
        slots[1].set()
        await self.until(lambda: len(self.engine.running) == 1)
        self.assertEqual(len(slots), 3)
        slots[2].set()
        await self.until(lambda: len(slots) == 4)
        self.assertEqual(len(self.engine.running), 1)

    async def test_fourteen_due_schedules_share_serial_checkin_lane(self):
        now = time.time()
        for index in range(14):
            identifier = fixtures.account(self.store, f'schedule-{index}')
            schedule_id = uuid.uuid4().hex
            self.store.execute('INSERT INTO schedules(id,name,interval_minutes,next_run,created) VALUES (?,?,?,?,?)', (schedule_id, str(index), 1440, now - 1, now))
            self.store.execute('INSERT INTO schedule_accounts VALUES (?,?)', (schedule_id, identifier))
        self.engine.tick_schedules(now)
        self.engine.tick_schedules(now)
        self.assertEqual(self.store.one('SELECT COUNT(*) AS count FROM jobs')['count'], 14)
        for index in range(14):
            job = self.engine.claim()
            self.assertIsNotNone(job)
            self.assertIsNone(self.engine.claim())
            await self.engine.process(job)
        self.assertEqual(self.service.checkins, 14)

    async def test_checkin_limit_changes_while_executing(self):
        gate = asyncio.Event()

        async def checkin(*args, **kwargs):
            await gate.wait()
            return self.service.result.copy()

        self.service.checkin = AsyncMock(side_effect=checkin)
        for index in range(6):
            self.enqueue('checkin')
        await self.engine.start()
        await self.until(lambda: self.service.checkin.await_count == 1)
        await self.limits(1, 2)
        await self.until(lambda: self.service.checkin.await_count == 2)
        self.assertEqual(len(self.engine.running), 2)
        self.assertEqual(self.store.one("SELECT COUNT(*) AS count FROM jobs WHERE status='pending'")['count'], 4)

    async def test_pending_delete_preserves_account_logs_and_tracking(self):
        job_id, identifier = self.enqueue('extract')
        response = await self.client.delete(f'/api/v1/jobs/{job_id}')
        self.assertEqual((response.status_code, response.json()['status']), (200, 'cancelled'))
        self.assertEqual((await self.client.get('/api/v1/jobs')).json()['total'], 0)
        status = (await self.client.get('/api/v1/jobs/status', params={'ids': job_id})).json()['items'][0]
        self.assertEqual(status['status'], 'cancelled')
        self.assertIsNotNone(self.store.account(identifier))
        self.assertTrue(self.store.one('SELECT id FROM logs WHERE job_id=?', (job_id,)))

    async def running_delete(self, kind, refresh=False):
        gate = asyncio.Event()

        async def hold(*args, **kwargs):
            await gate.wait()

        operation = AsyncMock(side_effect=hold)
        if refresh or kind == 'extract':
            self.service.extract = operation
        else:
            self.service.checkin = operation
        job_id, identifier = self.enqueue(kind, validity='invalid' if refresh else 'valid')
        await self.engine.start()
        await self.until(lambda: operation.await_count == 1)
        response = await self.client.delete(f'/api/v1/jobs/{job_id}')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(job_id, self.engine.running)
        self.assertEqual(response.json()['status'], 'uncertain' if kind == 'checkin' and not refresh else 'cancelled')
        self.assertIsNotNone(self.store.account(identifier))

    async def test_delete_running_extraction_waits_for_cleanup(self):
        await self.running_delete('extract')

    async def test_delete_running_checkin_retains_uncertain_statistic(self):
        await self.running_delete('checkin')
        self.assertEqual(self.store.one('SELECT code FROM checkins')['code'], 'uncertain')

    async def test_cancel_during_refresh_has_not_submitted_checkin(self):
        await self.running_delete('checkin', refresh=True)

    async def test_successful_job_delete_preserves_statistics(self):
        job_id, identifier = self.enqueue('checkin')
        await self.run_next()
        self.assertEqual((await self.client.delete(f'/api/v1/jobs/{job_id}')).status_code, 200)
        self.assertEqual(self.store.checkin_balance(account_id=identifier)['earned'], 2.5)

    async def test_expiry_refreshes_and_continues_same_job(self):
        job_id, identifier = self.enqueue('checkin')
        self.store.execute('UPDATE accounts SET result_enc=? WHERE id=?', (self.store.vault.seal({'session': 'expired', 'api_user': '42'}, f'result:{identifier}'), identifier))
        result = await self.run_next()
        self.assertEqual((result['status'], self.service.extracts, self.service.checkins), ('signed', 1, 2))
        self.assertEqual(self.store.one('SELECT COUNT(*) AS count FROM jobs')['count'], 1)
        logs = self.store.all('SELECT kind,message FROM logs WHERE job_id=?', (job_id,))
        self.assertTrue(any(row['kind'] == 'extract' and '直连' in row['message'] for row in logs))
        self.assertTrue(any('继续执行原签到任务' in row['message'] for row in logs))
        self.assertNotIn('test-account-password', json.dumps(logs))

    async def test_missing_cookie_refreshes_before_signing(self):
        _, identifier = self.enqueue('checkin', validity='invalid')
        self.store.execute('UPDATE accounts SET result_enc=NULL WHERE id=?', (identifier,))
        result = await self.run_next()
        self.assertEqual((result['status'], self.service.extracts, self.service.checkins), ('signed', 1, 1))

    async def test_persistent_expiry_stops_after_one_refresh(self):
        self.service.always_invalid = True
        self.enqueue('checkin')
        result = await self.run_next()
        self.assertEqual((result['status'], self.service.extracts, self.service.checkins), ('invalid', 1, 2))
        self.assertIsNone(self.engine.claim())

    async def test_disabled_refresh_and_missing_password_fail_safely(self):
        self.enqueue('checkin', validity='invalid', password='')
        self.assertEqual((await self.run_next())['status'], 'invalid')
        self.store.set_meta('settings', self.store.settings().model_copy(update={'auto_reextract': False}).model_dump_json())
        self.enqueue('checkin', validity='invalid')
        self.assertEqual((await self.run_next())['status'], 'invalid')
        self.assertEqual(self.service.extracts, 0)

    async def test_uncertain_and_manual_errors_never_retry_login(self):
        for code in ('uncertain', 'needs_manual', 'network_error'):
            self.service.checkin = AsyncMock(side_effect=TaskError(code, 'synthetic failure'))
            self.enqueue('checkin')
            self.assertEqual((await self.run_next())['status'], code)
        self.assertEqual(self.service.extracts, 0)

    async def test_proxy_logs_identify_routes_without_secrets(self):
        self.store.set_meta('settings', self.store.settings().model_copy(update={'proxy_mode': 'pool'}).model_dump_json())
        for index in range(2):
            identifier = uuid.uuid4().hex
            config = {'scheme': 'http', 'host': f'127.0.0.{index + 2}', 'port': 8080, 'username': 'proxy-user', 'password': 'proxy-secret'}
            self.store.execute('INSERT INTO proxies(id,name,config_enc,created) VALUES (?,?,?,?)', (identifier, f'node-{index}', self.store.vault.seal(config, f'proxy:{identifier}'), time.time()))

        class Bridge:
            def __init__(self, config, *args):
                self.config = config

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

        async def extract(username, password, route, settings):
            if route.config['host'] == '127.0.0.2':
                raise TaskError('proxy_error', 'synthetic network failure', retry_proxy=True)
            return {'session': 'private-cookie-value', 'api_user': '42', 'quota': 10}

        self.service.extract = AsyncMock(side_effect=extract)
        job_id, _ = self.enqueue('extract')
        with patch('app.engine.ProxyBridge', Bridge):
            self.assertEqual((await self.run_next())['status'], 'success')
        messages = '\n'.join(row['message'] for row in self.store.all('SELECT message FROM logs WHERE job_id=?', (job_id,)))
        self.assertIn('代理：node-0 · http://127.0.0.2:8080', messages)
        self.assertIn('代理：node-1 · http://127.0.0.3:8080', messages)
        self.assertNotIn('proxy-secret', messages)
        self.assertNotIn('private-cookie-value', messages)

    async def test_settings_do_not_reset_concurrency_or_schedule(self):
        fixtures.account(self.store)
        with self.store.transaction() as connection:
            schedule_id = self.store.auto_schedule(connection, 120)
        self.store.execute('UPDATE schedules SET enabled=0 WHERE id=?', (schedule_id,))
        original = self.store.one('SELECT * FROM schedules WHERE id=?', (schedule_id,))
        stale = self.store.settings().model_dump()
        await self.limits(3, 2)
        response = await self.client.put('/api/v1/settings', json=stale | {'auto_checkin': True, 'auto_checkin_interval_minutes': 60})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((self.store.settings().max_concurrency, self.store.settings().checkin_concurrency), (3, 2))
        self.assertEqual(self.store.one('SELECT * FROM schedules WHERE id=?', (schedule_id,)), original)

    async def test_partial_settings_never_enable_default_schedule(self):
        await self.client.put('/api/v1/settings', json={'account_gap': 4})
        self.assertFalse(self.store.settings().auto_checkin)
        self.assertEqual(self.store.one('SELECT COUNT(*) AS count FROM schedules')['count'], 0)

    async def test_filters_pause_and_limit_validation(self):
        self.enqueue('validate')
        self.enqueue('checkin')
        self.assertEqual((await self.client.get('/api/v1/jobs?lane=checkin&state=active')).json()['total'], 1)
        self.assertEqual((await self.client.get('/api/v1/jobs?lane=unknown')).status_code, 400)
        await self.client.post('/api/v1/queue/pause')
        await self.limits(5, 5)
        self.assertIsNone(self.engine.claim())
        self.assertTrue((await self.client.get('/api/v1/jobs')).json()['paused'])
        for value in (0, 6, 1.5):
            self.assertEqual((await self.client.put('/api/v1/queue/settings', json={'max_concurrency': value, 'checkin_concurrency': 1})).status_code, 422)
        self.client.headers['X-CSRF-Token'] = 'invalid'
        self.assertEqual((await self.client.put('/api/v1/queue/settings', json={'max_concurrency': 2, 'checkin_concurrency': 2})).status_code, 403)

    async def test_v4_migration_preserves_data_and_defaults_serial_checkin(self):
        job_id, identifier = self.enqueue()
        old = self.store.settings().model_dump()
        old.pop('checkin_concurrency')
        old['max_concurrency'] = 3
        self.store.set_meta('settings', json.dumps(old))
        self.store.execute('ALTER TABLE jobs DROP COLUMN hidden')
        self.store.execute('PRAGMA user_version=4')
        migrated = Store(self.config.data_dir, Vault(self.config.key))
        self.assertEqual(migrated.one('PRAGMA user_version')['user_version'], SCHEMA_VERSION)
        self.assertEqual((migrated.settings().max_concurrency, migrated.settings().checkin_concurrency), (3, 1))
        self.assertEqual(migrated.one('SELECT hidden FROM jobs WHERE id=?', (job_id,))['hidden'], 0)
        self.assertEqual(migrated.stored_password(identifier), 'test-account-password')

    async def test_expiry_variants_do_not_confuse_risk_controls(self):
        for message in ('Cookie 已失效', 'COOKIE is INVALID', 'session has expired', '登录状态已失效'):
            with self.assertRaises(TaskError) as caught:
                parse_response(200, json.dumps({'message': message}))
            self.assertEqual(caught.exception.code, 'invalid')
        with self.assertRaises(TaskError) as caught:
            parse_response(403, '{"message":"captcha"}')
        self.assertEqual(caught.exception.code, 'needs_manual')


if __name__ == '__main__':
    unittest.main(verbosity=2)
