"""v0.7.7 account ordering, selected members and one extra check-in round."""
import copy
import time
import unittest
from unittest.mock import AsyncMock

from app.crypto import Vault
from app.db import Store
from app.errors import TaskError
from app.providers import account_fingerprint
from tools.backup_fixture import export_document
from tools import verify_v3 as base


class AccountTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.ApiTests.asyncSetUp
    asyncTearDown = base.ApiTests.asyncTearDown

    def add(self, name, provider='anyrouter'):
        aid = base.account(self.store, name)
        self.store.execute('UPDATE accounts SET provider=?,login_hash=? WHERE id=?',
                           (provider, account_fingerprint(self.store.vault, name, provider), aid))
        return aid

    async def ids(self, provider=None, **params):
        if provider:
            params['provider'] = provider
        response = await self.client.get('/api/v1/accounts', params=params)
        self.assertEqual(response.status_code, 200)
        return [item['id'] for item in response.json()['items']]

    async def reorder(self, ids, provider=None):
        response = await self.client.post('/api/v1/accounts/reorder', json={'ids': ids, 'provider': provider})
        self.assertEqual(response.status_code, 200, response.text)

    async def test_three_orders_are_independent_and_persist_after_restart(self):
        a, b = self.add('a'), self.add('b')
        x, y = self.add('x', 'agentrouter'), self.add('y', 'agentrouter')
        await self.reorder([b, a], 'anyrouter')
        await self.reorder([y, x], 'agentrouter')
        self.assertEqual(await self.ids(), [a, b, x, y])
        await self.reorder([y, a, x, b])
        Store(self.config.data_dir, Vault(self.config.key))
        self.assertEqual(await self.ids(), [y, a, x, b])
        self.assertEqual(await self.ids('anyrouter'), [b, a])
        self.assertEqual(await self.ids('agentrouter'), [y, x])

    async def test_filtered_partial_page_only_reorders_occupied_slots(self):
        ids = [self.add(str(i)) for i in range(6)]
        await self.reorder(ids, 'anyrouter')
        self.store.execute("UPDATE accounts SET validity='invalid' WHERE id=?", (ids[1],))
        await self.reorder([ids[2], ids[0]], 'anyrouter')
        self.assertEqual(await self.ids('anyrouter'), [ids[2], ids[1], ids[0], *ids[3:]])
        self.assertEqual(await self.ids('anyrouter', validity='valid', limit=2), [ids[2], ids[0]])
        self.assertEqual(await self.ids(), ids)

    async def test_wrong_provider_unknown_duplicate_reorder_is_atomic(self):
        a, x = self.add('a'), self.add('x', 'agentrouter')
        for ids, provider in [([a, x], 'anyrouter'), ([a, a], None), ([a, 'missing'], None), ([a], 'other')]:
            response = await self.client.post('/api/v1/accounts/reorder', json={'ids': ids, 'provider': provider})
            self.assertIn(response.status_code, (400, 422))
            self.assertEqual(await self.ids(), [a, x])

    async def test_new_account_appends_to_platform_order(self):
        a, b = self.add('a'), self.add('b')
        await self.reorder([b, a], 'anyrouter')
        c = self.add('c')
        await self.reorder([c, a, b])
        self.assertEqual(await self.ids('anyrouter'), [b, a, c])

    async def test_migrate_v13_copies_existing_order(self):
        a, b = self.add('a'), self.add('b')
        await self.reorder([b, a])
        self.store.execute('DROP INDEX accounts_provider_position')
        self.store.execute('ALTER TABLE accounts DROP COLUMN provider_position')
        self.store.execute('PRAGMA user_version=13')
        Store(self.config.data_dir, Vault(self.config.key))
        self.assertEqual(await self.ids('anyrouter'), [b, a])
        self.assertEqual(self.store.one('PRAGMA user_version')['user_version'], 14)

    async def test_backup_restore_retains_all_orders(self):
        a, b = self.add('a'), self.add('b')
        x, y = self.add('x', 'agentrouter'), self.add('y', 'agentrouter')
        await self.reorder([b, a], 'anyrouter')
        await self.reorder([y, x], 'agentrouter')
        await self.reorder([x, b, a, y])
        document = await export_document(self.client)
        self.store.execute('DELETE FROM accounts')
        response = await self.client.post('/api/v1/backup/restore', json=document)
        self.assertEqual(response.status_code, 200, response.text)
        for provider, names in [(None, ['x', 'b', 'a', 'y']), ('anyrouter', ['b', 'a']), ('agentrouter', ['y', 'x'])]:
            rows = (await self.client.get('/api/v1/accounts', params={'provider': provider or ''})).json()['items']
            self.assertEqual([row['username'] for row in rows], names)

    async def test_schedule_members_paginate_and_survive_keep_accounts(self):
        ids = [self.add(f'member-{i}', 'agentrouter' if i % 2 else 'anyrouter') for i in range(13)]
        payload = {'name': 'daily', 'interval_minutes': 1440, 'ids': ids}
        schedule = (await self.client.post('/api/v1/schedules', json=payload)).json()['id']
        response = await self.client.get(f'/api/v1/schedules/{schedule}/accounts?limit=10&page=2')
        data = response.json()
        self.assertEqual((data['total'], len(data['items'])), (13, 3))
        self.assertEqual([r['id'] for r in data['items']], ids[10:])
        self.assertFalse({'password', 'session', 'login_enc', 'result_enc'} & data['items'][0].keys())
        self.store.execute("UPDATE accounts SET result_enc=NULL WHERE id=?", (ids[0],))
        response = await self.client.put(f'/api/v1/schedules/{schedule}', json=payload | {'ids': [], 'keep_accounts': True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await self.client.get(f'/api/v1/schedules/{schedule}/accounts')).json()['total'], 13)
        self.assertEqual((await self.client.get('/api/v1/schedules/missing/accounts')).status_code, 404)

    async def test_selection_preview_matches_cross_page_exclusions_and_provider(self):
        ids = [self.add(f'any-{i}') for i in range(12)]
        self.add('agent', 'agentrouter')
        selection = {'all_matching': True, 'provider': 'anyrouter', 'only_extracted': True, 'exclude_ids': [ids[1]]}
        data = (await self.client.post('/api/v1/accounts/selection-preview?limit=10&page=2', json=selection)).json()
        self.assertEqual((data['total'], [r['id'] for r in data['items']]), (11, [ids[-1]]))
        selection = {'ids': [ids[0], ids[-1]]}
        data = (await self.client.post('/api/v1/accounts/selection-preview', json=selection)).json()
        self.assertEqual(data['total'], 2)
        self.assertEqual((await self.client.post('/api/v1/accounts/selection-preview', json={'ids': []})).json()['total'], 0)


class RetryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.ApiTests.asyncSetUp
    asyncTearDown = base.ApiTests.asyncTearDown

    def receipt(self, code='no_credit', before=100, after=100):
        now = time.time()
        return dict(code=code, receipt_code='accepted', quota=after, quota_before=before, quota_after=after,
                    used_before=0, used_after=0, reward_amount=25 if code == 'signed' else None,
                    observed_before=now, observed_after=now, claimed_at=now, logs=['synthetic receipt'], message='synthetic')

    async def execute(self, results, provider='anyrouter', observed=100, source='manual'):
        aid = base.account(self.store, provider)
        self.store.execute('UPDATE accounts SET provider=? WHERE id=?', (provider, aid))
        self.rounds = []
        async def call(*args, **kwargs):
            callback = kwargs.get('before_submit')
            if callback:
                early = callback({'quota': 50000000, 'used_quota': 0}, time.time())
                if early:
                    return early
            if kwargs.get('on_submit'):
                kwargs['on_submit']()
            self.rounds.append(kwargs)
            result = results[len(self.rounds)-1]
            if isinstance(result, Exception):
                raise result
            result = copy.deepcopy(result)
            if provider == 'agentrouter':
                return dict(provider=provider, session=f'synthetic-{len(self.rounds)}', api_user='42', quota=result['quota'], checkin_result=result)
            return result
        self.service.checkin = call
        self.service.agent_login_checkin = call
        if isinstance(observed, Exception):
            self.service.validate = AsyncMock(side_effect=observed)
        else:
            self.service.validate = AsyncMock(return_value={'quota': observed, 'profile': {'used_quota': 0}})
        jid, _ = self.store.enqueue('checkin', aid, source=source)
        await self.engine.process(self.engine.claim())
        return self.store.one('SELECT * FROM jobs WHERE id=?', (jid,))

    async def test_anyrouter_no_credit_then_credit_in_same_job(self):
        job = await self.execute([self.receipt(), self.receipt('signed', after=125)])
        self.assertEqual((job['status'], len(self.rounds)), ('signed', 2))
        self.assertEqual(self.store.checkin_balance(job_id=job['id'])['earned'], 25)
        self.assertEqual(self.store.one('SELECT COUNT(*) n FROM checkins')['n'], 1)

    async def test_agentrouter_relogs_once_using_latest_credentials(self):
        job = await self.execute([self.receipt(), self.receipt('signed', after=125)], 'agentrouter')
        self.assertEqual((job['status'], len(self.rounds)), ('signed', 2))
        self.assertEqual(self.rounds[1]['before_credentials']['session'], 'synthetic-1')
        self.assertEqual(self.store.checkin_balance(job_id=job['id'])['earned'], 25)

    async def test_two_zero_rounds_stop_and_record_one_pending_result(self):
        job = await self.execute([self.receipt(), self.receipt()], source='schedule:fixture')
        self.assertEqual((job['status'], len(self.rounds)), ('no_credit', 2))
        self.assertIn('已自动再签到一轮，仍未到账', job['message'])
        self.assertEqual(self.store.one('SELECT COUNT(*) n FROM checkins')['n'], 1)
        state = self.store.daily_state(self.store.account(job['account_id']))
        self.assertIsNone(state.submitted_at)

    async def test_agentrouter_two_zero_rounds_stop(self):
        job = await self.execute([self.receipt(), self.receipt()], 'agentrouter')
        self.assertEqual((job['status'], len(self.rounds)), ('no_credit', 2))

    async def test_already_claimed_zero_is_retried(self):
        job = await self.execute([self.receipt('already_signed'), self.receipt('signed', after=125)])
        self.assertEqual((job['status'], len(self.rounds)), ('signed', 2))

    async def test_delay_confirmed_before_second_submission(self):
        job = await self.execute([self.receipt()], observed=125)
        self.assertEqual((job['status'], len(self.rounds)), ('signed', 1))
        self.assertEqual(self.store.checkin_balance(job_id=job['id'])['earned'], 25)

    async def test_agentrouter_delayed_credit_stops_before_relogin(self):
        job = await self.execute([self.receipt()], 'agentrouter', observed=125)
        self.assertEqual((job['status'], len(self.rounds)), ('signed', 1))
        self.assertEqual(self.store.checkin_balance(job_id=job['id'])['earned'], 25)

    async def test_cross_day_does_not_start_second_round(self):
        result = self.receipt() | {'observed_before': time.time() - 86400}
        job = await self.execute([result])
        self.assertEqual((job['status'], len(self.rounds)), ('uncertain', 1))

    async def test_success_never_retries(self):
        job = await self.execute([self.receipt('signed', after=125)])
        self.assertEqual((job['status'], len(self.rounds)), ('signed', 1))
        self.service.validate.assert_not_awaited()

    async def test_uncertain_never_retries(self):
        job = await self.execute([self.receipt('uncertain')])
        self.assertEqual((job['status'], len(self.rounds)), ('uncertain', 1))
        self.service.validate.assert_not_awaited()

    async def test_failed_preflight_never_resubmits(self):
        job = await self.execute([self.receipt()], observed=TaskError('network_error', 'synthetic'))
        self.assertEqual((job['status'], len(self.rounds)), ('uncertain', 1))

    async def test_incomplete_usage_never_resubmits(self):
        result = self.receipt() | {'used_after': None}
        job = await self.execute([result])
        self.assertEqual((job['status'], len(self.rounds)), ('uncertain', 1))

    async def test_second_round_unknown_stops_with_persisted_marker(self):
        job = await self.execute([self.receipt(), TaskError('uncertain', 'synthetic unknown')])
        self.assertEqual((job['status'], len(self.rounds)), ('uncertain', 2))
        state = self.store.daily_state(self.store.account(job['account_id']))
        self.assertIsNotNone(state.submitted_at)

    async def test_unrelated_balance_change_prevents_retry(self):
        job = await self.execute([self.receipt()], observed=110)
        self.assertEqual((job['status'], len(self.rounds)), ('uncertain', 1))


if __name__ == '__main__':
    unittest.main()
