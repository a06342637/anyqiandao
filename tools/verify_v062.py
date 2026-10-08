"""Regression coverage for the seven v0.6.1 audit findings; synthetic data only."""
import asyncio
import json
import time
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from app.checkin_state import BEIJING, submitted_today, observe_balance, merge_daily_state
from app.crypto import Vault
from app.db import Store
from app.errors import TaskError
from app.remote_backup_schema import RemoteBackupSettings, OSSSettings
from tools import verify_serial_routes as fixtures
from tools import verify_v3 as base
from tools.backup_fixture import export_document


class AuditFixTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.ApiTests.asyncSetUp
    asyncTearDown = fixtures.SerialRouteTests.asyncTearDown
    proxy = fixtures.SerialRouteTests.proxy
    routes = fixtures.SerialRouteTests.routes
    run_job = fixtures.SerialRouteTests.run_job

    async def test_referenced_proxy_cannot_be_deleted_and_backup_restores(self):
        proxy = await self.proxy('used')
        identifier = base.account(self.store)
        for configure in ('default', 'account', 'schedule'):
            with self.subTest(configure=configure):
                if configure == 'default':
                    await self.routes(tokens={'mode': 'proxy', 'proxy_id': proxy})
                elif configure == 'account':
                    await self.routes(tokens={'mode': 'direct'})
                    await self.client.put('/api/v1/accounts/routes/checkin', json={'ids': [identifier], 'network_route': {'mode': 'proxy', 'proxy_id': proxy}})
                else:
                    await self.client.put('/api/v1/accounts/routes/checkin', json={'ids': [identifier], 'network_route': {'mode': 'direct'}})
                    await self.client.post('/api/v1/schedules', json={'ids': [identifier], 'name': 'referenced', 'network_route': {'mode': 'proxy', 'proxy_id': proxy}})
                self.assertEqual((await self.client.delete('/api/v1/proxies/' + proxy)).status_code, 409)
                doc = await export_document(self.client)
                result = await self.client.post('/api/v1/backup/restore', json=doc)
                self.assertEqual(result.status_code, 200, result.text)

    async def test_proxy_protocols_remain_distinct_when_restoring(self):
        first = await self.proxy('dual')
        config = self.store.vault.open(self.store.one('SELECT config_enc FROM proxies WHERE id=?', (first,))['config_enc'], f'proxy:{first}')
        result = await self.client.post('/api/v1/proxies', json={**config, 'scheme': 'socks5', 'name': 'second'})
        second = result.json()['id']
        for job in result.json()['job_ids']:
            self.engine.cancel(job)
        await self.routes(tokens={'mode': 'proxy', 'proxy_id': first}, dashboard={'mode': 'proxy', 'proxy_id': second})
        doc = await export_document(self.client)
        self.store.execute('DELETE FROM proxies')
        restored = await self.client.post('/api/v1/backup/restore', json=doc)
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(self.store.one('SELECT COUNT(*) AS n FROM proxies')['n'], 2)
        routes = self.store.settings().operation_routes
        self.assertNotEqual(routes.tokens.proxy_id, routes.dashboard.proxy_id)

    async def test_memory_drop_returns_task_with_payload_to_queue(self):
        identifier = base.account(self.store)
        job_id, _ = self.store.enqueue('validate', identifier, payload={'repair_invalid': False})
        with patch('app.engine.memory_status', return_value={'ready': False}):
            await self.engine.process(self.engine.claim())
        job = self.store.one('SELECT * FROM jobs WHERE id=?', (job_id,))
        self.assertEqual(job['status'], 'pending')
        self.assertIsNotNone(job['payload_enc'])
        self.assertIsNone(job['finished'])
        await self.engine.process(self.engine.claim())
        self.assertEqual(self.store.one('SELECT status FROM jobs WHERE id=?', (job_id,))['status'], 'success')

    async def test_uncertain_submissions_survive_restart_history_cleanup_and_backup(self):
        identifier = base.account(self.store)
        self.service.validate = AsyncMock(return_value={'quota': 10, 'profile': {'used_quota': 0}})
        count = 0
        async def submit(*args, on_submit, **kwargs):
            nonlocal count
            on_submit()
            count += 1
            raise TaskError('network_error', 'synthetic missing receipt', retry_proxy=True)
        self.service.checkin = submit
        self.assertEqual((await self.run_job('checkin', identifier))['status'], 'uncertain')
        self.store.execute('DELETE FROM jobs')
        self.store.execute('DELETE FROM checkins')
        reopened = Store(self.config.data_dir, Vault(self.config.key))
        self.assertTrue(submitted_today(reopened.daily_state(reopened.account(identifier)), '42', time.time()))
        backup = await export_document(self.client)
        self.assertEqual((await self.client.post('/api/v1/backup/restore', json=backup)).status_code, 200)
        schedule = (await self.client.post('/api/v1/schedules', json={'ids': [identifier], 'name': 'again', 'interval_minutes': 5})).json()['id']
        self.store.execute('UPDATE schedules SET next_run=? WHERE id=?', (time.time() - 1, schedule))
        self.engine.tick_schedules()
        await self.engine.process(self.engine.claim())
        self.assertEqual(count, 1)
        self.assertEqual(self.store.one('SELECT status FROM jobs')['status'], 'uncertain')
        # A delayed reward is reconciled without another submission.
        self.service.validate = AsyncMock(return_value={'quota': 35, 'profile': {'used_quota': 0}})
        self.assertEqual((await self.run_job('checkin', identifier))['status'], 'already_signed')
        self.assertEqual(count, 1)

    async def test_migration_protects_legacy_uncertain_account(self):
        identifier = base.account(self.store)
        self.store.execute("UPDATE accounts SET checkin_status='uncertain',last_checkin=? WHERE id=?", (time.time(), identifier))
        self.store.execute('PRAGMA user_version=9')
        migrated = Store(self.config.data_dir, Vault(self.config.key))
        self.assertTrue(submitted_today(migrated.daily_state(migrated.account(identifier)), '42', time.time()))

    async def test_old_backup_preserves_today_uncertain_protection(self):
        identifier = base.account(self.store)
        self.store.execute("UPDATE accounts SET checkin_status='uncertain',last_checkin=? WHERE id=?", (time.time(), identifier))
        doc = await export_document(self.client)
        doc['schema_version'] = 9
        self.assertEqual((await self.client.post('/api/v1/backup/restore', json=doc)).status_code, 200)
        self.assertTrue(submitted_today(self.store.daily_state(self.store.account(identifier)), '42', time.time()))

    async def test_cancel_before_and_after_submission_have_distinct_status(self):
        for submitted in (False, True):
            started = asyncio.Event()
            async def slow(*args, on_submit, **kwargs):
                if submitted:
                    on_submit()
                started.set()
                await asyncio.Event().wait()
            self.service.checkin = slow
            identifier = base.account(self.store, 'cancel-' + str(submitted))
            job_id, _ = self.store.enqueue('checkin', identifier)
            job = self.engine.claim()
            task = asyncio.create_task(self.engine.run_job(job))
            self.engine.running[job_id] = (job, task)
            await started.wait()
            task.cancel()
            await task
            self.assertEqual(self.store.one('SELECT status FROM jobs WHERE id=?', (job_id,))['status'], 'uncertain' if submitted else 'cancelled')

    async def test_all_proxy_options_are_returned_without_secrets(self):
        for index in range(105):
            identifier = str(index)
            self.store.execute('INSERT INTO proxies(id,name,config_enc,created) VALUES (?,?,?,?)',
                (identifier, 'node-' + identifier, self.store.vault.seal({'scheme': 'http', 'host': 'private.invalid', 'password': 'synthetic-secret'}, f'proxy:{identifier}'), time.time()))
        response = await self.client.get('/api/v1/proxies/options')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['items']), 105)
        self.assertNotIn('synthetic-secret', response.text)
        self.assertNotIn('private.invalid', response.text)

    async def test_backup_and_browser_execution_share_one_slot(self):
        service = self.app.state.remote_backup
        service.save(RemoteBackupSettings(oss=OSSSettings(enabled=True, bucket='synthetic-bucket', access_key_id='synthetic-id', access_key_secret='synthetic-secret')))
        entered, release = asyncio.Event(), asyncio.Event()
        async def archive(*args):
            entered.set()
            await release.wait()
        with patch.object(service, '_run', side_effect=archive):
            await self.engine.resource_lock.acquire()
            service.launch()
            await asyncio.sleep(0.02)
            self.assertFalse(entered.is_set())
            self.engine.resource_lock.release()
            await asyncio.wait_for(entered.wait(), 2)
            identifier = base.account(self.store)
            job_id, _ = self.store.enqueue('validate', identifier)
            await self.engine.start()
            await asyncio.sleep(0.05)
            self.assertEqual(self.store.one('SELECT status FROM jobs WHERE id=?', (job_id,))['status'], 'pending')
            release.set()
            await service.task
            for _ in range(100):
                if self.store.one('SELECT status FROM jobs WHERE id=?', (job_id,))['status'] == 'success':
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(self.store.one('SELECT status FROM jobs WHERE id=?', (job_id,))['status'], 'success')


class SubmissionStateTests(unittest.TestCase):
    def test_guard_expires_next_beijing_day_and_never_follows_other_identity(self):
        now = datetime(2026, 10, 8, 23, 59, tzinfo=BEIJING).timestamp()
        state = observe_balance(None, '42', 10, 0, now)
        state.submitted_at = now
        self.assertTrue(submitted_today(state, '42', now + 1))
        self.assertFalse(submitted_today(state, '99', now + 1))
        self.assertFalse(submitted_today(state, '42', now + 61))
        newer = observe_balance(state, '42', 10, 0, now + 61)
        self.assertIsNone(newer.submitted_at)
        merged = merge_daily_state(state, newer, '42', now + 61)
        self.assertIsNone(merged.submitted_at)


if __name__ == '__main__':
    unittest.main(verbosity=2)
