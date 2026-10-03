import json
import sys
import time
import unittest
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.crypto import Vault
from app.db import SCHEMA_VERSION, Store
from app.errors import TaskError
from app.responses import checkin_result
from app.router_service import BrowserError, BrowserSession, CHECKIN_PATH, REQUEST_MARKER, RouterService, api_headers, checkin_gain
from app.schemas import RuntimeSettings
from tools.verify_v3 import ApiTests as ExistingFixture, account


class CredentialTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ExistingFixture.asyncSetUp
    asyncTearDown = ExistingFixture.asyncTearDown
    perform = ExistingFixture.perform

    async def action(self, identifiers, action='validate', **options):
        response = await self.client.post('/api/v1/accounts/actions', json={'ids': identifiers, 'action': action, **options})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def run_next(self):
        job = self.engine.claim()
        self.assertIsNotNone(job)
        await self.engine.process(job)
        return self.store.one('SELECT * FROM jobs WHERE id=?', (job['id'],))

    async def test_extract_invalid_filters_mixed_selection(self):
        identifiers = [account(self.store, 'selection-' + status, validity=status) for status in ('valid', 'invalid', 'unknown', 'blocked', 'network_error')]
        result = await self.action(identifiers, 'extract_invalid')
        self.assertEqual((result['queued'], result['filtered_count']), (1, 4))
        job = await self.run_next()
        self.assertEqual((job['account_id'], job['kind'], job['status']), (identifiers[1], 'extract', 'success'))
        self.assertEqual(self.service.extracts, 1)
        self.assertEqual(self.service.checkins, 0)

    async def test_extract_invalid_handles_cross_page_exclusion(self):
        identifiers = [account(self.store, f'cross-page-{index}', validity='invalid') for index in range(14)]
        account(self.store, 'untouched-valid')
        response = await self.client.post('/api/v1/accounts/actions', json={'all_matching': True, 'exclude_ids': [identifiers[0]], 'action': 'extract_invalid'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()['queued'], response.json()['filtered_count']), (13, 1))
        queued = {row['account_id'] for row in self.store.all("SELECT account_id FROM jobs WHERE status='pending'")}
        self.assertEqual(queued, set(identifiers[1:]))

    async def test_extract_invalid_rechecks_state_at_execution(self):
        identifier = account(self.store, validity='invalid')
        await self.action([identifier], 'extract_invalid')
        self.store.execute("UPDATE accounts SET validity='valid' WHERE id=?", (identifier,))
        job = await self.run_next()
        self.assertEqual(job['status'], 'success')
        self.assertIn('跳过', job['message'])
        self.assertEqual(self.service.extracts, 0)

    async def test_extract_invalid_reports_missing_password(self):
        identifier = account(self.store, validity='invalid', password=None)
        result = await self.action([identifier], 'extract_invalid')
        self.assertEqual((result['queued'], result['needs_password_count']), (0, 1))
        self.assertEqual(result['job_ids'], [])

    async def test_repair_stays_in_original_job_and_does_not_checkin(self):
        identifier = account(self.store)
        self.store.set_meta('settings', self.store.settings().model_copy(update={'auto_reextract': False, 'auto_checkin': True}).model_dump_json())
        self.service.validate = AsyncMock(side_effect=[TaskError('invalid', 'expired fixture'), {'quota': 10}])
        result = await self.action([identifier], repair_invalid=True)
        job = await self.run_next()
        self.assertEqual(job['id'], result['job_ids'][0])
        self.assertEqual(job['status'], 'success')
        self.assertIn('自动重新提取并验证有效', job['message'])
        self.assertEqual(self.service.validate.await_count, 2)
        self.assertEqual(self.service.extracts, 1)
        self.assertEqual(self.service.checkins, 0)
        self.assertEqual(self.store.one('SELECT COUNT(*) AS count FROM jobs')['count'], 1)
        self.assertEqual(self.store.account(identifier)['result']['session'], 'fresh')

    async def test_repair_option_can_override_global_on(self):
        identifier = account(self.store)
        self.service.validate = AsyncMock(side_effect=TaskError('invalid', 'expired fixture'))
        await self.action([identifier], repair_invalid=False)
        self.assertEqual((await self.run_next())['status'], 'invalid')
        self.assertEqual(self.service.extracts, 0)
        self.assertEqual(self.store.one('SELECT COUNT(*) AS count FROM jobs')['count'], 1)

    async def test_repair_missing_cookie_uses_stored_password(self):
        identifier = account(self.store)
        self.store.execute('UPDATE accounts SET result_enc=NULL WHERE id=?', (identifier,))
        await self.action([identifier], repair_invalid=True)
        self.assertEqual((await self.run_next())['status'], 'success')
        self.assertEqual(self.service.extracts, 1)
        self.assertEqual(self.service.checkins, 0)

    async def test_check_only_skips_missing_cookie(self):
        identifier = account(self.store)
        self.store.execute('UPDATE accounts SET result_enc=NULL WHERE id=?', (identifier,))
        result = await self.action([identifier], repair_invalid=False)
        self.assertEqual((result['queued'], result['skipped']), (0, 1))

    async def test_network_captcha_and_upstream_errors_never_reextract(self):
        for code in ('network_error', 'needs_manual', 'upstream_error'):
            with self.subTest(code=code):
                identifier = account(self.store, 'error-' + code)
                old_result = self.store.one('SELECT result_enc FROM accounts WHERE id=?', (identifier,))['result_enc']
                self.service.validate = AsyncMock(side_effect=TaskError(code, 'fixture error'))
                await self.action([identifier], repair_invalid=True)
                self.assertEqual((await self.run_next())['status'], code)
                self.assertEqual(self.store.one('SELECT result_enc FROM accounts WHERE id=?', (identifier,))['result_enc'], old_result)
        self.assertEqual(self.service.extracts, 0)

    async def test_repair_validates_fresh_cookie_and_stops_after_one_login(self):
        identifier = account(self.store)
        self.service.validate = AsyncMock(side_effect=TaskError('invalid', 'still expired'))
        await self.action([identifier], repair_invalid=True)
        self.assertEqual((await self.run_next())['status'], 'invalid')
        self.assertEqual(self.service.extracts, 1)
        self.assertEqual(self.service.validate.await_count, 2)
        self.assertIsNone(self.engine.claim())

    async def test_failed_repair_preserves_old_cookie(self):
        identifier = account(self.store)
        self.service.validate = AsyncMock(side_effect=TaskError('invalid', 'expired fixture'))
        self.service.extract = AsyncMock(side_effect=TaskError('invalid_credentials', 'wrong password fixture'))
        original = self.store.account(identifier)['result']
        await self.action([identifier], repair_invalid=True)
        self.assertEqual((await self.run_next())['status'], 'invalid_credentials')
        self.assertEqual(self.store.account(identifier)['result'], original)
        self.assertEqual(self.store.account(identifier)['validity'], 'invalid')

    async def test_repair_without_password_is_actionable(self):
        identifier = account(self.store, password=None)
        self.service.validate = AsyncMock(side_effect=TaskError('invalid', 'expired fixture'))
        await self.action([identifier], repair_invalid=True)
        job = await self.run_next()
        self.assertEqual(job['status'], 'invalid')
        self.assertIn('未保存登录密码', job['message'])
        self.assertEqual(self.service.extracts, 0)

    async def test_repair_flag_is_rejected_for_unrelated_actions(self):
        identifier = account(self.store)
        response = await self.client.post('/api/v1/accounts/actions', json={'ids': [identifier], 'action': 'checkin', 'repair_invalid': True})
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.engine.claim())

    async def test_unconfirmed_receipt_is_not_counted_as_signed(self):
        identifier = account(self.store)
        self.service.result = {'code': 'uncertain', 'message': '未确认新增额度', 'quota': 10, 'quota_before': 10, 'quota_after': 10, 'logs': []}
        job = await self.perform(identifier)
        stats = (await self.client.get('/api/v1/stats')).json()
        self.assertEqual((job['status'], stats['signed'], stats['uncertain'], stats['earned']), ('uncertain', 0, 1, 0))
        self.assertIsNone(self.store.checkin_balance(job_id=job['id'])['earned'])
        self.assertEqual(self.service.checkins, 1)

    def accepted_receipt(self):
        return {'code': 'uncertain', 'receipt_code': 'accepted', 'verified_gain': 0,
                'message': '接口未确认新增额度', 'quota': 35, 'quota_before': 35, 'quota_after': 35, 'logs': []}

    async def test_repeat_receipt_uses_confirmed_reward_without_double_counting(self):
        identifier = account(self.store)
        self.store.execute('UPDATE accounts SET last_extracted=? WHERE id=?', (time.time() - 60, identifier))
        self.store.record_checkin(identifier, None, 'signed', 10, 35)
        self.service.result = self.accepted_receipt()
        first = await self.perform(identifier)
        second = await self.perform(identifier)
        stats = (await self.client.get('/api/v1/stats')).json()
        self.assertEqual((first['status'], second['status']), ('already_signed', 'already_signed'))
        self.assertEqual((stats['signed'], stats['already'], stats['earned']), (1, 2, 25))
        self.assertEqual(self.service.checkins, 1)
        self.assertIsNone(self.store.checkin_balance(job_id=second['id'])['earned'])
        logs = self.store.all('SELECT message FROM logs WHERE job_id=?', (second['id'],))
        self.assertTrue(any('今日签到依据' in row['message'] for row in logs))

    async def test_zero_gain_and_unverified_history_do_not_prove_a_repeat(self):
        for code, source, before, after in [('signed', 'live', 35, 35), ('signed', 'legacy', 10, 35), ('uncertain', 'live', 10, 35)]:
            with self.subTest(code=code, source=source):
                identifier = account(self.store, code + source)
                self.store.execute('UPDATE accounts SET last_extracted=? WHERE id=?', (time.time() - 60, identifier))
                self.store.record_checkin(identifier, None, code, before, after)
                self.store.execute('UPDATE checkins SET balance_source=? WHERE account_id=?', (source, identifier))
                self.service.result = self.accepted_receipt()
                self.assertEqual((await self.perform(identifier))['status'], 'uncertain')

    async def test_previous_day_future_and_old_identity_rewards_are_not_reused(self):
        now = time.time()
        zone = ZoneInfo('Asia/Shanghai')
        start = datetime.fromtimestamp(now, zone).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        for name, created, extracted in [('yesterday', start - 1, start - 60), ('future', now + 3600, now - 60), ('old-identity', now - 30, now - 10)]:
            with self.subTest(name=name):
                identifier = account(self.store, name)
                self.store.execute('UPDATE accounts SET last_extracted=? WHERE id=?', (extracted, identifier))
                self.store.record_checkin(identifier, None, 'signed', 10, 35)
                self.store.execute('UPDATE checkins SET created=? WHERE account_id=?', (created, identifier))
                self.service.result = self.accepted_receipt()
                self.assertEqual((await self.perform(identifier))['status'], 'uncertain')

    async def test_uncertain_network_result_cannot_be_resolved_from_history(self):
        identifier = account(self.store)
        self.store.execute('UPDATE accounts SET last_extracted=? WHERE id=?', (time.time() - 60, identifier))
        self.store.record_checkin(identifier, None, 'signed', 10, 35)
        for receipt, gain in [(None, 0), ('accepted', None)]:
            self.service.result = {**self.accepted_receipt(), 'receipt_code': receipt, 'verified_gain': gain}
            self.assertEqual((await self.perform(identifier))['status'], 'uncertain')

    async def test_rewards_include_concurrent_usage_in_statistics_and_backup(self):
        identifier = account(self.store)
        self.service.result.update({'quota': 123, 'quota_before': 100, 'quota_after': 123, 'used_before': 10, 'used_after': 12})
        job = await self.perform(identifier)
        balance = self.store.checkin_balance(job_id=job['id'])
        self.assertEqual((balance['delta'], balance['earned']), (23, 25))
        self.assertEqual((await self.client.get('/api/v1/stats')).json()['earned'], 25)
        document = (await self.client.post('/api/v1/backup')).json()
        self.assertEqual((document['checkins'][0]['used_before'], document['checkins'][0]['used_after']), (10, 12))
        self.store.execute('DELETE FROM checkins WHERE account_id=?', (identifier,))
        response = await self.client.post('/api/v1/backup/restore', json=document)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await self.client.get('/api/v1/stats')).json()['earned'], 25)

    async def test_schema_five_upgrade_preserves_history(self):
        identifier = account(self.store)
        self.store.record_checkin(identifier, None, 'signed', 10, 35)
        self.store.execute('ALTER TABLE checkins DROP COLUMN used_before')
        self.store.execute('ALTER TABLE checkins DROP COLUMN used_after')
        self.store.execute('PRAGMA user_version=5')
        migrated = Store(self.config.data_dir, Vault(self.config.key))
        self.assertEqual(migrated.one('PRAGMA user_version')['user_version'], SCHEMA_VERSION)
        balance = migrated.checkin_balance(account_id=identifier)
        self.assertEqual(balance['earned'], 25)
        self.assertIsNone(balance['used_before'])

    async def test_account_log_categories_are_scoped(self):
        selected = account(self.store, 'selected')
        other = account(self.store, 'other')
        self.store.log('checkin', 'selected checkin', account_id=selected)
        self.store.log('validate', 'selected validation', account_id=selected)
        self.store.log('extract', 'other extraction', account_id=other)
        response = await self.client.get('/api/v1/logs', params={'account_id': selected})
        payload = response.json()
        self.assertEqual(payload['total'], 2)
        self.assertEqual(payload['categories'], {'checkin': 1, 'validate': 1})
        self.assertTrue(all(row['account_id'] == selected for row in payload['items']))


class ReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def run_checkin(self, receipt, before, after):
        self.requests = []
        self.after_index = 0
        self.profiles = 0

        async def api(path, method='GET', **options):
            self.requests.append((path, method))
            if path == CHECKIN_PATH:
                return 200, json.dumps(receipt)
            if self.profiles == 0:
                profile = before
            else:
                profile = after[min(self.after_index, len(after) - 1)]
                self.after_index += 1
            self.profiles += 1
            if isinstance(profile, TaskError):
                raise profile
            return 200, json.dumps({'success': True, 'data': {'id': 42, **profile}})

        @asynccontextmanager
        async def signed_in(*arguments):
            yield SimpleNamespace(api=api)

        service = RouterService()
        service.signed_in = signed_in
        with patch('app.router_service.asyncio.sleep', new=AsyncMock()):
            result = await service.checkin({'session': 'fixture-secret-session', 'api_user': '42'}, None, RuntimeSettings())
        self.assertEqual(self.requests.count((CHECKIN_PATH, 'POST')), 1)
        self.assertNotIn('fixture-secret-session', json.dumps(result))
        return result

    async def test_blank_success_and_unchanged_balance_are_unconfirmed(self):
        result = await self.run_checkin({'success': True, 'message': ''}, {'quota': 50000000}, [{'quota': 50000000}])
        self.assertEqual(result['code'], 'uncertain')
        self.assertEqual(self.profiles, 4)
        self.assertTrue(any('message=（空）' in line for line in result['logs']))

    async def test_blank_success_with_verified_reward_is_signed(self):
        result = await self.run_checkin({'success': True, 'message': ''}, {'quota': 50000000}, [{'quota': 62500000}])
        self.assertEqual((result['code'], result['quota_before'], result['quota_after']), ('signed', 100, 125))

    async def test_delayed_balance_reads_do_not_repeat_post(self):
        result = await self.run_checkin({'success': True}, {'quota': 50000000}, [{'quota': 50000000}, {'quota': 62500000}])
        self.assertEqual(result['code'], 'signed')
        self.assertEqual(self.profiles, 3)

    async def test_explicit_already_signed_never_becomes_new_reward(self):
        result = await self.run_checkin({'success': False, 'message': '今天已经签到'}, {'quota': 50000000}, [{'quota': 62500000}])
        self.assertEqual(result['code'], 'already_signed')

    async def test_explicit_success_survives_balance_read_failure(self):
        result = await self.run_checkin({'success': True, 'message': '签到成功，获得 $25 额度'}, {'quota': 50000000}, [TaskError('network_error', 'fixture read failure')])
        self.assertEqual(result['code'], 'signed')
        self.assertIsNone(result['quota_after'])

    async def test_blank_success_with_missing_after_balance_is_unconfirmed(self):
        result = await self.run_checkin({'success': True}, {'quota': 50000000}, [TaskError('network_error', 'fixture read failure')])
        self.assertEqual(result['code'], 'uncertain')

    async def test_usage_can_confirm_reward_when_net_balance_is_unchanged(self):
        result = await self.run_checkin({'success': True}, {'quota': 50000000, 'used_quota': 1000000}, [{'quota': 50000000, 'used_quota': 13500000}])
        self.assertEqual(result['code'], 'signed')
        self.assertEqual((result['used_before'], result['used_after']), (2, 27))

    def test_receipt_parser_requires_explicit_success(self):
        self.assertEqual(checkin_result(200, '{"success":true,"message":""}')[0], 'accepted')
        self.assertEqual(checkin_result(200, '{"code":0}')[0], 'accepted')
        self.assertEqual(checkin_result(200, '{"success":true,"message":"签到成功"}')[0], 'signed')
        for payload in ({'success': 'true'}, {'success': False, 'code': 0}, {'code': False}, {'ret': True}):
            with self.subTest(payload=payload), self.assertRaises(TaskError):
                checkin_result(200, json.dumps(payload))

    def test_used_counter_reset_does_not_invent_reward(self):
        self.assertIsNone(checkin_gain({'quota': 50, 'used_quota': 100}, {'quota': 100, 'used_quota': 0}))

    def test_requests_match_site_cache_policy(self):
        self.assertEqual(api_headers({'api_user': 42})['Cache-Control'], 'no-store')

    async def test_navigation_error_after_post_never_retries(self):
        session = BrowserSession(None, RuntimeSettings())
        session.page = SimpleNamespace(evaluate=AsyncMock(side_effect=BrowserError('Execution context was destroyed')))
        session.settle = AsyncMock()
        with self.assertRaises(TaskError) as caught:
            await session.api(CHECKIN_PATH, method='POST')
        self.assertEqual(caught.exception.code, 'uncertain')
        self.assertEqual(session.page.evaluate.await_count, 1)
        session.settle.assert_not_awaited()

    async def test_navigation_error_during_get_can_retry(self):
        session = BrowserSession(None, RuntimeSettings())
        session.page = SimpleNamespace(evaluate=AsyncMock(side_effect=[BrowserError('Execution context was destroyed'), {'status': 200, 'text': '{}'}]))
        session.settle = AsyncMock()
        self.assertEqual(await session.api('/api/user/self'), (200, '{}'))
        self.assertEqual(session.page.evaluate.await_count, 2)

    async def test_waf_challenge_after_post_never_retries(self):
        challenge = "<html><script>var arg1='92F6A9882045E05FEDF95F928639A27F25A8C24A';(function(a,c){})();document.location.reload();</script></html>"
        session = BrowserSession(None, RuntimeSettings())
        session.page = SimpleNamespace(evaluate=AsyncMock(return_value={'status': 200, 'text': challenge}),
                                       reload=AsyncMock())
        session.settle = AsyncMock()
        with self.assertRaises(TaskError) as caught:
            await session.api(CHECKIN_PATH, method='POST')
        self.assertEqual(caught.exception.code, 'uncertain')
        self.assertIs(caught.exception.retry_proxy, False)
        session.page.evaluate.assert_called_once()
        session.page.evaluate.assert_awaited_once()
        session.page.reload.assert_not_called()
        session.settle.assert_not_called()

    async def test_waf_challenge_during_get_can_retry(self):
        challenge = "<html><script>var arg1='92F6A9882045E05FEDF95F928639A27F25A8C24A';(function(a,c){})();document.location.reload();</script></html>"
        session = BrowserSession(None, RuntimeSettings())
        session.page = SimpleNamespace(evaluate=AsyncMock(side_effect=[{'status': 200, 'text': challenge}, {'status': 200, 'text': '{}'}]),
                                       reload=AsyncMock())
        session.settle = AsyncMock()
        self.assertEqual(await session.api('/api/user/self', method='GET'), (200, '{}'))
        self.assertEqual(session.page.evaluate.await_count, 2)
        session.page.reload.assert_awaited_once_with(wait_until='domcontentloaded')
        session.settle.assert_awaited_once()

    async def test_post_redirect_is_unconfirmed_without_retry(self):
        session = BrowserSession(None, RuntimeSettings())
        session.page = SimpleNamespace(evaluate=AsyncMock(return_value={'status': 0, 'type': 'opaqueredirect', 'text': ''}))
        with self.assertRaises(TaskError) as caught:
            await session.api(CHECKIN_PATH, method='POST')
        self.assertEqual(caught.exception.code, 'uncertain')
        self.assertEqual(session.page.evaluate.await_count, 1)

    async def test_background_frontend_auto_checkin_is_blocked(self):
        session = BrowserSession(None, RuntimeSettings())
        route = SimpleNamespace(request=SimpleNamespace(url='https://anyrouter.top' + CHECKIN_PATH, method='POST', headers={}), abort=AsyncMock(), continue_=AsyncMock())
        await session.protect_destination(route)
        route.abort.assert_awaited_once()
        route.continue_.assert_not_awaited()

    async def test_explicit_checkin_is_allowed_without_leaking_internal_marker(self):
        session = BrowserSession(None, RuntimeSettings())
        route = SimpleNamespace(request=SimpleNamespace(url='https://anyrouter.top' + CHECKIN_PATH, method='POST', headers={REQUEST_MARKER: session.request_marker, 'accept': 'application/json'}), abort=AsyncMock(), continue_=AsyncMock())
        await session.protect_destination(route)
        route.abort.assert_not_awaited()
        route.continue_.assert_awaited_once_with(headers={'accept': 'application/json'})


if __name__ == '__main__':
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(case) for case in (CredentialTests, ReceiptTests))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
