"""Check every task against the website, never substitute calendar-day history."""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.checkin_state import beijing_day, submitted_today
from tools.verify_v3 import ApiTests, account


class WebsiteEligibilityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ApiTests.asyncSetUp
    asyncTearDown = ApiTests.asyncTearDown
    perform = ApiTests.perform

    def receipt(self, code='signed', before=100, after=125):
        return {'code': code, 'receipt_code': 'accepted', 'quota': after, 'quota_before': before,
                'quota_after': after, 'used_before': 0, 'used_after': 0, 'verified_gain': after - before,
                'reward_amount': 25 if code == 'signed' else None, 'message': 'synthetic receipt', 'logs': []}

    async def test_same_beijing_day_prior_reward_does_not_skip_website(self):
        identifier = account(self.store)
        self.store.observe_account(identifier, '42', 100, 0, at=time.time()-120, confirm=True, evidence='balance_25')
        self.store.record_checkin(identifier, None, 'signed', 75, 100, used_before=0, used_after=0, reward_amount=25)
        self.service.result = self.receipt()
        job = await self.perform(identifier)
        self.assertEqual((job['status'], self.service.checkins), ('signed', 1))
        self.assertEqual(self.store.checkin_balance(job_id=job['id'])['earned'], 25)

    async def test_prior_reward_cannot_convert_no_credit_into_success(self):
        identifier = account(self.store)
        self.store.observe_account(identifier, '42', 100, 0, at=time.time()-120, confirm=True, evidence='balance_25')
        self.service.result = self.receipt('no_credit', 100, 100)
        job = await self.perform(identifier)
        self.assertEqual(job['status'], 'no_credit')
        self.assertIsNone(self.store.checkin_balance(job_id=job['id'])['earned'])
        self.assertEqual(self.store.public_account(self.store.account(identifier))['today_checkin'], 'no_credit')

    async def test_legacy_empty_receipt_is_not_promoted_from_history(self):
        identifier = account(self.store)
        self.store.observe_account(identifier, '42', 100, 0, at=time.time()-120, confirm=True)
        self.service.result = self.receipt('uncertain', 100, 100)
        self.assertEqual((await self.perform(identifier))['status'], 'uncertain')

    async def test_completed_no_credit_releases_marker_for_later_automatic_task(self):
        identifier = account(self.store)
        calls = []
        async def checkin(*args, before_submit, on_submit):
            before_submit({'quota': 50000000, 'used_quota': 0}, time.time())
            on_submit()
            calls.append(1)
            return self.receipt('no_credit', 100, 100)
        self.service.checkin = checkin
        for _ in range(2):
            jid, _ = self.store.enqueue('checkin', identifier, source='schedule:fixture')
            await self.engine.process(self.engine.claim())
            self.assertEqual(self.store.one('SELECT status FROM jobs WHERE id=?', (jid,))['status'], 'no_credit')
            self.assertFalse(submitted_today(self.store.daily_state(self.store.account(identifier)), '42', time.time()))
        self.assertEqual(len(calls), 2)

    async def test_legacy_completed_success_marker_does_not_block_next_task(self):
        identifier = account(self.store)
        self.store.mark_submission(identifier, '42')
        self.store.execute("UPDATE accounts SET checkin_status='signed' WHERE id=?", (identifier,))
        self.service.result = self.receipt()
        self.assertEqual((await self.perform(identifier))['status'], 'signed')
        self.assertEqual(self.service.checkins, 1)

    async def test_unknown_request_outcome_stays_protected(self):
        identifier = account(self.store)
        self.store.mark_submission(identifier, '42')
        self.store.execute("UPDATE accounts SET checkin_status='uncertain' WHERE id=?", (identifier,))
        self.assertEqual((await self.perform(identifier))['status'], 'uncertain')
        self.assertEqual(self.service.checkins, 0)

    async def test_manual_confirmation_does_not_prevent_later_claim(self):
        identifier = account(self.store)
        self.store.observe_account(identifier, '42', 100, 0, at=time.time()-120, confirm=True, evidence='manual')
        self.store.execute("UPDATE accounts SET checkin_status='manual_confirmed' WHERE id=?", (identifier,))
        self.service.result = self.receipt()
        self.assertEqual((await self.perform(identifier))['status'], 'signed')
        self.assertEqual(self.service.checkins, 1)

    async def test_uncredited_result_is_filterable_and_can_be_acknowledged(self):
        identifier = account(self.store)
        self.store.observe_account(identifier, '42', 100, 0, at=time.time()-120, confirm=True, evidence='balance_25')
        self.service.result = self.receipt('no_credit', 100, 100)
        await self.perform(identifier)
        response = await self.client.get('/api/v1/accounts?needs_confirmation=true')
        self.assertEqual(response.json()['total'], 1)
        stats = (await self.client.get('/api/v1/stats')).json()
        self.assertEqual((stats['signed'], stats['no_credit'], stats['earned']), (0, 1, 0))
        response = await self.client.post(f'/api/v1/accounts/{identifier}/checkin/confirm', json={'day': beijing_day(time.time())})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(self.store.public_account(self.store.account(identifier))['manual_confirmation'])
        stats = (await self.client.get('/api/v1/stats')).json()
        self.assertEqual((stats['signed'], stats['no_credit'], stats['manual'], stats['earned']), (0, 0, 1, 0))


if __name__ == '__main__':
    unittest.main(defaultTest=['WebsiteEligibilityTests'], verbosity=2)
