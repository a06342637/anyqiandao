import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.backup_fixture import export_document
from tools.verify_v3 import ApiTests as ExistingFixture, account
from app.console_data import read_console, token_view, usage_view
from app.errors import TaskError
from app.schemas import RuntimeSettings


class ApiFixture(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ExistingFixture.asyncSetUp
    asyncTearDown = ExistingFixture.asyncTearDown
    perform = ExistingFixture.perform

    def job(self, identifier, *, kind='validate', status='success', age=0, hidden=0):
        job_id, _ = self.store.enqueue(kind, identifier)
        stamp = time.time() - age * 86400
        self.store.execute('UPDATE jobs SET status=?,created=?,finished=?,hidden=? WHERE id=?',
                           (status, stamp, stamp if status not in ('pending', 'running') else None, hidden, job_id))
        return job_id


class HistoryTests(ApiFixture):
    async def test_independent_retention_counts_and_active_protection(self):
        identifier = account(self.store)
        original = self.store.account(identifier)
        self.store.set_meta('settings', self.store.settings().model_copy(update={
            'log_retention_days': 7, 'queue_retention_days': 30, 'stats_retention_days': 90,
        }).model_dump_json())
        kept = self.job(identifier, age=10)
        expired = self.job(identifier, age=31, hidden=1)
        active = self.job(identifier, kind='extract', status='pending', age=100)
        self.store.log('validate', 'expired log', account_id=identifier, job_id=kept)
        self.store.log('extract', 'active log', account_id=identifier, job_id=active)
        self.store.execute('UPDATE logs SET created=?', (time.time() - 100 * 86400,))
        self.store.record_checkin(identifier, None, 'signed', 1, 2)
        self.store.execute('UPDATE checkins SET created=?', (time.time() - 91 * 86400,))
        self.store.record_checkin(identifier, None, 'signed', 2, 3)
        response = await self.client.post('/api/v1/logs/cleanup')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {'removed': 3, 'logs': 1, 'jobs': 1, 'checkins': 1})
        self.assertIsNotNone(self.store.one('SELECT id FROM jobs WHERE id=?', (kept,)))
        self.assertIsNone(self.store.one('SELECT id FROM jobs WHERE id=?', (expired,)))
        self.assertEqual(self.store.one('SELECT status FROM jobs WHERE id=?', (active,))['status'], 'pending')
        self.assertEqual(self.store.one('SELECT COUNT(*) AS count FROM logs WHERE job_id=?', (active,))['count'], 1)
        self.assertEqual(self.store.account(identifier)['result_enc'], original['result_enc'])
        self.assertEqual(self.store.account(identifier)['login_enc'], original['login_enc'])

    async def test_legacy_retention_inherits_log_days(self):
        settings = self.store.settings().model_dump()
        settings.pop('queue_retention_days')
        settings['log_retention_days'] = 19
        self.store.set_meta('settings', json.dumps(settings))
        response = (await self.client.get('/api/v1/settings')).json()
        self.assertEqual(response['settings']['queue_retention_days'], 19)
        self.assertGreater(response['next_log_cleanup'], time.time())

    async def test_retention_save_preserves_other_settings(self):
        response = await self.client.put('/api/v1/settings', json={'queue_retention_days': 17, 'stats_retention_days': 33, 'log_cleanup_hours': 5})
        self.assertEqual(response.status_code, 200)
        settings = self.store.settings()
        self.assertEqual((settings.queue_retention_days, settings.stats_retention_days, settings.log_cleanup_hours), (17, 33, 5))
        self.assertFalse(settings.auto_checkin)
        self.assertEqual(settings.proxy_mode, 'direct')

    async def test_manual_log_clear_preserves_active_and_other_scope(self):
        first = account(self.store, 'first')
        second = account(self.store, 'second')
        active = self.job(first, status='running')
        for identifier, job_id in ((first, None), (first, active), (second, None)):
            self.store.log('validate', 'fixture', account_id=identifier, job_id=job_id)
        response = await self.client.post('/api/v1/logs/delete', json={'all_matching': True, 'account_id': first})
        self.assertEqual(response.json(), {'removed': 1, 'protected': 1})
        self.assertEqual(self.store.one('SELECT COUNT(*) AS count FROM logs WHERE account_id=?', (second,))['count'], 1)

    async def test_log_cross_page_selection_and_exclusions(self):
        for index in range(25):
            self.store.log('checkin', f'fixture {index}')
        identifiers = [str(row['id']) for row in self.store.all('SELECT id FROM logs')]
        response = await self.client.post('/api/v1/logs/delete', json={'all_matching': True, 'category': 'checkin', 'exclude_ids': identifiers[:2]})
        self.assertEqual(response.json()['removed'], 23)
        self.assertEqual(self.store.one("SELECT COUNT(*) AS count FROM logs WHERE category='checkin'")['count'], 2)

    async def test_cutoff_and_invalid_selection(self):
        self.store.log('extract', 'old')
        self.store.execute('UPDATE logs SET created=?', (time.time() - 1000,))
        self.store.log('extract', 'recent')
        response = await self.client.post('/api/v1/logs/delete', json={'all_matching': True, 'category': 'extract', 'before': time.time() - 100})
        self.assertEqual(response.json()['removed'], 1)
        self.assertEqual((await self.client.post('/api/v1/logs/delete', json={})).status_code, 400)
        self.assertEqual((await self.client.post('/api/v1/logs/delete', json={'all_matching': True, 'before': time.time() + 3600})).status_code, 400)

    async def test_checkin_history_and_account_statistics_clear(self):
        identifier = account(self.store)
        self.store.record_checkin(identifier, None, 'signed', 10, 15, used_before=1, used_after=2)
        response = await self.client.get('/api/v1/checkins', params={'account_id': identifier})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['items'][0]['earned'], 6)
        self.assertEqual((await self.client.get('/api/v1/stats')).json()['signed'], 1)
        response = await self.client.post('/api/v1/checkins/delete', json={'all_matching': True, 'account_id': identifier})
        self.assertEqual(response.json()['removed'], 1)
        self.assertEqual((await self.client.get('/api/v1/stats')).json()['signed'], 0)
        self.assertIsNotNone(self.store.account(identifier)['result'])

    async def test_checkin_cleanup_protects_active_results(self):
        identifier = account(self.store)
        active = self.job(identifier, kind='checkin', status='running')
        self.store.record_checkin(identifier, active, 'signed', 10, 15)
        response = await self.client.post('/api/v1/checkins/delete', json={'all_matching': True})
        self.assertEqual(response.json(), {'removed': 0, 'protected': 1})

    async def test_queue_finished_cleanup_preserves_pending_and_logs(self):
        identifier = account(self.store)
        finished = self.job(identifier)
        pending = self.job(identifier, kind='checkin', status='pending')
        self.store.log('validate', 'keep log', job_id=finished)
        response = await self.client.post('/api/v1/jobs/delete', json={'all_matching': True, 'state': 'finished'})
        self.assertEqual(response.json(), {'removed': 1, 'protected': 0})
        self.assertEqual(self.store.one('SELECT hidden FROM jobs WHERE id=?', (finished,))['hidden'], 1)
        self.assertEqual(self.store.one('SELECT status FROM jobs WHERE id=?', (pending,))['status'], 'pending')
        self.assertEqual(self.store.one('SELECT COUNT(*) AS count FROM logs WHERE job_id=?', (finished,))['count'], 1)

    async def test_bulk_queue_cancel_with_safe_exit_protection(self):
        identifier = account(self.store)
        pending = self.job(identifier, status='pending')
        running = self.job(identifier, kind='checkin', status='running')
        response = await self.client.post('/api/v1/jobs/delete', json={'ids': [pending, running]})
        self.assertEqual(response.json(), {'removed': 1, 'protected': 1})
        self.assertEqual(self.store.one('SELECT status FROM jobs WHERE id=?', (pending,))['status'], 'cancelled')
        self.assertEqual(self.store.one('SELECT hidden FROM jobs WHERE id=?', (running,))['hidden'], 0)

    async def test_cleanup_requires_csrf(self):
        self.client.headers['X-CSRF-Token'] = 'invalid'
        for path in ('logs/delete', 'checkins/delete', 'jobs/delete'):
            self.assertEqual((await self.client.post('/api/v1/' + path, json={'all_matching': True})).status_code, 403)


