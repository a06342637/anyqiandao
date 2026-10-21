"""Provider separation, AgentRouter login rewards and durable random schedules."""
import json
import random
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.checkin_state import BEIJING
from app.crypto import Vault
from app.db import Store
from app.errors import TaskError
from app.providers import account_fingerprint
from app.router_service import BrowserSession, RouterService, REQUEST_MARKER
from app.schemas import RuntimeSettings
from tools.backup_fixture import export_document
from tools.verify_v3 import ApiTests, account


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ApiTests.asyncSetUp
    asyncTearDown = ApiTests.asyncTearDown

    async def add(self, provider, name='same-user'):
        response = await self.client.post('/api/v1/accounts/import', json={'import_id': provider+name,
            'accounts': [{'username': name, 'password': 'synthetic-password', 'row_id': '1', 'provider': provider}]})
        self.assertEqual(response.status_code, 200, response.text)
        for job in response.json()['job_ids']:
            self.engine.cancel(job)
        return response.json()['account_ids'][0]

    async def test_same_username_is_separate_on_two_platforms(self):
        any_id, agent_id = await self.add('anyrouter'), await self.add('agentrouter')
        self.assertNotEqual(any_id, agent_id)
        self.assertEqual((await self.add('agentrouter')), agent_id)
        rows = (await self.client.get('/api/v1/accounts?provider=agentrouter')).json()
        self.assertEqual(rows['total'], 1)
        self.assertEqual(rows['items'][0]['provider'], 'agentrouter')

    async def test_existing_accounts_keep_anyrouter_identity_and_ciphertext(self):
        identifier = account(self.store)
        original = self.store.account(identifier)
        self.store.execute('PRAGMA user_version=11')
        reopened = Store(self.config.data_dir, Vault(self.config.key))
        current = reopened.account(identifier)
        self.assertEqual(current['provider'], 'anyrouter')
        self.assertEqual(current['login_enc'], original['login_enc'])
        self.assertEqual(current['login_hash'], account_fingerprint(reopened.vault, 'synthetic'))

    async def test_same_api_user_does_not_mix_platform_credentials(self):
        agent = await self.add('agentrouter')
        self.store.execute('UPDATE accounts SET result_enc=? WHERE id=?',
                           (self.store.vault.seal({'session': 'agent-session', 'api_user': '42'}, f'result:{agent}'), agent))
        response = await self.client.post('/api/v1/accounts/import-credentials', json={'accounts': [{'username': 'any-session', 'session': 'any-session-value', 'api_user': '42'}]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['added'], 1)
        self.assertEqual(self.store.account(agent)['result']['provider'], 'agentrouter')
        response = await self.client.put(f'/api/v1/accounts/{agent}/credentials', json={'session': 'wrong', 'api_user': '42'})
        self.assertEqual(response.status_code, 400)

    async def test_agent_task_uses_fresh_login_and_actual_reward(self):
        identifier = await self.add('agentrouter')
        now = time.time()
        receipt = {'code': 'signed', 'receipt_code': 'accepted', 'quota': 175, 'quota_before': 0, 'quota_after': 175,
                   'used_before': 0, 'used_after': 0, 'verified_gain': 175, 'reward_amount': 175,
                   'observed_before': now, 'observed_after': now, 'claimed_at': now, 'message': 'verified', 'logs': []}
        self.service.agent_login_checkin = AsyncMock(return_value={'provider': 'agentrouter', 'session': 'fresh-agent', 'api_user': '42', 'quota': 175, 'checkin_result': receipt})
        jid, _ = self.store.enqueue('checkin', identifier)
        await self.engine.process(self.engine.claim())
        self.assertEqual(self.store.one('SELECT status FROM jobs WHERE id=?', (jid,))['status'], 'signed')
        self.assertEqual(self.store.checkin_balance(job_id=jid)['earned'], 175)
        self.assertEqual(self.service.checkins, 0)
        self.service.agent_login_checkin.assert_awaited_once()

    async def test_cross_provider_backup_keeps_names_members_and_rewards_separate(self):
        any_id, agent_id = await self.add('anyrouter'), await self.add('agentrouter')
        for identifier in (any_id, agent_id):
            self.store.execute('UPDATE accounts SET result_enc=? WHERE id=?',
                               (self.store.vault.seal({'session': 'synthetic', 'api_user': '42'}, f'result:{identifier}'), identifier))
        self.store.record_checkin(agent_id, None, 'signed', 0, 175, used_before=0, used_after=0, reward_amount=175)
        response = await self.client.post('/api/v1/schedules', json={'ids': [any_id, agent_id], 'name': 'mixed', 'interval_minutes': 60})
        self.assertEqual(response.status_code, 200)
        document = await export_document(self.client)
        self.assertEqual({item['provider'] for item in document['accounts']}, {'anyrouter', 'agentrouter'})
        self.store.execute('DELETE FROM schedules')
        self.store.execute('DELETE FROM accounts')
        response = await self.client.post('/api/v1/backup/restore', json=document)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.one('SELECT COUNT(*) n FROM accounts')['n'], 2)
        self.assertEqual(self.store.one('SELECT COUNT(*) n FROM schedule_accounts')['n'], 2)
        self.assertEqual((await self.client.get('/api/v1/stats')).json()['earned'], 175)

    async def test_agent_credential_repair_never_logs_into_anyrouter(self):
        identifier = await self.add('agentrouter')
        self.store.execute('UPDATE accounts SET result_enc=? WHERE id=?', (self.store.vault.seal({'session': 'old-agent', 'api_user': '42'}, f'result:{identifier}'), identifier))
        self.service.validate = AsyncMock(side_effect=[TaskError('invalid', 'expired'), {'quota': 175, 'profile': {'used_quota': 0}}])
        now = time.time()
        self.service.agent_login_checkin = AsyncMock(return_value={'provider': 'agentrouter', 'session': 'fresh-agent', 'api_user': '42', 'quota': 175,
            'checkin_result': {'code': 'signed', 'receipt_code': 'accepted', 'quota': 175, 'quota_before': 0, 'quota_after': 175,
                'used_before': 0, 'used_after': 0, 'reward_amount': 175, 'observed_before': now, 'observed_after': now, 'claimed_at': now, 'message': 'verified', 'logs': []}})
        jid, _ = self.store.enqueue('validate', identifier, payload={'repair_invalid': True})
        await self.engine.process(self.engine.claim())
        self.assertEqual(self.store.one('SELECT status FROM jobs WHERE id=?', (jid,))['status'], 'success')
        self.assertEqual(self.service.extracts, 0)
        self.service.agent_login_checkin.assert_awaited_once()
        self.assertEqual(self.store.checkin_balance(job_id=jid)['earned'], 175)


class AgentProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_reply_and_self_query_confirm_175_without_sign_in_post(self):
        calls = []
        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def open(self, *args): pass
            async def challenge_visible(self): return False
            async def cookies(self): return {'session': 'synthetic-agent-session'}
            async def api(self, path, **options):
                calls.append((path, options.get('method', 'GET')))
                profile = {'id': 42, 'quota': 0 if path.endswith('login') else 87500000, 'used_quota': 0}
                return 200, json.dumps({'success': True, 'data': profile})
        with patch('app.router_service.BrowserSession', return_value=Session()):
            result = await RouterService().agent_login_checkin('synthetic', 'synthetic', None, RuntimeSettings())
        self.assertEqual(calls, [('/api/user/login', 'POST'), ('/api/user/self', 'GET')])
        self.assertEqual(result['checkin_result']['reward_amount'], 175)

    async def test_agent_background_self_is_blocked_until_explicit_probe(self):
        session = BrowserSession(None, RuntimeSettings(), 'agentrouter')
        route = SimpleNamespace(request=SimpleNamespace(url='https://agentrouter.org/api/user/self', method='GET', headers={}), abort=AsyncMock(), continue_=AsyncMock())
        await session.protect_destination(route)
        route.abort.assert_awaited_once()
        route.request.headers = {REQUEST_MARKER: session.request_marker}
        await session.protect_destination(route)
        route.continue_.assert_awaited_once_with(headers={})

    async def test_browser_cannot_send_provider_credentials_to_other_site(self):
        session = BrowserSession(None, RuntimeSettings(), 'agentrouter')
        route = SimpleNamespace(request=SimpleNamespace(url='https://anyrouter.top/api/user/self', method='GET', headers={}), abort=AsyncMock(), continue_=AsyncMock())
        await session.protect_destination(route)
        route.abort.assert_awaited_once()

    async def test_redirected_page_cannot_receive_login_password(self):
        session = BrowserSession(None, RuntimeSettings(), 'agentrouter')
        session.page = SimpleNamespace(url='https://anyrouter.top/login', evaluate=AsyncMock())
        with self.assertRaises(TaskError):
            await session.api('/api/user/login', method='POST', body='synthetic')
        session.page.evaluate.assert_not_awaited()


class RandomScheduleTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ApiTests.asyncSetUp
    asyncTearDown = ApiTests.asyncTearDown

    async def create(self, count=12):
        self.now = datetime.now(BEIJING).replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
        self.start, self.end = self.now+3600, self.now+6*3600
        self.ids = [account(self.store, f'random-{n}') for n in range(count)]
        with patch('app.settings_api.time.time', return_value=self.now), patch('app.random_schedule.SystemRandom', return_value=random.Random(17)):
            response = await self.client.post('/api/v1/schedules', json={'ids': self.ids, 'name': 'window', 'mode': 'random_window', 'window_start': '13:00', 'window_end': '18:00'})
        self.assertEqual(response.status_code, 200, response.text)
        self.schedule = response.json()['id']
        return self.slots()

    def slots(self):
        return self.store.all('SELECT * FROM schedule_dispatches WHERE schedule_id=? ORDER BY due_at', (self.schedule,))

    async def test_times_are_distributed_inside_window_and_survive_restart(self):
        slots = await self.create()
        self.assertEqual(len(slots), 12)
        self.assertTrue(all(self.start <= row['due_at'] < self.end for row in slots))
        self.assertGreater(slots[-1]['due_at']-slots[0]['due_at'], (self.end-self.start)*0.8)
        Store(self.config.data_dir, Vault(self.config.key))
        with patch('app.settings_api.time.time', return_value=self.now):
            await self.client.get(f'/api/v1/schedules/{self.schedule}/assignments')
        self.engine.tick_schedules(self.start)
        self.assertEqual(self.slots(), slots)

    async def test_only_due_account_is_enqueued_once(self):
        slots = await self.create()
        now = slots[0]['due_at']+0.001
        self.engine.tick_schedules(now)
        self.engine.tick_schedules(now)
        jobs = self.store.all("SELECT * FROM jobs WHERE kind='checkin'")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['account_id'], slots[0]['account_id'])
        self.assertEqual(jobs[0]['not_after'], self.end)

    async def test_missed_window_never_bursts_accounts_after_restart(self):
        await self.create()
        self.engine.tick_schedules(self.end+1)
        self.assertEqual(self.store.one('SELECT COUNT(*) n FROM jobs')['n'], 0)
        slots = self.slots()
        self.assertEqual(sum(row['status']=='missed' for row in slots), 12)
        self.assertEqual(sum(row['status']=='planned' for row in slots), 12)
        self.assertTrue(all(row['due_at']>=self.start+86400 for row in slots if row['status']=='planned'))

    async def test_queued_work_expires_at_end_of_window(self):
        slots = await self.create(1)
        self.engine.tick_schedules(slots[0]['due_at']+0.001)
        with patch('app.engine.time.time', return_value=self.end+1):
            self.assertIsNone(self.engine.claim())
        self.assertEqual(self.store.one('SELECT status FROM jobs')['status'], 'cancelled')

    async def test_invalid_window_is_rejected(self):
        identifier = account(self.store)
        response = await self.client.post('/api/v1/schedules', json={'ids': [identifier], 'name': 'invalid', 'mode': 'random_window', 'window_start': '18:00', 'window_end': '13:00'})
        self.assertEqual(response.status_code, 422)

    async def test_random_backup_preserves_exact_assignments(self):
        slots = await self.create()
        with patch('app.backup_api.time.time', return_value=self.now):
            document = await export_document(self.client)
            self.store.execute('DELETE FROM schedules')
            response = await self.client.post('/api/v1/backup/restore', json=document)
        self.assertEqual(response.status_code, 200, response.text)
        self.schedule = self.store.one('SELECT id FROM schedules')['id']
        self.assertEqual([r['due_at'] for r in self.slots()], [r['due_at'] for r in slots])

    async def test_pause_resume_only_replans_unstarted_accounts(self):
        slots = await self.create(3)
        self.engine.tick_schedules(slots[0]['due_at']+0.001)
        jid = self.store.one('SELECT id FROM jobs')['id']
        self.store.execute("UPDATE jobs SET status='signed',started=?,finished=? WHERE id=?", (slots[0]['due_at'], slots[0]['due_at']+1, jid))
        await self.client.post(f'/api/v1/schedules/{self.schedule}/pause')
        now = (slots[0]['due_at']+slots[1]['due_at'])/2
        with patch('app.settings_api.time.time', return_value=now):
            response = await self.client.post(f'/api/v1/schedules/{self.schedule}/resume')
        self.assertEqual(response.status_code, 200)
        current = self.slots()
        completed = next(r for r in current if r['account_id']==slots[0]['account_id'])
        self.assertEqual(completed['due_at'], slots[0]['due_at'])
        self.assertTrue(all(now < r['due_at'] < self.end for r in current if r['status']=='planned'))

    async def test_default_random_plan_does_not_enqueue_new_any_account_immediately(self):
        await self.create(1)
        self.store.set_meta('auto_schedule_id', self.schedule)
        self.store.set_meta('settings', self.store.settings().model_copy(update={'auto_checkin': True}).model_dump_json())
        identifier = account(self.store, 'new-random-member')
        with patch('app.engine.time.time', return_value=self.now):
            self.engine.after_extract({'id': 'synthetic-extract', 'source': 'import'}, self.store.account(identifier))
        self.assertEqual(self.store.one('SELECT COUNT(*) n FROM jobs')['n'], 0)
        slot = self.store.one('SELECT * FROM schedule_dispatches WHERE account_id=?', (identifier,))
        self.assertTrue(self.start <= slot['due_at'] < self.end)

    async def test_edited_window_backup_preserves_completed_old_times(self):
        slots = await self.create(3)
        self.engine.tick_schedules(slots[0]['due_at']+0.001)
        self.store.execute("UPDATE jobs SET status='signed',started=?,finished=?", (slots[0]['due_at'], slots[0]['due_at']+1))
        with patch('app.settings_api.time.time', return_value=self.start+3600):
            response = await self.client.put(f'/api/v1/schedules/{self.schedule}', json={'name': 'window', 'mode': 'random_window',
                'window_start': '16:00', 'window_end': '17:00', 'keep_accounts': True})
            self.assertEqual(response.status_code, 200, response.text)
            document = await export_document(self.client)
            response = await self.client.post('/api/v1/backup/restore', json=document)
        self.assertEqual(response.status_code, 200, response.text)
        old = self.store.one("SELECT due_at FROM schedule_dispatches WHERE status='queued' AND account_id=?", (slots[0]['account_id'],))
        self.assertEqual(old['due_at'], slots[0]['due_at'])


if __name__ == '__main__':
    unittest.main(defaultTest=['ProviderTests', 'AgentProtocolTests', 'RandomScheduleTests'], verbosity=2)
