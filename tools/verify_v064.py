"""Regression checks for receipt-only success and Beijing-day status."""
import json
import sys
import time
import unittest
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.checkin_state import BEIJING, confirmed_today
from app.errors import TaskError
from app.router_service import CHECKIN_PATH, RouterService
from app.schemas import RuntimeSettings
from tools.verify_v033 import ReceiptTests
from tools.verify_v3 import ApiTests, account


class VerifiedReceiptTests(unittest.IsolatedAsyncioTestCase):
    run_checkin = ReceiptTests.run_checkin

    async def test_success_text_without_credit_is_uncertain(self):
        result = await self.run_checkin({'success': True, 'message': '签到成功，获得 $25 额度'},
                                       {'quota': 50000000, 'used_quota': 1000000},
                                       [{'quota': 50000000, 'used_quota': 1000000}], expected_posts=3)
        self.assertEqual(result['code'], 'no_credit')
        self.assertIsNone(result['reward_amount'])
        self.assertEqual(self.profiles, 12)

    async def test_success_text_waits_for_delayed_credit(self):
        result = await self.run_checkin({'success': True, 'message': '签到成功'},
                                       {'quota': 50000000, 'used_quota': 1000000},
                                       [{'quota': 50000000, 'used_quota': 1000000},
                                        {'quota': 62500000, 'used_quota': 1000000}])
        self.assertEqual((result['code'], result['reward_amount']), ('signed', 25))
        self.assertEqual(self.profiles, 3)

    async def test_transient_post_balance_failure_only_retries_get(self):
        result = await self.run_checkin({'success': True, 'message': '签到成功'},
                                       {'quota': 50000000, 'used_quota': 0},
                                       [TaskError('network_error', 'temporary'),
                                        {'quota': 62500000, 'used_quota': 0}])
        self.assertEqual(result['code'], 'signed')
        self.assertEqual(self.requests, [('/api/user/self', 'GET'), (CHECKIN_PATH, 'POST'),
                                         ('/api/user/self', 'GET'), ('/api/user/self', 'GET')])

    async def test_concurrent_consumption_can_exceed_reward(self):
        result = await self.run_checkin({'success': True, 'message': '签到成功'},
                                       {'quota': 50000000, 'used_quota': 5000000},
                                       [{'quota': 45000000, 'used_quota': 22500000}])
        self.assertEqual((result['code'], result['verified_gain']), ('signed', 25))
        self.assertLess(result['quota_after'], result['quota_before'])

    async def test_usage_without_credit_is_not_a_reward(self):
        result = await self.run_checkin({'success': True, 'message': '签到成功'},
                                       {'quota': 50000000, 'used_quota': 0},
                                       [{'quota': 37500000, 'used_quota': 12500000}], expected_posts=3)
        self.assertEqual((result['code'], result['verified_gain']), ('no_credit', 0))

    async def test_explicit_already_signed_does_not_count_new_reward(self):
        result = await self.run_checkin({'success': False, 'message': '今天已经签到'},
                                       {'quota': 50000000, 'used_quota': 0},
                                       [{'quota': 50000000, 'used_quota': 0}])
        self.assertEqual(result['code'], 'no_credit')
        self.assertIsNone(result['reward_amount'])

    async def test_missing_before_balance_never_sends_post(self):
        api = AsyncMock(return_value=(200, json.dumps({'success': True, 'data': {'id': 42}})))
        @asynccontextmanager
        async def signed_in(*args):
            yield SimpleNamespace(api=api)
        service = RouterService()
        service.signed_in = signed_in
        with self.assertRaises(TaskError) as caught:
            await service.checkin({'session': 'synthetic', 'api_user': '42'}, None, RuntimeSettings())
        self.assertEqual(caught.exception.code, 'upstream_error')
        api.assert_awaited_once()
        self.assertEqual(api.call_args.args, ('/api/user/self',))

    async def test_credit_crossing_beijing_midnight_is_not_counted(self):
        midnight = datetime(2026, 10, 18, tzinfo=BEIJING).timestamp()
        with patch('app.router_service.time.time', side_effect=[midnight - 2, midnight - 1, midnight + 1, midnight + 1]):
            result = await self.run_checkin({'success': True, 'message': '签到成功'},
                                           {'quota': 50000000}, [{'quota': 62500000}])
        self.assertEqual(result['code'], 'uncertain')
        self.assertIsNone(result['reward_amount'])


class DailyStatusTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ApiTests.asyncSetUp
    asyncTearDown = ApiTests.asyncTearDown
    perform = ApiTests.perform

    async def test_yesterday_success_does_not_show_as_today(self):
        identifier = account(self.store)
        midnight = datetime(2026, 10, 18, tzinfo=BEIJING).timestamp()
        self.store.observe_account(identifier, '42', 125, 0, at=midnight - 60, confirm=True)
        self.store.execute("UPDATE accounts SET checkin_status='signed',last_checkin=? WHERE id=?", (midnight - 60, identifier))
        with patch('app.db.time.time', return_value=midnight):
            public = self.store.public_account(self.store.account(identifier))
        self.assertEqual((public['checkin_day'], public['today_checkin']), ('2026-10-18', 'unconfirmed'))
        self.assertEqual(public['last_signed_at'], midnight - 60)

    async def test_attempt_timestamp_is_not_confirmation(self):
        identifier = account(self.store)
        self.store.execute("UPDATE accounts SET checkin_status='uncertain',last_checkin=? WHERE id=?", (time.time(), identifier))
        self.store.mark_submission(identifier, '42')
        public = self.store.public_account(self.store.account(identifier))
        self.assertEqual(public['today_checkin'], 'uncertain')
        self.assertIsNone(public['last_signed_at'])

    async def test_dashboard_and_accounts_share_today_status(self):
        identifier = account(self.store)
        now = time.time()
        self.store.observe_account(identifier, '42', 125, 0, at=now - 86400, confirm=True)
        self.store.record_checkin(identifier, None, 'signed', 100, 125, used_before=0, used_after=0, reward_amount=25)
        self.store.execute('UPDATE checkins SET created=? WHERE account_id=?', (now - 86400, identifier))
        stats = (await self.client.get('/api/v1/stats')).json()
        row = next(item for item in stats['accounts'] if item['account_id'] == identifier)
        self.assertEqual(row['last_code'], 'signed')
        self.assertEqual(row['today_checkin'], 'unconfirmed')
        self.assertEqual(row['checkin_day'], self.store.public_account(self.store.account(identifier))['checkin_day'])
        self.assertNotIn('result_enc', row)
        self.store.observe_account(identifier, '42', 125, 0, at=now)
        self.store.observe_account(identifier, '42', 150, 0, at=now + 0.001)
        stats = (await self.client.get('/api/v1/stats')).json()
        self.assertEqual(stats['accounts'][0]['today_checkin'], 'confirmed')

    async def test_spending_preserves_today_confirmation(self):
        identifier = account(self.store)
        now = time.time()
        self.store.observe_account(identifier, '42', 100, 0, at=now - 2)
        self.store.observe_account(identifier, '42', 125, 0, at=now - 1)
        self.store.observe_account(identifier, '42', 75, 50, at=now)
        public = self.store.public_account(self.store.account(identifier))
        self.assertEqual(public['today_checkin'], 'confirmed')
        self.assertEqual(public['last_signed_at'], now - 1)

    async def test_engine_rejects_receipt_only_success_and_reward(self):
        identifier = account(self.store)
        self.service.result = {'code': 'signed', 'receipt_code': 'signed', 'message': '签到成功',
                               'quota': 100, 'quota_before': 100, 'quota_after': 100,
                               'used_before': 0, 'used_after': 0, 'reward_amount': 25, 'logs': []}
        job = await self.perform(identifier)
        self.assertEqual(job['status'], 'uncertain')
        self.assertIsNone(self.store.checkin_balance(job_id=job['id'])['earned'])
        self.assertFalse(confirmed_today(self.store.daily_state(self.store.account(identifier)), '42', time.time()))

    async def test_fabricated_fixed_reward_history_does_not_confirm(self):
        identifier = account(self.store)
        self.store.execute('UPDATE accounts SET last_extracted=? WHERE id=?', (time.time() - 60, identifier))
        self.store.record_checkin(identifier, None, 'signed', 100, 100, used_before=0, used_after=0, reward_amount=25)
        result = self.engine.resolve_checkin_receipt(self.store.account(identifier),
                    {'code': 'uncertain', 'receipt_code': 'accepted', 'verified_gain': 0,
                     'quota': 100, 'quota_before': 100, 'quota_after': 100, 'used_before': 0, 'used_after': 0,
                     'message': '未到账', 'logs': []}, self.store.settings())
        self.assertEqual(result['code'], 'uncertain')


if __name__ == '__main__':
    unittest.main(defaultTest=['VerifiedReceiptTests', 'DailyStatusTests'], verbosity=2)