class ManualCredentialTests(ApiFixture):
    async def import_manual(self, api_user='123', session='manual-test-session', username='manual fixture'):
        response = await self.client.post('/api/v1/accounts/import-credentials', json={'accounts': [{'api_user': api_user, 'session': session, 'username': username}]})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_manual_import_is_encrypted_and_passwordless(self):
        imported = await self.import_manual(api_user=123, session='session=manual-test-session; Path=/')
        self.assertEqual((imported['added'], imported['queued']), (1, 0))
        identifier = imported['account_ids'][0]
        row = self.store.account(identifier)
        self.assertIsNone(row['login']['password'])
        self.assertEqual(row['result'], {'session': 'manual-test-session', 'api_user': '123'})
        self.assertNotIn('manual-test-session', row['result_enc'])
        public = (await self.client.get('/api/v1/accounts')).json()['items'][0]
        self.assertEqual(public['credential_source'], 'session')
        self.assertNotIn('manual-test-session', json.dumps(public))

    async def test_manual_import_only_enqueues_checkin(self):
        self.store.set_meta('settings', self.store.settings().model_copy(update={'auto_checkin': True}).model_dump_json())
        imported = await self.import_manual()
        self.assertEqual(imported['queued'], 1)
        self.assertEqual([row['kind'] for row in self.store.all('SELECT kind FROM jobs')], ['checkin'])
        self.assertEqual(self.store.one('SELECT COUNT(*) AS count FROM schedule_accounts')['count'], 1)
        await self.engine.process(self.engine.claim())
        self.assertEqual((self.service.checkins, self.service.extracts), (1, 0))

    async def test_duplicate_identity_does_not_override_password_account(self):
        identifier = account(self.store, 'password owner')
        original = self.store.account(identifier)
        imported = await self.import_manual(api_user='42', username='different display name')
        self.assertEqual((imported['added'], imported['skipped']), (0, 1))
        current = self.store.account(identifier)
        self.assertEqual((current['login_enc'], current['result_enc']), (original['login_enc'], original['result_enc']))

    async def test_duplicate_manual_import_is_idempotent(self):
        first = await self.import_manual()
        second = await self.import_manual(username='another label')
        self.assertEqual(second['account_ids'], first['account_ids'])
        self.assertEqual((second['added'], second['skipped']), (0, 1))

    async def test_invalid_manual_account_stops_scheduled_retries(self):
        self.store.set_meta('settings', self.store.settings().model_copy(update={'auto_checkin': True}).model_dump_json())
        imported = await self.import_manual(session='expired')
        identifier = imported['account_ids'][0]
        await self.engine.process(self.engine.claim())
        self.assertEqual(self.store.account(identifier)['validity'], 'invalid')
        self.assertIn('手动更新', self.store.account(identifier)['message'])
        self.assertEqual(self.service.extracts, 0)
        self.store.execute('UPDATE schedules SET next_run=?', (time.time() - 60,))
        self.engine.tick_schedules()
        self.assertEqual(self.store.one('SELECT COUNT(*) AS count FROM jobs')['count'], 1)
        response = await self.client.put(f'/api/v1/accounts/{identifier}/credentials', json={'session': 'refreshed-manual-session', 'api_user': '123'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()['job_ids']), 1)
        await self.engine.process(self.engine.claim())
        self.assertEqual(self.store.account(identifier)['checkin_status'], 'signed')
        self.assertEqual(self.service.extracts, 0)

    async def test_manual_validation_never_extracts(self):
        identifier = (await self.import_manual())['account_ids'][0]
        self.service.validate = AsyncMock(side_effect=TaskError('invalid', 'expired fixture'))
        result = await self.perform(identifier, 'validate')
        self.assertEqual(result['status'], 'invalid')
        self.assertIn('手动更新', result['message'])
        self.assertEqual(self.service.extracts, 0)

    async def test_password_account_still_recovers_automatically(self):
        identifier = account(self.store, validity='invalid')
        result = await self.perform(identifier)
        self.assertEqual(result['status'], 'signed')
        self.assertEqual(self.service.extracts, 1)

    async def test_update_rejects_other_user_and_active_jobs(self):
        identifier = (await self.import_manual())['account_ids'][0]
        original = self.store.account(identifier)['result_enc']
        path = f'/api/v1/accounts/{identifier}/credentials'
        self.assertEqual((await self.client.put(path, json={'session': 'another-session', 'api_user': '999'})).status_code, 400)
        self.job(identifier, status='pending')
        self.assertEqual((await self.client.put(path, json={'session': 'another-session', 'api_user': '123'})).status_code, 409)
        self.assertEqual(self.store.account(identifier)['result_enc'], original)

    async def test_manual_rename_preserves_credentials(self):
        identifier = (await self.import_manual())['account_ids'][0]
        original = self.store.account(identifier)['result_enc']
        response = await self.client.put(f'/api/v1/accounts/{identifier}', json={'username': 'renamed manual fixture'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.store.account(identifier)['result_enc'], original)
        self.assertEqual(self.store.public_account(self.store.account(identifier))['credential_source'], 'session')

    async def test_manual_can_explicitly_switch_to_password_mode(self):
        identifier = (await self.import_manual())['account_ids'][0]
        response = await self.client.put(f'/api/v1/accounts/{identifier}', json={'username': 'actual-login', 'password': 'new-synthetic-password'})
        self.assertEqual(response.status_code, 200)
        row = self.store.account(identifier)
        self.assertIsNone(row['result'])
        self.assertEqual(self.store.public_account(row)['credential_source'], 'password')

    async def test_invalid_credentials_rejected_without_partial_import(self):
        for value in ('0', '-1', '12.5', True, 12.5, '9223372036854775808', 'abc'):
            response = await self.client.post('/api/v1/accounts/import-credentials', json={'accounts': [{'session': 'synthetic-session', 'api_user': value}]})
            self.assertEqual(response.status_code, 422, str(value))
        self.assertEqual(self.store.one('SELECT COUNT(*) AS count FROM accounts')['count'], 0)

    async def test_manual_backup_roundtrip_keeps_source(self):
        await self.import_manual()
        document = await export_document(self.client)
        self.assertFalse(document['accounts'][0].get('password'))
        self.store.execute('DELETE FROM accounts')
        response = await self.client.post('/api/v1/backup/restore', json=document)
        self.assertEqual(response.status_code, 200, response.text)
        public = (await self.client.get('/api/v1/accounts')).json()['items'][0]
        self.assertEqual(public['credential_source'], 'session')
        self.assertFalse(public['has_password'])


class ConsoleTests(ApiFixture):
    async def query(self, identifier, view='tokens', **values):
        response = await self.client.post(f'/api/v1/accounts/{identifier}/insights', json={'view': view, **values})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['job_id']

    async def test_console_is_serialized_readonly_and_encrypted(self):
        identifier = account(self.store)
        original = self.store.account(identifier)
        secret = 'sk-only-a-synthetic-token-for-tests'
        self.service.insights = AsyncMock(return_value={'view': 'tokens', 'items': [{'key': secret}]})
        job_id = await self.query(identifier)
        self.assertEqual(await self.query(identifier), job_id)
        job = self.engine.claim()
        self.store.enqueue('checkin', identifier)
        self.assertIsNone(self.engine.claim())
        await self.engine.process(job)
        response = await self.client.get(f'/api/v1/accounts/{identifier}/insights/{job_id}')
        self.assertEqual(response.json()['data']['items'][0]['key'], secret)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        row = self.store.one('SELECT * FROM console_results WHERE job_id=?', (job_id,))
        self.assertNotIn(secret, row['result_enc'])
        self.assertNotIn(secret, json.dumps(self.store.all('SELECT * FROM logs')))
        self.assertNotIn(secret, json.dumps(self.store.all('SELECT * FROM jobs')))
        current = self.store.account(identifier)
        self.assertEqual((current['login_enc'], current['result_enc'], current['last_checkin']), (original['login_enc'], original['result_enc'], original['last_checkin']))
        self.assertEqual((self.service.extracts, self.service.checkins), (0, 0))

    async def test_concurrent_different_queries_are_not_mixed(self):
        identifier = account(self.store)
        await self.query(identifier)
        response = await self.client.post(f'/api/v1/accounts/{identifier}/insights', json={'view': 'dashboard'})
        self.assertEqual(response.status_code, 409)

    async def test_console_results_expire_and_are_account_scoped(self):
        identifier = account(self.store)
        other = account(self.store, 'other')
        self.service.insights = AsyncMock(return_value={'view': 'tokens', 'items': []})
        job_id = await self.query(identifier)
        await self.engine.process(self.engine.claim())
        self.assertEqual((await self.client.get(f'/api/v1/accounts/{other}/insights/{job_id}')).status_code, 404)
        self.store.execute('UPDATE console_results SET expires=0')
        self.assertEqual((await self.client.get(f'/api/v1/accounts/{identifier}/insights/{job_id}')).status_code, 410)

    async def test_console_results_invalidated_when_credentials_change(self):
        identifier = account(self.store)
        self.service.insights = AsyncMock(return_value={'view': 'tokens', 'items': []})
        job_id = await self.query(identifier)
        await self.engine.process(self.engine.claim())
        self.store.execute('UPDATE accounts SET result_enc=? WHERE id=?', (self.store.vault.seal({'session': 'new', 'api_user': '42'}, f'result:{identifier}'), identifier))
        self.assertEqual((await self.client.get(f'/api/v1/accounts/{identifier}/insights/{job_id}')).status_code, 409)

    async def test_invalid_manual_console_does_not_log_in(self):
        identifier = account(self.store, password=None)
        self.service.insights = AsyncMock(side_effect=TaskError('invalid', 'expired fixture'))
        job_id = await self.query(identifier)
        await self.engine.process(self.engine.claim())
        self.assertEqual(self.store.account(identifier)['validity'], 'invalid')
        self.assertIn('手动更新', self.store.account(identifier)['message'])
        self.assertEqual(self.service.extracts, 0)
        self.assertEqual((await self.client.get(f'/api/v1/accounts/{identifier}/insights/{job_id}')).json()['status'], 'invalid')

    async def test_console_secrets_are_not_exported_in_backup(self):
        identifier = account(self.store)
        self.service.insights = AsyncMock(return_value={'view': 'tokens', 'items': [{'key': 'sk-synthetic-backup-exclusion'}]})
        await self.query(identifier)
        await self.engine.process(self.engine.claim())
        self.assertNotIn('sk-synthetic-backup-exclusion', json.dumps(await export_document(self.client)))


class ConsoleAdapterTests(unittest.IsolatedAsyncioTestCase):
    def session(self, responses):
        return type('Session', (), {'api': AsyncMock(side_effect=[(200, json.dumps({'success': True, 'data': data})) for data in responses])})()

    def profile(self):
        return {'id': 42, 'username': 'fixture', 'quota': 5000000, 'used_quota': 2500000, 'request_count': 99, 'group': 'default'}

    async def test_read_tokens_uses_only_get_and_correct_pagination(self):
        session = self.session([self.profile(), [{'id': 1, 'user_id': 42, 'key': 'synthetic-token-key', 'unlimited_quota': True, 'model_limits_enabled': True, 'model_limits': 'model-a,model-b', 'allow_ips': '127.0.0.1\n192.0.2.0/24', 'expired_time': -1}]])
        result = await read_console(session, {'session': 'never-print', 'api_user': '42'}, RuntimeSettings(), {'view': 'tokens', 'page': 2, 'limit': 10})
        self.assertEqual(result['items'][0]['key'], 'sk-synthetic-token-key')
        self.assertEqual(result['items'][0]['model_limits'], ['model-a', 'model-b'])
        self.assertEqual(result['profile']['quota'], 10)
        self.assertIn('/api/token/?p=1&size=10', session.api.call_args_list[1].args[0])
        self.assertTrue(all(call.kwargs.get('method', 'GET') == 'GET' for call in session.api.call_args_list))

    async def test_token_owner_mismatch_and_masked_key(self):
        with self.assertRaises(TaskError):
            token_view({'id': 1, 'user_id': 99}, '42')
        self.assertIsNone(token_view({'key': 'sk-*********'}, '42')['key'])

    async def test_usage_totals_and_empty_data(self):
        now = int(time.time())
        rows = [{'user_id': 42, 'model_name': 'model-a', 'count': 6, 'token_used': 1200, 'quota': 500000, 'created_at': now}]
        result = usage_view(rows, now - 3600, now, 'hour', 'Asia/Seoul', '42')
        self.assertEqual((result['requests'], result['tokens'], result['cost'], result['rpm'], result['tpm']), (6, 1200, 1, 0.1, 20))
        self.assertEqual(usage_view([], now - 3600, now, 'hour', 'UTC', '42')['requests'], 0)

    async def test_usage_failure_is_not_fabricated_as_zero(self):
        session = self.session([self.profile()])
        session.api.side_effect = [(200, json.dumps({'success': True, 'data': self.profile()})), (503, 'unavailable')]
        result = await read_console(session, {'session': 'never-print', 'api_user': '42'}, RuntimeSettings(), {'view': 'dashboard', 'range': 'day'})
        self.assertIsNone(result['usage'])
        self.assertTrue(result['usage_error'])
        self.assertEqual(result['profile']['quota'], 10)

    async def test_identity_is_verified_before_token_request(self):
        session = self.session([self.profile() | {'id': 99}])
        with self.assertRaises(TaskError):
            await read_console(session, {'session': 'never-print', 'api_user': '42'}, RuntimeSettings(), {'view': 'tokens', 'page': 1, 'limit': 10})
        self.assertEqual(session.api.call_count, 1)


if __name__ == '__main__':
    unittest.main(defaultTest=['HistoryTests', 'ManualCredentialTests', 'ConsoleTests', 'ConsoleAdapterTests'], verbosity=2)
