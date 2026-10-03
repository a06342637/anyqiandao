import json
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.checkin_state import BEIJING, balance_credit, beijing_day, confirmed_today, fixed_reward, observe_balance
from app.crypto import Vault
from app.db import SCHEMA_VERSION, Store
from tools.backup_fixture import export_document
from tools.verify_v3 import ApiTests as ExistingFixture, account
from tools.verify_v033 import ReceiptTests as ReceiptFixture


class DailyStateTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 15, 10, tzinfo=BEIJING).timestamp()

    def baseline(self, quota=100, used=10):
        return observe_balance(None, '42', quota, used, self.now)

    def test_first_read_has_no_invented_midnight_balance(self):
        state = self.baseline()
        self.assertIsNone(state.confirmed_at)
        self.assertFalse(confirmed_today(state, '42', self.now))

    def test_exact_25_with_usage_confirms_even_if_balance_falls(self):
        state = observe_balance(self.baseline(), '42', 95, 40, self.now + 1)
        self.assertTrue(confirmed_today(state, '42', self.now + 1))
        self.assertEqual(state.evidence, 'balance_25')

    def test_consumption_and_unchanged_balance_never_invent_reward(self):
        for quota, used in ((100, 10), (90, 20), (70, 40)):
            with self.subTest(quota=quota):
                state = observe_balance(self.baseline(), '42', quota, used, self.now + 1)
                self.assertIsNone(state.confirmed_at)

    def test_consumption_preserves_confirmed_day_and_time(self):
        signed = observe_balance(self.baseline(), '42', 120, 15, self.now + 1)
        consumed = observe_balance(signed, '42', 0, 135, self.now + 2)
        self.assertEqual(consumed.confirmed_at, signed.confirmed_at)
        self.assertEqual(consumed.last_signed_at, signed.last_signed_at)
        self.assertTrue(confirmed_today(consumed, '42', self.now + 2))

    def test_non_25_transfers_are_not_signin(self):
        for gain in (0.01, 5, 24, 26, 50, 100, 125):
            with self.subTest(gain=gain):
                state = observe_balance(self.baseline(), '42', 100 + gain, 10, self.now + 1)
                self.assertFalse(confirmed_today(state, '42', self.now + 1))

    def test_reward_after_separate_transfer_is_recognized(self):
        transferred = observe_balance(self.baseline(), '42', 200, 10, self.now + 1)
        signed = observe_balance(transferred, '42', 220, 15, self.now + 2)
        self.assertTrue(confirmed_today(signed, '42', self.now + 2))

    def test_beijing_midnight_not_seoul_midnight_resets_day(self):
        before_midnight = datetime(2026, 9, 15, 23, 30, tzinfo=BEIJING).timestamp()
        signed = observe_balance(None, '42', 125, 10, before_midnight, confirm=True)
        self.assertTrue(confirmed_today(signed, '42', before_midnight + 60))
        midnight = datetime(2026, 9, 16, tzinfo=BEIJING).timestamp()
        self.assertFalse(confirmed_today(signed, '42', midnight))
        fresh = observe_balance(signed, '42', 150, 10, midnight + 1)
        self.assertIsNone(fresh.confirmed_at)
        self.assertEqual(fresh.last_signed_at, signed.last_signed_at)

    def test_identity_and_future_evidence_are_never_reused(self):
        signed = observe_balance(self.baseline(), '42', 125, 10, self.now + 1)
        self.assertFalse(confirmed_today(signed, '99', self.now + 2))
        self.assertFalse(confirmed_today(signed, '42', self.now))
        changed = observe_balance(signed, '99', 150, 10, self.now + 2)
        self.assertIsNone(changed.confirmed_at)
        self.assertIsNone(changed.last_signed_at)

    def test_out_of_order_observation_cannot_overwrite_newer_state(self):
        baseline = self.baseline()
        stale = observe_balance(baseline, '42', 125, 10, self.now - 1)
        self.assertEqual(stale, baseline)

    def test_used_counter_reset_and_partial_read_are_not_rewards(self):
        for quota, used in ((125, 0), (125, None), (None, 10)):
            with self.subTest(quota=quota, used=used):
                state = observe_balance(self.baseline(), '42', quota, used, self.now + 1)
                self.assertIsNone(state.confirmed_at)

    def test_float_tolerance_and_invalid_values(self):
        self.assertTrue(fixed_reward(25.00000001))
        self.assertTrue(fixed_reward(24.99999999))
        for value in (True, None, float('nan'), float('inf'), 10 ** 500, 25.001):
            self.assertFalse(fixed_reward(value))
        self.assertEqual(balance_credit({'quota': 100, 'used_quota': 10}, {'quota': 75, 'used_quota': 60}), 25)


class DailyApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ExistingFixture.asyncSetUp
    asyncTearDown = ExistingFixture.asyncTearDown
    perform = ExistingFixture.perform

    def seed_confirmation(self, identifier):
        return self.store.observe_account(identifier, '42', 125, 10, at=time.time() - 1, confirm=True)

    def receipt(self, **updates):
        return {'code': 'uncertain', 'receipt_code': 'accepted', 'verified_gain': 0,
                'message': '合成空回执', 'quota': 125, 'quota_before': 125, 'quota_after': 125,
                'used_before': 10, 'used_after': 10, 'logs': [], **updates}

    async def test_readonly_tokens_update_balance_without_checkin_status_or_time(self):
        identifier = account(self.store)
        checked_at = time.time() - 100
        self.store.execute("UPDATE accounts SET checkin_status='uncertain',last_checkin=? WHERE id=?", (checked_at, identifier))
        self.store.observe_account(identifier, '42', 100, 10, at=time.time() - 2)
        self.service.insights = AsyncMock(return_value={'view': 'tokens', 'profile': {'quota': 120, 'used_quota': 15},
                                                       'items': [], 'fetched_at': time.time()})
        response = await self.client.post(f'/api/v1/accounts/{identifier}/insights', json={'view': 'tokens'})
        self.assertEqual(response.status_code, 200)
        await self.engine.process(self.engine.claim())
        current = self.store.account(identifier)
        self.assertEqual((current['checkin_status'], current['last_checkin'], current['quota']), ('uncertain', checked_at, 120))
        self.assertEqual((self.service.checkins, self.service.extracts), (0, 0))
        self.assertEqual(self.store.one('SELECT COUNT(*) AS count FROM checkins')['count'], 0)
        self.assertTrue(confirmed_today(self.store.daily_state(current), '42', time.time()))

    async def test_confirmed_day_only_refreshes_and_does_not_submit_checkin(self):
        identifier = account(self.store)
        signed = self.seed_confirmation(identifier)
        self.service.validate = AsyncMock(return_value={'quota': 110, 'profile': {'used_quota': 12500000}})
        job = await self.perform(identifier)
        self.assertEqual(job['status'], 'already_signed')
        self.assertIn('未重复提交', job['message'])
        self.assertEqual(self.service.checkins, 0)
        self.assertEqual(self.store.daily_state(self.store.account(identifier)).confirmed_at, signed.confirmed_at)
        self.assertIsNone(self.store.checkin_balance(job_id=job['id'])['earned'])

    async def test_evidence_survives_manual_history_delete_and_restart(self):
        identifier = account(self.store)
        self.seed_confirmation(identifier)
        self.store.record_checkin(identifier, None, 'signed', 100, 125, reward_amount=25)
        response = await self.client.post('/api/v1/checkins/delete', json={'all_matching': True})
        self.assertEqual(response.json()['removed'], 1)
        reopened = Store(self.config.data_dir, Vault(self.config.key))
        self.assertTrue(confirmed_today(reopened.daily_state(reopened.account(identifier)), '42', time.time()))
        self.assertEqual((await self.perform(identifier))['status'], 'already_signed')
        self.assertEqual(self.service.checkins, 0)

    async def test_retention_does_not_remove_daily_evidence(self):
        identifier = account(self.store)
        self.seed_confirmation(identifier)
        self.store.record_checkin(identifier, None, 'signed', 100, 125, reward_amount=25)
        self.store.execute('UPDATE checkins SET created=?', (time.time() - 100 * 86400,))
        self.assertEqual(self.store.cleanup()['checkins'], 1)
        self.assertTrue(confirmed_today(self.store.daily_state(self.store.account(identifier)), '42', time.time()))

    async def test_backup_restores_daily_evidence_and_fixed_reward(self):
        identifier = account(self.store)
        self.seed_confirmation(identifier)
        self.store.record_checkin(identifier, None, 'signed', 100, 200, reward_amount=25)
        document = await export_document(self.client)
        self.assertEqual(document['checkins'][0]['reward_amount'], 25)
        self.assertEqual(document['accounts'][0]['daily_checkin']['api_user'], '42')
        self.store.execute('UPDATE accounts SET checkin_state_enc=NULL')
        self.store.execute('DELETE FROM checkins')
        response = await self.client.post('/api/v1/backup/restore', json=document)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(confirmed_today(self.store.daily_state(self.store.account(identifier)), '42', time.time()))
        self.assertEqual((await self.client.get('/api/v1/stats')).json()['earned'], 25)

    async def test_backup_cannot_mix_other_identity_evidence(self):
        identifier = account(self.store)
        self.seed_confirmation(identifier)
        document = await export_document(self.client)
        document['accounts'][0]['daily_checkin']['api_user'] = '99'
        self.store.execute('UPDATE accounts SET checkin_state_enc=NULL')
        response = await self.client.post('/api/v1/backup/restore', json=document)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.store.daily_state(self.store.account(identifier)))

    async def test_same_identity_session_refresh_preserves_day(self):
        identifier = account(self.store, password=None)
        self.seed_confirmation(identifier)
        response = await self.client.put(f'/api/v1/accounts/{identifier}/credentials', json={'session': 'new-synthetic-session', 'api_user': '42'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(confirmed_today(self.store.daily_state(self.store.account(identifier)), '42', time.time()))

    async def test_login_identity_change_clears_evidence(self):
        identifier = account(self.store)
        self.seed_confirmation(identifier)
        response = await self.client.put(f'/api/v1/accounts/{identifier}', json={'username': 'new-synthetic-identity'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self.store.account(identifier)['checkin_state_enc'])

    async def test_different_api_user_cannot_reuse_evidence(self):
        identifier = account(self.store)
        self.seed_confirmation(identifier)
        self.store.execute('UPDATE accounts SET result_enc=? WHERE id=?',
                           (self.store.vault.seal({'session': 'synthetic-other', 'api_user': '99'}, f'result:{identifier}'), identifier))
        self.assertIsNone(self.store.daily_state(self.store.account(identifier)))

    async def test_first_unknown_empty_response_remains_unconfirmed_in_logs(self):
        identifier = account(self.store)
        self.service.result = self.receipt()
        job = await self.perform(identifier)
        self.assertEqual(job['status'], 'uncertain')
        self.assertEqual((await self.client.get('/api/v1/stats')).json()['earned'], 0)
        self.assertFalse(confirmed_today(self.store.daily_state(self.store.account(identifier)), '42', time.time()))

    async def test_direct_25_credit_persists_independent_evidence(self):
        identifier = account(self.store)
        self.service.result = self.receipt(code='signed', verified_gain=25, quota_before=100, reward_amount=25)
        job = await self.perform(identifier)
        self.assertEqual(job['status'], 'signed')
        self.assertEqual(self.store.checkin_balance(job_id=job['id'])['earned'], 25)
        self.assertTrue(confirmed_today(self.store.daily_state(self.store.account(identifier)), '42', time.time()))

    async def test_explicit_receipt_with_invite_transfer_counts_only_25(self):
        identifier = account(self.store)
        self.service.result = self.receipt(code='signed', receipt_code='signed', verified_gain=100,
                                          quota_before=100, quota_after=200, quota=200, reward_amount=25)
        job = await self.perform(identifier)
        self.assertEqual(self.store.checkin_balance(job_id=job['id'])['delta'], 100)
        self.assertEqual((await self.client.get('/api/v1/stats')).json()['earned'], 25)

    async def test_pre_request_daily_increase_resolves_empty_response(self):
        identifier = account(self.store)
        self.store.observe_account(identifier, '42', 100, 10, at=time.time() - 1)
        self.service.result = self.receipt()
        self.assertEqual((await self.perform(identifier))['status'], 'already_signed')
        self.assertEqual((await self.client.get('/api/v1/stats')).json()['earned'], 0)

    async def test_today_evidence_survives_other_credit_during_receipt(self):
        identifier = account(self.store)
        signed = self.seed_confirmation(identifier)
        result = self.engine.resolve_checkin_receipt(self.store.account(identifier),
                    self.receipt(verified_gain=100, quota_after=225, quota=225), self.store.settings())
        self.assertEqual(result['code'], 'already_signed')
        self.assertIsNone(result.get('reward_amount'))
        self.assertEqual(self.store.daily_state(self.store.account(identifier)).confirmed_at, signed.confirmed_at)

    async def test_today_evidence_survives_missing_post_request_balance(self):
        identifier = account(self.store)
        self.seed_confirmation(identifier)
        result = self.engine.resolve_checkin_receipt(self.store.account(identifier),
                    self.receipt(verified_gain=None, quota_after=None, used_after=None), self.store.settings())
        self.assertEqual(result['code'], 'already_signed')
        self.assertIsNone(result.get('reward_amount'))

    async def test_previous_day_receipt_does_not_borrow_today_confirmation(self):
        identifier = account(self.store)
        midnight = datetime(2026, 9, 15, tzinfo=BEIJING).timestamp()
        self.store.observe_account(identifier, '42', 125, 10, at=midnight + 1, confirm=True)
        with patch('app.engine.time.time', return_value=midnight + 3):
            result = self.engine.resolve_checkin_receipt(self.store.account(identifier),
                        self.receipt(claimed_at=midnight - 1, observed_before=midnight - 2, observed_after=midnight + 2),
                        self.store.settings())
        self.assertEqual(result['code'], 'uncertain')

    async def test_beijing_legacy_evidence_ignores_workspace_timezone(self):
        identifier = account(self.store)
        self.store.set_meta('settings', self.store.settings().model_copy(update={'timezone': 'Asia/Seoul'}).model_dump_json())
        now = datetime(2026, 9, 14, 23, 30, tzinfo=BEIJING).timestamp()
        self.store.execute('UPDATE accounts SET last_extracted=? WHERE id=?', (now - 3600, identifier))
        self.store.record_checkin(identifier, None, 'signed', 100, 125)
        self.store.execute('UPDATE checkins SET created=?', (now - 1800,))
        with patch('app.engine.time.time', return_value=now):
            result = self.engine.resolve_checkin_receipt(self.store.account(identifier), self.receipt(), self.store.settings())
        self.assertEqual(result['code'], 'already_signed')

    async def test_receipt_crossing_midnight_does_not_confirm_new_day(self):
        identifier = account(self.store)
        midnight = datetime(2026, 9, 15, tzinfo=BEIJING).timestamp()
        result = self.receipt(code='signed', verified_gain=25, quota_before=100, reward_amount=25,
                              observed_before=midnight - 2, observed_after=midnight + 2, claimed_at=midnight - 1)
        with patch('app.engine.time.time', return_value=midnight + 2):
            self.engine.resolve_checkin_receipt(self.store.account(identifier), result, self.store.settings())
        state = self.store.daily_state(self.store.account(identifier))
        self.assertEqual(state.day, beijing_day(midnight))
        self.assertFalse(confirmed_today(state, '42', midnight + 2))

    async def test_schema_seven_upgrade_keeps_old_credentials_and_rows(self):
        identifier = account(self.store)
        self.store.record_checkin(identifier, None, 'signed', 100, 125)
        original = self.store.account(identifier)
        self.store.execute('ALTER TABLE accounts DROP COLUMN checkin_state_enc')
        self.store.execute('ALTER TABLE checkins DROP COLUMN reward_amount')
        self.store.execute('PRAGMA user_version=7')
        migrated = Store(self.config.data_dir, Vault(self.config.key))
        self.assertEqual(migrated.one('PRAGMA user_version')['user_version'], SCHEMA_VERSION)
        self.assertEqual(migrated.account(identifier)['result_enc'], original['result_enc'])
        self.assertEqual(migrated.account(identifier)['login_enc'], original['login_enc'])
        self.assertEqual(migrated.checkin_balance(account_id=identifier)['earned'], 25)


class FixedReceiptTests(unittest.IsolatedAsyncioTestCase):
    run_checkin = ReceiptFixture.run_checkin

    async def test_partial_usage_counter_never_fabricates_fixed_reward(self):
        for before, after in (({'quota': 50000000, 'used_quota': 5000000}, {'quota': 62500000}),
                              ({'quota': 50000000}, {'quota': 62500000, 'used_quota': 5000000})):
            with self.subTest(before=before):
                result = await self.run_checkin({'success': True}, before, [after])
                self.assertEqual(result['code'], 'uncertain')
                self.assertIsNone(result['reward_amount'])

    async def test_invite_like_increases_do_not_prove_fixed_reward(self):
        for amount in (5, 24, 26, 50, 100):
            with self.subTest(amount=amount):
                result = await self.run_checkin({'success': True}, {'quota': 50000000, 'used_quota': 0},
                                               [{'quota': (100 + amount) * 500000, 'used_quota': 0}])
                self.assertEqual(result['code'], 'uncertain')
                self.assertIsNone(result['reward_amount'])

    async def test_fixed_reward_is_recorded_with_simultaneous_consumption(self):
        result = await self.run_checkin({'success': True}, {'quota': 50000000, 'used_quota': 0},
                                       [{'quota': 47500000, 'used_quota': 15000000}])
        self.assertEqual((result['code'], result['reward_amount']), ('signed', 25))

    async def test_explicit_receipt_does_not_count_entire_transfer(self):
        result = await self.run_checkin({'success': True, 'message': '签到成功'}, {'quota': 50000000}, [{'quota': 100000000}])
        self.assertEqual(result['reward_amount'], 25)
        self.assertLessEqual(result['observed_before'], result['observed_after'])


if __name__ == '__main__':
    unittest.main(defaultTest=['DailyStateTests', 'DailyApiTests', 'FixedReceiptTests'], verbosity=2)
