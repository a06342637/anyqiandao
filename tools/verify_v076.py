"""Only measured check-in increments enter earnings; all accounts are synthetic."""
import json
import time
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from app.crypto import Vault
from app.db import Store, checkin_earned
from app.errors import TaskError
from app.router_service import RouterService
from app.schemas import RuntimeSettings
from tools.backup_fixture import export_document
from tools import verify_v3 as base

account = base.account


class EarningsTests(unittest.TestCase):
    def test_reward_field_cannot_replace_missing_or_smaller_balance_increment(self):
        row = dict(code='signed', balance_source='live', quota_before=1000, quota_after=1025,
                   used_before=0, used_after=0, reward_amount=1025)
        for changes, expected in [({}, 25), ({'quota_before': None}, None),
                                  ({'quota_after': 1000}, 0), ({'used_before': None}, None),
                                  ({'used_before': 20, 'used_after': 10}, None),
                                  ({'quota_after': 1023, 'used_after': 2}, 25),
                                  ({'reward_amount': 10}, 10), ({'balance_source': 'legacy'}, None),
                                  ({'code': 'already_signed'}, None)]:
            with self.subTest(changes=changes):
                self.assertEqual(checkin_earned(row | changes), expected)


class AgentBaselineTests(unittest.IsolatedAsyncioTestCase):
    async def run_protocol(self, balances, previous=None, previous_error=False):
        calls = []
        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def open(self, *args): pass
            async def challenge_visible(self): return False
            async def cookies(self): return {'session': 'synthetic-fresh'}
            async def api(self, path, **options):
                calls.append(path)
                quota = 0 if path.endswith('login') else balances.pop(0)
                return 200, json.dumps({'success': True, 'data': {'id': 42, 'quota': quota * 500000, 'used_quota': 0}})
        @asynccontextmanager
        async def signed_in(*args):
            if previous_error:
                raise TaskError('invalid', 'synthetic expired')
            yield Session()
        service = RouterService()
        with patch('app.router_service.BrowserSession', return_value=Session()), \
             patch.object(service, 'signed_in', signed_in), patch('app.router_service.asyncio.sleep', new=AsyncMock()):
            result = await service.agent_login_checkin('synthetic', 'synthetic', None, RuntimeSettings(), before_credentials=previous)
        return result['checkin_result'], calls

    async def test_zero_login_dto_does_not_count_full_account_balance(self):
        result, calls = await self.run_protocol([1500, 1500, 1500, 1500])
        self.assertEqual((result['quota_before'], result['quota_after'], result['code']), (1500, 1500, 'no_credit'))
        self.assertIsNone(result['reward_amount'])
        self.assertNotIn('/api/user/sign_in', calls)

    async def test_real_prelogin_balance_counts_only_login_increment(self):
        credentials = {'provider': 'agentrouter', 'session': 'synthetic-old', 'api_user': '42'}
        result, calls = await self.run_protocol([1500, 1525], credentials)
        self.assertEqual((result['quota_before'], result['quota_after'], result['reward_amount']), (1500, 1525, 25))
        self.assertEqual(calls, ['/api/user/self', '/api/user/login', '/api/user/self'])

    async def test_expired_credentials_establish_baseline_without_inventing_reward(self):
        credentials = {'provider': 'agentrouter', 'session': 'synthetic-old', 'api_user': '42'}
        result, _ = await self.run_protocol([1500, 1500, 1500, 1500], credentials, previous_error=True)
        self.assertEqual(result['code'], 'no_credit')
        self.assertIsNone(result['reward_amount'])

    async def test_delayed_credit_after_first_observation_is_measured(self):
        result, _ = await self.run_protocol([1500, 1525])
        self.assertEqual(result['reward_amount'], 25)


class StatisticsAndLogsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.ApiTests.asyncSetUp
    asyncTearDown = base.ApiTests.asyncTearDown

    async def test_each_provider_totals_only_delta_and_logs_before_after_increment(self):
        for provider in ('agentrouter', 'anyrouter'):
            identifier = account(self.store, provider)
            self.store.execute('UPDATE accounts SET provider=? WHERE id=?', (provider, identifier))
            now = time.time()
            receipt = dict(code='signed', receipt_code='accepted', quota=1525, quota_before=1500, quota_after=1525,
                           used_before=0, used_after=0, reward_amount=1525, observed_before=now,
                           observed_after=now, claimed_at=now, message='synthetic receipt', logs=[])
            if provider == 'agentrouter':
                self.service.agent_login_checkin = AsyncMock(return_value={'provider': provider, 'session': 'synthetic', 'api_user': '42',
                    'quota': 1525, 'checkin_result': receipt})
            else:
                self.service.result = receipt
            jid, _ = self.store.enqueue('checkin', identifier)
            await self.engine.process(self.engine.claim())
            stats = (await self.client.get('/api/v1/stats', params={'provider': provider, 'range': 'week'})).json()
            self.assertEqual(stats['earned'], 25)
            self.assertEqual(sum(point['earned'] for point in stats['series']), 25)
            self.assertEqual(stats['accounts'][0]['earned'], 25)
            job = (await self.client.get('/api/v1/jobs/status', params={'ids': jid})).json()['items'][0]
            self.assertEqual(job['balance']['earned'], 25)
            self.assertIn('签到前 $1500.0000 → 签到后 $1525.0000；本次新增 +$25.0000', job['message'])
            logs = (await self.client.get('/api/v1/logs', params={'job_id': jid})).json()['items']
            self.assertTrue(any('本次新增 +$25.0000' in log['message'] for log in logs))
            history = (await self.client.get('/api/v1/checkins', params={'account_id': identifier})).json()['items']
            self.assertEqual(history[0]['earned'], 25)

    async def test_upgrade_preserves_history_but_excludes_old_agent_login_baselines(self):
        agent = account(self.store, 'agent-upgrade')
        any_id = account(self.store, 'any-upgrade')
        self.store.execute("UPDATE accounts SET provider='agentrouter' WHERE id=?", (agent,))
        self.store.record_checkin(agent, None, 'signed', 0, 1525, used_before=0, used_after=0, reward_amount=1525)
        self.store.record_checkin(any_id, None, 'signed', 1500, 1525, reward_amount=25)
        before = self.store.account(agent)
        self.store.execute('PRAGMA user_version=12')
        reopened = Store(self.config.data_dir, Vault(self.config.key))
        self.assertEqual(reopened.checkin_balance(account_id=agent)['balance_source'], 'legacy')
        self.assertIsNone(reopened.checkin_balance(account_id=agent)['earned'])
        self.assertEqual(reopened.account(agent)['login_enc'], before['login_enc'])
        self.assertEqual(reopened.account(agent)['quota'], before['quota'])
        self.assertEqual(reopened.checkin_balance(account_id=any_id)['earned'], 25)
        self.assertEqual(reopened.one('SELECT COUNT(*) n FROM checkins')['n'], 2)
        reopened.record_checkin(agent, None, 'signed', 1500, 1525, reward_amount=25)
        Store(self.config.data_dir, Vault(self.config.key))
        stats = (await self.client.get('/api/v1/stats?provider=agentrouter')).json()
        self.assertEqual((stats['earned'], stats['legacy_records']), (25, 1))

    async def test_old_backup_cannot_reintroduce_full_agent_balance_as_earnings(self):
        identifier = account(self.store, 'agent-backup')
        self.store.execute("UPDATE accounts SET provider='agentrouter' WHERE id=?", (identifier,))
        self.store.record_checkin(identifier, None, 'signed', 0, 1525, reward_amount=1525)
        document = await export_document(self.client)
        document['schema_version'] = 12
        self.store.execute('DELETE FROM checkins')
        self.assertEqual((await self.client.post('/api/v1/backup/restore', json=document)).status_code, 200)
        stats = (await self.client.get('/api/v1/stats?provider=agentrouter')).json()
        self.assertEqual((stats['earned'], stats['legacy_records']), (0, 1))


if __name__ == '__main__':
    unittest.main(verbosity=2)
