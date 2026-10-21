"""Provider statistics and import notes, using synthetic accounts only."""
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.checkin_state import BEIJING
from tools.verify_v3 import ApiTests, account


class DashboardImportTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ApiTests.asyncSetUp
    asyncTearDown = ApiTests.asyncTearDown

    async def test_provider_statistics_separate_counts_rewards_balances_and_daily_series(self):
        any_id = account(self.store, 'any-statistics')
        agent_id = account(self.store, 'agent-statistics')
        self.store.execute("UPDATE accounts SET provider='agentrouter',quota=200 WHERE id=?", (agent_id,))
        self.store.execute('UPDATE accounts SET quota=100 WHERE id=?', (any_id,))
        moment = datetime(2026, 10, 21, 12, tzinfo=BEIJING)
        start = moment.replace(hour=0).timestamp()
        records = [(any_id, 'signed', 25, start + 1), (any_id, 'no_credit', None, start + 2),
                   (agent_id, 'signed', 200, start + 3), (agent_id, 'error', None, start + 4),
                   (agent_id, 'uncertain', None, start + 5), (agent_id, 'manual_confirmed', None, start + 6),
                   (any_id, 'signed', 25, start - 1), (agent_id, 'signed', 175, start - 2),
                   (agent_id, 'signed', 9999, moment.timestamp() + 1)]
        for identifier, code, reward, created in records:
            self.store.execute("INSERT INTO checkins(account_id,code,quota_before,quota_after,used_before,used_after,reward_amount,created,balance_source) VALUES (?,?,0,?,0,0,?,?,'live')",
                               (identifier, code, reward, reward, created))
        with patch('app.statistics.datetime') as clock:
            clock.now.side_effect = lambda zone: moment.astimezone(zone)
            clock.fromtimestamp.side_effect = datetime.fromtimestamp
            for span in ('day', 'week', 'month'):
                results = {}
                for provider in ('anyrouter', 'agentrouter'):
                    response = await self.client.get('/api/v1/stats', params={'range': span, 'provider': provider})
                    self.assertEqual(response.status_code, 200, response.text)
                    stats = results[provider] = response.json()
                    self.assertEqual(stats['provider'], provider)
                    self.assertEqual(stats['accounts_total'], 1)
                    self.assertEqual({row['provider'] for row in stats['accounts']}, {provider})
                    for field in ('signed', 'already', 'failed', 'uncertain', 'no_credit', 'manual', 'earned'):
                        self.assertAlmostEqual(stats[field], sum(point[field] for point in stats['series']), places=4)
                    self.assertLessEqual(stats['accounts'][0]['last_balance']['created'], moment.timestamp())
                any_stats, agent_stats = results['anyrouter'], results['agentrouter']
                self.assertEqual((any_stats['earned'], agent_stats['earned']), (25, 200) if span == 'day' else (50, 375))
                self.assertEqual((any_stats['balance_total'], agent_stats['balance_total']), (100, 200))
                self.assertEqual((any_stats['no_credit'], agent_stats['no_credit']), (1, 0))
                self.assertEqual((any_stats['failed'], agent_stats['failed'], agent_stats['uncertain'], agent_stats['manual']), (0, 1, 1, 1))
                combined = (await self.client.get('/api/v1/stats', params={'range': span})).json()
                for field in ('attempts', 'signed', 'failed', 'uncertain', 'no_credit', 'manual', 'earned', 'balance_total', 'accounts_total'):
                    self.assertAlmostEqual(combined[field], any_stats[field] + agent_stats[field], places=4)

    async def test_empty_provider_and_invalid_provider(self):
        account(self.store)
        stats = (await self.client.get('/api/v1/stats?provider=agentrouter&range=week')).json()
        self.assertEqual((stats['attempts'], stats['earned'], stats['balance_total'], stats['accounts_total']), (0, 0, 0, 0))
        self.assertEqual(stats['accounts'], [])
        self.assertEqual(len(stats['series']), 7)
        self.assertEqual((await self.client.get('/api/v1/stats?provider=other')).status_code, 422)
        self.assertEqual((await self.client.get('/api/v1/stats?provider=anyrouter&range=other')).status_code, 400)

    async def test_password_import_saves_notes_for_both_providers_without_overwriting_duplicates(self):
        for provider in ('anyrouter', 'agentrouter'):
            rows = [{'username': 'same-import-name', 'password': 'synthetic-password', 'row_id': '1', 'provider': provider, 'note': '  主账号 · ' + provider + '  '},
                    {'username': 'legacy-without-note', 'password': 'synthetic-password', 'row_id': '2', 'provider': provider}]
            response = await self.client.post('/api/v1/accounts/import', json={'import_id': provider, 'accounts': rows})
            self.assertEqual(response.status_code, 200, response.text)
            identifier, legacy_id = response.json()['account_ids']
            self.assertEqual(self.store.account(identifier)['note'], '主账号 · ' + provider)
            self.assertEqual(self.store.account(legacy_id)['note'], '')
            rows[0]['note'] = 'replacement'
            repeated = await self.client.post('/api/v1/accounts/import', json={'import_id': provider + '-repeat', 'accounts': rows})
            self.assertEqual(repeated.json()['queued'], 0)
            self.assertEqual(self.store.account(identifier)['note'], '主账号 · ' + provider)
        self.assertEqual(self.store.one('SELECT COUNT(*) n FROM accounts')['n'], 4)

    async def test_session_import_preserves_notes_and_rejects_oversized_notes(self):
        row = {'username': 'session-with-note', 'session': 'synthetic-session', 'api_user': '101', 'note': '  手动导入备注  '}
        response = await self.client.post('/api/v1/accounts/import-credentials', json={'accounts': [row]})
        self.assertEqual(response.status_code, 200, response.text)
        identifier = response.json()['account_ids'][0]
        self.assertEqual(self.store.account(identifier)['note'], '手动导入备注')
        row['note'] = 'replacement'
        self.assertEqual((await self.client.post('/api/v1/accounts/import-credentials', json={'accounts': [row]})).json()['skipped'], 1)
        self.assertEqual(self.store.account(identifier)['note'], '手动导入备注')
        self.assertEqual((await self.client.post('/api/v1/accounts/import-credentials', json={'accounts': [row | {'api_user': '102', 'note': 'x' * 501}]})).status_code, 422)
        password_row = {'username': 'bad-note', 'password': 'synthetic-password', 'provider': 'agentrouter', 'row_id': '1', 'note': 'x' * 501}
        self.assertEqual((await self.client.post('/api/v1/accounts/import', json={'import_id': 'bad-note', 'accounts': [password_row]})).status_code, 422)


if __name__ == '__main__':
    unittest.main()
