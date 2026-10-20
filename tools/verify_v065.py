"""Three-attempt check-in, manual acknowledgement and filtered selection."""
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.checkin_state import BEIJING, beijing_day, confirmed_today, submitted_today
from app.errors import TaskError
from app.crypto import Vault
from app.db import Store
from tools.backup_fixture import export_document
from tools.verify_v033 import ReceiptTests
from tools.verify_v3 import ApiTests, account


class ThreeAttemptTests(unittest.IsolatedAsyncioTestCase):
    run_checkin = ReceiptTests.run_checkin

    async def test_second_attempt_can_receive_reward(self):
        before = {'quota': 50000000, 'used_quota': 0}
        result = await self.run_checkin({'success': True}, before,
                                       [before] * 4 + [{'quota': 62500000, 'used_quota': 0}], expected_posts=2)
        self.assertEqual((result['code'], result['reward_amount'], result['attempts']), ('signed', 25, 2))

    async def test_third_attempt_can_receive_reward(self):
        before = {'quota': 50000000, 'used_quota': 0}
        result = await self.run_checkin({'success': True}, before,
                                       [before] * 8 + [{'quota': 62500000, 'used_quota': 0}], expected_posts=3)
        self.assertEqual((result['code'], result['reward_amount'], result['attempts']), ('signed', 25, 3))

    async def test_three_zero_credits_stop_without_success(self):
        before = {'quota': 50000000, 'used_quota': 0}
        result = await self.run_checkin({'success': True}, before, [before], expected_posts=3)
        self.assertEqual((result['code'], result['attempts']), ('no_credit', 3))
        self.assertIsNone(result['reward_amount'])
        self.assertEqual(self.profiles, 12)

    async def test_delayed_credit_before_retry_prevents_another_post(self):
        before = {'quota': 50000000, 'used_quota': 0}
        result = await self.run_checkin({'success': True}, before,
                                       [before] * 3 + [{'quota': 62500000, 'used_quota': 0}])
        self.assertEqual((result['code'], result['attempts']), ('signed', 1))

    async def test_incomplete_post_balance_does_not_repeat(self):
        before = {'quota': 50000000, 'used_quota': 0}
        result = await self.run_checkin({'success': True}, before, [TaskError('network_error', 'synthetic')])
        self.assertEqual((result['code'], result['attempts']), ('uncertain', 1))

    async def test_missing_retry_baseline_stops(self):
        before = {'quota': 50000000, 'used_quota': 0}
        result = await self.run_checkin({'success': True}, before, [before] * 3 + [TaskError('network_error', 'synthetic')])
        self.assertEqual((result['code'], result['attempts']), ('uncertain', 1))


class ConfirmationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ApiTests.asyncSetUp
    asyncTearDown = ApiTests.asyncTearDown
    perform = ApiTests.perform

    def pending(self, name='pending', at=None):
        identifier = account(self.store, name)
        now = time.time() if at is None else at
        self.store.observe_account(identifier, '42', 100, 0, at=now - 1)
        if at is None:
            self.store.mark_submission(identifier, '42')
        self.store.execute("UPDATE accounts SET checkin_status='uncertain',last_checkin=? WHERE id=?", (now, identifier))
        self.store.record_checkin(identifier, None, 'uncertain', 100, 100, used_before=0, used_after=0)
        self.store.execute('UPDATE checkins SET created=? WHERE account_id=?', (now, identifier))
        return identifier

    async def confirm(self, identifier, day=None):
        return await self.client.post(f'/api/v1/accounts/{identifier}/checkin/confirm',
                                      json={'day': day or beijing_day(time.time())})

    async def test_confirm_clears_pending_without_inventing_reward(self):
        identifier = self.pending()
        before = self.store.account(identifier)
        response = await self.confirm(identifier)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['resolved'], 1)
        row = self.store.account(identifier)
        public = self.store.public_account(row)
        self.assertEqual(public['today_checkin'], 'confirmed')
        self.assertTrue(public['manual_confirmation'])
        self.assertEqual(row['quota'], before['quota'])
        self.assertEqual(row['last_checkin'], before['last_checkin'])
        stats = (await self.client.get('/api/v1/stats')).json()
        self.assertEqual((stats['signed'], stats['uncertain'], stats['manual'], stats['failed'], stats['earned']), (0, 0, 1, 0, 0))
        self.assertEqual(self.store.checkin_balance(account_id=identifier)['code'], 'manual_confirmed')
        self.assertIsNone(self.store.checkin_balance(account_id=identifier)['earned'])

    async def test_confirmation_is_idempotent(self):
        identifier = self.pending()
        self.assertEqual((await self.confirm(identifier)).status_code, 200)
        self.assertEqual((await self.confirm(identifier)).json()['resolved'], 0)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM logs WHERE message LIKE '管理员已确认%' ")['n'], 1)

    async def test_active_job_blocks_confirmation(self):
        identifier = self.pending()
        self.store.enqueue('checkin', identifier)
        self.assertEqual((await self.confirm(identifier)).status_code, 409)
        self.assertEqual(self.store.public_account(self.store.account(identifier))['today_checkin'], 'uncertain')

    async def test_stale_date_cannot_acknowledge_new_day(self):
        identifier = self.pending()
        self.assertEqual((await self.confirm(identifier, beijing_day(time.time() - 86400))).status_code, 409)

    async def test_unattempted_account_cannot_be_marked_signed(self):
        identifier = account(self.store)
        self.assertEqual((await self.confirm(identifier)).status_code, 409)

    async def test_manual_confirmation_does_not_skip_later_website_request(self):
        identifier = self.pending()
        await self.confirm(identifier)
        self.assertEqual((await self.perform(identifier))['status'], 'signed')
        self.assertEqual(self.service.checkins, 1)

    async def test_manual_evidence_survives_backup_and_restart(self):
        identifier = self.pending()
        await self.confirm(identifier)
        backup = await export_document(self.client)
        self.assertEqual(backup['accounts'][0]['daily_checkin']['evidence'], 'manual')
        reopened = Store(self.config.data_dir, Vault(self.config.key))
        self.assertTrue(confirmed_today(reopened.daily_state(reopened.account(identifier)), '42', time.time()))
        self.assertEqual((await self.client.post('/api/v1/backup/restore', json=backup)).status_code, 200)

    async def test_filter_and_cross_page_actions_only_select_pending_today(self):
        selected = [self.pending('pending-' + str(n)) for n in range(3)]
        self.pending('yesterday', time.time() - 86400)
        account(self.store, 'unattempted')
        await self.confirm(selected[0])
        response = await self.client.get('/api/v1/accounts?needs_confirmation=true&limit=1&page=2')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['total'], 2)
        self.assertEqual(response.json()['items'][0]['today_checkin'], 'uncertain')
        response = await self.client.post('/api/v1/accounts/actions', json={'all_matching': True,
                   'needs_confirmation': True, 'exclude_ids': [selected[1]], 'action': 'checkin', 'retry_unconfirmed': True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['queued'], 1)
        self.assertEqual(self.store.one("SELECT account_id FROM jobs WHERE status='pending'")['account_id'], selected[2])

    async def test_explicit_manual_retry_releases_marker_and_runs(self):
        identifier = self.pending()
        response = await self.client.post('/api/v1/accounts/actions', json={'ids': [identifier], 'action': 'checkin', 'retry_unconfirmed': True})
        self.assertEqual(response.status_code, 200)
        await self.engine.process(self.engine.claim())
        self.assertEqual(self.service.checkins, 1)


if __name__ == '__main__':
    unittest.main(defaultTest=['ThreeAttemptTests', 'ConfirmationTests'], verbosity=2)
