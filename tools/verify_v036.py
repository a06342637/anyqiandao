import json
import sys
import time
import unittest
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.checkin_state import BEIJING, confirmed_today, merge_daily_state, observe_balance
from app.router_service import CHECKIN_PATH, RouterService
from tools.verify_v3 import ApiTests as ExistingFixture, account


class BalanceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 15, 12, tzinfo=BEIJING).timestamp()
        self.baseline = observe_balance(None, '42', 100, 10, self.now)

    def test_incomplete_read_preserves_a_complete_balance_pair(self):
        for quota, used in ((None, None), (None, 20), (120, None)):
            with self.subTest(quota=quota, used=used):
                partial = observe_balance(self.baseline, '42', quota, used, self.now + 1)
                self.assertEqual((partial.quota, partial.used_quota), (100, 10))
                self.assertFalse(confirmed_today(partial, '42', self.now + 1))
                recovered = observe_balance(partial, '42', 120, 15, self.now + 2)
                self.assertTrue(confirmed_today(recovered, '42', self.now + 2))

    def test_recovery_does_not_mix_balances_from_different_samples(self):
        partial = observe_balance(self.baseline, '42', None, 35, self.now + 1)
        recovered = observe_balance(partial, '42', 100, 10, self.now + 2)
        self.assertFalse(confirmed_today(recovered, '42', self.now + 2))

    def test_recovery_after_consumption_does_not_invent_credit(self):
        partial = observe_balance(self.baseline, '42', 90, None, self.now + 1)
        recovered = observe_balance(partial, '42', 75, 35, self.now + 2)
        self.assertFalse(confirmed_today(recovered, '42', self.now + 2))

    def test_failed_read_across_midnight_does_not_reuse_yesterday_baseline(self):
        midnight = datetime(2026, 9, 16, tzinfo=BEIJING).timestamp()
        partial = observe_balance(self.baseline, '42', None, None, midnight)
        recovered = observe_balance(partial, '42', 125, 10, midnight + 1)
        self.assertFalse(confirmed_today(recovered, '42', midnight + 1))

    def test_explicit_receipt_survives_missing_balance(self):
        confirmed = observe_balance(self.baseline, '42', None, None, self.now + 1, confirm=True)
        self.assertTrue(confirmed_today(confirmed, '42', self.now + 1))
        self.assertEqual((confirmed.quota, confirmed.used_quota), (100, 10))


class StateMergeTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 15, 12, tzinfo=BEIJING).timestamp()
        self.confirmed = observe_balance(None, '42', 125, 10, self.now - 2, confirm=True)

    def test_newer_partial_backup_preserves_complete_pair_and_receipt(self):
        partial = observe_balance(None, '42', None, None, self.now - 1)
        merged = merge_daily_state(self.confirmed, partial, '42', self.now)
        self.assertTrue(confirmed_today(merged, '42', self.now))
        self.assertEqual((merged.quota, merged.used_quota), (125, 10))
        self.assertEqual(merged.confirmed_at, self.confirmed.confirmed_at)

    def test_other_identity_and_future_snapshot_cannot_override_state(self):
        for incoming in (observe_balance(None, '99', 999, 10, self.now - 1, confirm=True),
                         observe_balance(None, '42', 999, 10, self.now + 1, confirm=True)):
            with self.subTest(incoming=incoming.api_user):
                merged = merge_daily_state(self.confirmed, incoming, '42', self.now)
                self.assertEqual(merged, self.confirmed)

    def test_malformed_day_cannot_fabricate_today_receipt(self):
        malformed = self.confirmed.model_copy(update={'observed_at': self.now - 86400})
        self.assertFalse(confirmed_today(malformed, '42', self.now))
        self.assertIsNone(merge_daily_state(None, malformed, '42', self.now))

    def test_midnight_merge_does_not_carry_yesterday_balance_or_confirmation(self):
        midnight = datetime(2026, 9, 16, tzinfo=BEIJING).timestamp()
        partial = observe_balance(None, '42', None, None, midnight)
        merged = merge_daily_state(self.confirmed, partial, '42', midnight + 1)
        self.assertFalse(confirmed_today(merged, '42', midnight + 1))
        self.assertEqual((merged.quota, merged.used_quota), (None, None))
        self.assertEqual(merged.last_signed_at, self.confirmed.last_signed_at)

    def test_multiple_confirmations_keep_first_evidence_time(self):
        newer = observe_balance(None, '42', 110, 25, self.now - 1, confirm=True)
        merged = merge_daily_state(self.confirmed, newer, '42', self.now)
        self.assertEqual(merged.confirmed_at, self.confirmed.confirmed_at)
        self.assertEqual((merged.quota, merged.used_quota), (110, 25))


class RestoreSafetyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ExistingFixture.asyncSetUp
    asyncTearDown = ExistingFixture.asyncTearDown
    perform = ExistingFixture.perform

    async def test_missing_balance_does_not_clear_last_known_account_balance(self):
        identifier = account(self.store)
        self.store.execute('UPDATE accounts SET quota=100 WHERE id=?', (identifier,))
        self.service.validate = AsyncMock(return_value={'quota': None})
        response = await self.client.post('/api/v1/accounts/actions', json={'action': 'validate', 'ids': [identifier]})
        self.assertEqual(response.status_code, 200, response.text)
        await self.engine.process(self.engine.claim())
        self.assertEqual(self.store.account(identifier)['quota'], 100)
        self.service.result = {'code': 'uncertain', 'receipt_code': 'accepted', 'verified_gain': None,
                               'message': '模拟余额未读到', 'quota': None, 'quota_before': None, 'quota_after': None, 'logs': []}
        self.assertEqual((await self.perform(identifier))['status'], 'uncertain')
        self.assertEqual(self.store.account(identifier)['quota'], 100)

    async def test_newer_unconfirmed_backup_cannot_erase_today_confirmation(self):
        identifier = account(self.store)
        now = time.time()
        confirmed = self.store.observe_account(identifier, '42', 125, 10, at=now - 60, confirm=True)
        document = (await self.client.post('/api/v1/backup')).json()
        document['accounts'][0]['daily_checkin'] = observe_balance(None, '42', 110, 25, now - 30).model_dump()
        response = await self.client.post('/api/v1/backup/restore', json=document)
        self.assertEqual(response.status_code, 200, response.text)
        restored = self.store.daily_state(self.store.account(identifier))
        self.assertTrue(confirmed_today(restored, '42', time.time()))
        self.assertEqual(restored.confirmed_at, confirmed.confirmed_at)
        self.assertEqual((restored.quota, restored.used_quota), (110, 25))

    async def test_older_receipt_can_complete_newer_unconfirmed_snapshot(self):
        identifier = account(self.store)
        now = time.time()
        confirmed = self.store.observe_account(identifier, '42', 125, 10, at=now - 60, confirm=True)
        document = (await self.client.post('/api/v1/backup')).json()
        self.store.execute('UPDATE accounts SET checkin_state_enc=NULL WHERE id=?', (identifier,))
        self.store.observe_account(identifier, '42', 110, 25, at=now - 10)
        response = await self.client.post('/api/v1/backup/restore', json=document)
        self.assertEqual(response.status_code, 200, response.text)
        restored = self.store.daily_state(self.store.account(identifier))
        self.assertTrue(confirmed_today(restored, '42', time.time()))
        self.assertEqual(restored.confirmed_at, confirmed.confirmed_at)
        self.assertEqual(restored.observed_at, now - 10)
        self.assertEqual((restored.quota, restored.used_quota), (110, 25))

    async def test_old_backup_cannot_roll_back_balance_read_by_tokens(self):
        identifier = account(self.store)
        now = time.time()
        self.store.execute('UPDATE accounts SET quota=?,created=?,updated=?,last_validated=? WHERE id=?',
                           (100, now - 100, now - 100, now - 100, identifier))
        self.store.observe_account(identifier, '42', 100, 10, at=now - 60)
        document = (await self.client.post('/api/v1/backup')).json()
        self.service.insights = AsyncMock(return_value={'view': 'tokens', 'profile': {'quota': 120, 'used_quota': 15},
                                                       'items': [], 'fetched_at': now - 1})
        response = await self.client.post(f'/api/v1/accounts/{identifier}/insights', json={'view': 'tokens'})
        self.assertEqual(response.status_code, 200)
        await self.engine.process(self.engine.claim())
        response = await self.client.post('/api/v1/backup/restore', json=document)
        self.assertEqual(response.status_code, 200, response.text)
        restored = self.store.account(identifier)
        self.assertEqual(restored['quota'], 120)
        self.assertTrue(confirmed_today(self.store.daily_state(restored), '42', time.time()))
        self.assertIsNone(restored['last_checkin'])

    async def test_yesterday_confirmation_cannot_confirm_today_backup(self):
        identifier = account(self.store)
        now = time.time()
        self.store.observe_account(identifier, '42', 125, 10, at=now - 86400, confirm=True)
        document = (await self.client.post('/api/v1/backup')).json()
        document['accounts'][0]['daily_checkin'] = observe_balance(None, '42', 125, 10, now - 1).model_dump()
        response = await self.client.post('/api/v1/backup/restore', json=document)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(confirmed_today(self.store.daily_state(self.store.account(identifier)), '42', time.time()))


class CheckinPreflightTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ExistingFixture.asyncSetUp
    asyncTearDown = ExistingFixture.asyncTearDown
    perform = ExistingFixture.perform

    def configure_router(self, before, after):
        self.requests = []
        self.profiles = 0

        async def upstream_api(path, method='GET', **options):
            self.requests.append((path, method))
            if path == CHECKIN_PATH:
                return 200, json.dumps({'success': True})
            profile = before if self.profiles == 0 else after
            self.profiles += 1
            return 200, json.dumps({'success': True, 'data': {'id': 42, **profile}})

        @asynccontextmanager
        async def signed_in(*arguments):
            yield SimpleNamespace(api=upstream_api)

        self.engine.service = RouterService()
        self.engine.service.signed_in = signed_in

    async def test_fresh_preflight_credit_skips_post_without_second_browser(self):
        identifier = account(self.store)
        self.store.observe_account(identifier, '42', 100, 10, at=time.time() - 2)
        profile = {'quota': 62500000, 'used_quota': 5000000}
        self.configure_router(profile, profile)
        with patch('app.router_service.asyncio.sleep', new=AsyncMock()):
            job = await self.perform(identifier)
        self.assertEqual(job['status'], 'already_signed')
        self.assertEqual(self.requests.count((CHECKIN_PATH, 'POST')), 0)
        self.assertEqual(self.profiles, 1)
        self.assertTrue(confirmed_today(self.store.daily_state(self.store.account(identifier)), '42', time.time()))

    async def test_non_25_transfer_does_not_skip_real_checkin(self):
        identifier = account(self.store)
        self.store.observe_account(identifier, '42', 100, 10, at=time.time() - 2)
        self.configure_router({'quota': 100000000, 'used_quota': 5000000}, {'quota': 112500000, 'used_quota': 5000000})
        job = await self.perform(identifier)
        self.assertEqual(job['status'], 'signed')
        self.assertEqual(self.requests.count((CHECKIN_PATH, 'POST')), 1)
        self.assertEqual(self.store.checkin_balance(job_id=job['id'])['earned'], 25)

    async def test_first_balance_read_does_not_skip_checkin(self):
        identifier = account(self.store)
        self.configure_router({'quota': 62500000, 'used_quota': 5000000}, {'quota': 75000000, 'used_quota': 5000000})
        job = await self.perform(identifier)
        self.assertEqual(job['status'], 'signed')
        self.assertEqual(self.requests.count((CHECKIN_PATH, 'POST')), 1)


class BeijingStatisticsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ExistingFixture.asyncSetUp
    asyncTearDown = ExistingFixture.asyncTearDown

    async def test_statistics_use_beijing_day_not_display_timezone(self):
        identifier = account(self.store)
        moment = datetime(2026, 9, 15, 23, 30, tzinfo=BEIJING)
        midnight = moment.replace(hour=0, minute=0).timestamp()
        for created in (midnight - 1, midnight + 1, moment.timestamp() - 1, moment.timestamp() + 1):
            self.store.execute("INSERT INTO checkins(account_id,code,quota_before,quota_after,reward_amount,created,balance_source) VALUES (?,'signed',100,125,25,?,'live')",
                               (identifier, created))
        for timezone in ('Asia/Seoul', 'UTC', 'America/Los_Angeles'):
            with self.subTest(timezone=timezone):
                self.store.set_meta('settings', self.store.settings().model_copy(update={'timezone': timezone}).model_dump_json())
                with patch('app.statistics.datetime') as clock:
                    clock.now.side_effect = lambda zone: moment.astimezone(zone)
                    clock.fromtimestamp.side_effect = datetime.fromtimestamp
                    response = await self.client.get('/api/v1/stats')
                    self.assertEqual(response.status_code, 200, response.text)
                    stats = response.json()
                self.assertEqual((stats['timezone'], stats['attempts'], stats['earned']), ('Asia/Shanghai', 2, 50))
                self.assertEqual(stats['since'], midnight)
                self.assertEqual(stats['series'][0]['day'], '09-15')


if __name__ == '__main__':
    unittest.main(defaultTest=['BalanceRecoveryTests', 'StateMergeTests', 'RestoreSafetyTests', 'CheckinPreflightTests', 'BeijingStatisticsTests'], verbosity=2)
