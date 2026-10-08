"""Global serialization, explicit routing, safe fallback and portable backups."""
import asyncio
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from argon2 import PasswordHasher
from app.config import Config
from app.errors import TaskError
from app.main import create_app
from app.network_routes import route_for
from app.schemas import NetworkRoute, RuntimeSettings
from app.router_service import BrowserSession
from app.resources import memory_status
from tools import verify_v3 as fixtures
from tools.backup_fixture import export_document


class FakeBridge:
    def __init__(self, config, *args):
        self.config = config
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False


class SerialRouteTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ApiTests.asyncSetUp

    async def asyncTearDown(self):
        await self.engine.stop()
        await fixtures.ApiTests.asyncTearDown(self)

    async def proxy(self, name):
        response = await self.client.post('/api/v1/proxies', json={'name': name, 'scheme': 'http', 'host': name + '.example.invalid', 'port': 8080, 'username': 'synthetic', 'password': 'synthetic-proxy-secret'})
        self.assertEqual(response.status_code, 200, response.text)
        # Creating a node also queues its connectivity test. Keep these routing
        # tests offline and leave the account job at the head of the queue.
        for job_id in response.json()['job_ids']:
            self.engine.cancel(job_id)
        return response.json()['id']

    async def routes(self, **overrides):
        values = self.store.settings().operation_routes.model_dump()
        values.update(overrides)
        response = await self.client.put('/api/v1/settings/routes', json=values)
        self.assertEqual(response.status_code, 200, response.text)

    async def run_job(self, kind='validate', identifier=None, payload=None):
        identifier = identifier or fixtures.account(self.store, str(time.time_ns()))
        job_id, _ = self.store.enqueue(kind, identifier, payload=payload)
        job = self.engine.claim()
        self.assertEqual(job['id'], job_id)
        await self.engine.process(job)
        return self.store.one('SELECT * FROM jobs WHERE id=?', (job_id,))

    async def test_adding_proxy_never_opts_in_any_operation(self):
        await self.proxy('unused')
        self.store.set_meta('settings', self.store.settings().model_copy(update={'proxy_mode': 'pool'}).model_dump_json())
        self.service.validate = AsyncMock(return_value={'quota': 10})
        with patch('app.engine.ProxyBridge', FakeBridge) as bridge:
            job = await self.run_job()
        self.assertEqual(job['status'], 'success')
        self.assertIsNone(self.service.validate.call_args.args[1])
        self.assertTrue(all(route['mode'] == 'direct' for route in self.store.settings().operation_routes.model_dump().values()))

    async def test_five_proxy_failures_then_one_direct_attempt(self):
        chosen = await self.proxy('chosen')
        await self.proxy('unused')
        await self.routes(validate={'mode': 'proxy', 'proxy_id': chosen})
        calls = []
        async def validate(credentials, route, settings):
            calls.append(route.config['host'] if route else 'direct')
            if route:
                raise TaskError('network_error', 'synthetic network error', retry_proxy=True)
            return {'quota': 10}
        self.service.validate = AsyncMock(side_effect=validate)
        with patch('app.engine.ProxyBridge', FakeBridge), patch('app.engine.asyncio.sleep', new=AsyncMock()):
            result = await self.run_job()
        self.assertEqual(result['status'], 'success')
        self.assertEqual(calls, ['chosen.example.invalid'] * 5 + ['direct'])
        messages = [row['message'] for row in self.store.all('SELECT message FROM logs WHERE job_id=?', (result['id'],))]
        self.assertTrue(any('5/5' in message for message in messages))
        self.assertTrue(any('直连替补' in message for message in messages))
        self.assertNotIn('synthetic-proxy-secret', '\n'.join(messages))

    async def test_successful_retry_stops_without_fallback(self):
        chosen = await self.proxy('chosen')
        await self.routes(validate={'mode': 'proxy', 'proxy_id': chosen})
        self.service.validate = AsyncMock(side_effect=[TaskError('network_error', 'synthetic', retry_proxy=True), {'quota': 10}])
        with patch('app.engine.ProxyBridge', FakeBridge), patch('app.engine.asyncio.sleep', new=AsyncMock()):
            self.assertEqual((await self.run_job())['status'], 'success')
        self.assertEqual(self.service.validate.await_count, 2)
        self.assertTrue(all(call.args[1] is not None for call in self.service.validate.call_args_list))

    async def test_submitted_checkin_never_falls_back_or_repeats(self):
        chosen = await self.proxy('chosen')
        await self.routes(checkin={'mode': 'proxy', 'proxy_id': chosen})
        async def sign(credentials, route, settings, *, on_submit, **kwargs):
            on_submit()
            raise TaskError('network_error', 'connection lost after submission', retry_proxy=True)
        self.service.checkin = AsyncMock(side_effect=sign)
        with patch('app.engine.ProxyBridge', FakeBridge):
            result = await self.run_job('checkin')
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(self.service.checkin.await_count, 1)

    async def test_invalid_credentials_do_not_retry_proxy_five_times(self):
        chosen = await self.proxy('chosen')
        await self.routes(extract={'mode': 'proxy', 'proxy_id': chosen})
        self.service.extract = AsyncMock(side_effect=TaskError('invalid_credentials', 'synthetic account error'))
        with patch('app.engine.ProxyBridge', FakeBridge):
            result = await self.run_job('extract')
        self.assertEqual(result['status'], 'invalid_credentials')
        self.assertEqual(self.service.extract.await_count, 1)

    async def test_direct_connection_lost_after_submission_is_uncertain(self):
        async def sign(credentials, route, settings, *, on_submit, **kwargs):
            on_submit()
            raise TaskError('network_error', 'synthetic dropped receipt', retry_proxy=True)
        self.service.checkin = AsyncMock(side_effect=sign)
        self.assertEqual((await self.run_job('checkin'))['status'], 'uncertain')
        self.assertEqual(self.service.checkin.await_count, 1)

    async def test_disabled_selected_proxy_does_not_silently_change_route(self):
        chosen = await self.proxy('disabled')
        await self.routes(validate={'mode': 'proxy', 'proxy_id': chosen})
        self.store.execute('UPDATE proxies SET enabled=0 WHERE id=?', (chosen,))
        self.service.validate = AsyncMock()
        self.assertEqual((await self.run_job())['status'], 'proxy_error')
        self.service.validate.assert_not_called()

    async def test_refresh_uses_its_own_route_then_returns_to_validation_route(self):
        validate_proxy, refresh_proxy = await self.proxy('validate'), await self.proxy('refresh')
        await self.routes(validate={'mode': 'proxy', 'proxy_id': validate_proxy}, refresh={'mode': 'proxy', 'proxy_id': refresh_proxy})
        identifier = fixtures.account(self.store, validity='invalid')
        seen = []
        async def validate(credentials, route, settings):
            seen.append(route.config['host'])
            if credentials['session'] == 'expired':
                raise TaskError('invalid', 'synthetic expired cookie')
            return {'quota': 12}
        async def extract(username, password, route, settings):
            seen.append(route.config['host'])
            return {'session': 'fresh', 'api_user': '42', 'quota': 10}
        self.service.validate = AsyncMock(side_effect=validate)
        self.service.extract = AsyncMock(side_effect=extract)
        with patch('app.engine.ProxyBridge', FakeBridge):
            result = await self.run_job('validate', identifier, {'repair_invalid': True})
        self.assertEqual(result['status'], 'success')
        self.assertEqual(seen, ['validate.example.invalid', 'refresh.example.invalid', 'validate.example.invalid'])

    async def test_tokens_and_dashboard_routes_are_independent(self):
        chosen = await self.proxy('tokens')
        await self.routes(tokens={'mode': 'proxy', 'proxy_id': chosen})
        self.service.insights = AsyncMock(return_value={'profile': {}, 'items': []})
        with patch('app.engine.ProxyBridge', FakeBridge):
            first = await self.run_job('insights', payload={'view': 'tokens'})
            second = await self.run_job('insights', payload={'view': 'dashboard'})
        self.assertEqual((first['status'], second['status']), ('success', 'success'))
        self.assertIsNotNone(self.service.insights.call_args_list[0].args[1])
        self.assertIsNone(self.service.insights.call_args_list[1].args[1])

    async def test_schedule_override_account_override_and_default(self):
        chosen = await self.proxy('account')
        identifier = fixtures.account(self.store)
        response = await self.client.put('/api/v1/accounts/routes/checkin', json={'ids': [identifier], 'network_route': {'mode': 'proxy', 'proxy_id': chosen}})
        self.assertEqual(response.status_code, 200)
        schedule = (await self.client.post('/api/v1/schedules', json={'name': 'test', 'ids': [identifier], 'network_route': {'mode': 'direct'}})).json()['id']
        settings = self.store.settings()
        account = self.store.account(identifier)
        self.assertEqual(route_for(self.store, settings, {'source': 'manual'}, account, 'checkin').proxy_id, chosen)
        self.assertEqual(route_for(self.store, settings, {'source': 'schedule:' + schedule}, account, 'checkin').mode, 'direct')
        self.assertEqual((await self.client.get('/api/v1/schedules')).json()['items'][0]['network_route']['mode'], 'direct')

    async def test_global_slot_is_held_through_cleanup_after_job_status_finishes(self):
        first = fixtures.account(self.store, 'first')
        second = fixtures.account(self.store, 'second')
        first_id, _ = self.store.enqueue('validate', first)
        self.store.enqueue('checkin', second)
        job = self.engine.claim()
        self.engine.running[first_id] = (job, None)
        self.engine.finish(job, 'success', 'done but cleanup still running')
        self.assertIsNone(self.engine.claim())
        self.engine.running.clear()
        self.assertIsNotNone(self.engine.claim())

    async def test_memory_pressure_leaves_tasks_queued(self):
        identifier = fixtures.account(self.store)
        job_id, _ = self.store.enqueue('checkin', identifier)
        with patch('app.engine.memory_status', return_value={'ready': False}):
            await self.engine.start()
            for _ in range(50):
                if self.engine.resource_wait:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(self.engine.resource_wait)
            self.assertEqual(self.store.one('SELECT status FROM jobs WHERE id=?', (job_id,))['status'], 'pending')

    async def test_routes_survive_backup_with_remapped_proxy_ids(self):
        chosen = await self.proxy('backup')
        await self.routes(tokens={'mode': 'proxy', 'proxy_id': chosen})
        identifier = fixtures.account(self.store)
        await self.client.put('/api/v1/accounts/routes/checkin', json={'ids': [identifier], 'network_route': {'mode': 'proxy', 'proxy_id': chosen}})
        await self.client.post('/api/v1/schedules', json={'name': 'backup-plan', 'ids': [identifier], 'network_route': {'mode': 'proxy', 'proxy_id': chosen}})
        document = await export_document(self.client)
        config = Config(bytes(reversed(range(32))), PasswordHasher().hash('synthetic-restore'), self.root / 'restored', 'https://restore.invalid', start_worker=False)
        application = create_app(config)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url=config.public_url) as client:
            login = await client.post('/api/v1/auth/login', json={'password': 'synthetic-restore'})
            client.headers['X-CSRF-Token'] = login.json()['csrf_token']
            result = await client.post('/api/v1/backup/restore', json=document)
            self.assertEqual(result.status_code, 200, result.text)
            proxy_id = (await client.get('/api/v1/proxies')).json()['items'][0]['id']
            self.assertNotEqual(proxy_id, chosen)
            self.assertEqual((await client.get('/api/v1/settings')).json()['settings']['operation_routes']['tokens']['proxy_id'], proxy_id)
            self.assertEqual((await client.get('/api/v1/accounts')).json()['items'][0]['checkin_route']['proxy_id'], proxy_id)
            self.assertEqual((await client.get('/api/v1/schedules')).json()['items'][0]['network_route']['proxy_id'], proxy_id)

    async def test_missing_proxy_in_backup_rolls_back_entire_restore(self):
        chosen = await self.proxy('missing')
        await self.routes(tokens={'mode': 'proxy', 'proxy_id': chosen})
        document = await export_document(self.client)
        document['proxies'] = []
        before = self.store.settings().model_dump()
        result = await self.client.post('/api/v1/backup/restore', json=document)
        self.assertEqual(result.status_code, 400, result.text)
        self.assertEqual(self.store.settings().model_dump(), before)


class BrowserCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_cancel_waits_for_browser_and_driver_cleanup(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def close():
            started.set()
            await release.wait()
        session = BrowserSession(None, RuntimeSettings())
        session.browser = SimpleNamespace(close=AsyncMock(side_effect=close))
        manager = SimpleNamespace(__aexit__=AsyncMock())
        session.manager = manager
        task = asyncio.create_task(session.__aexit__(None, None, None))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        manager.__aexit__.assert_awaited_once()


class MemoryAdmissionTests(unittest.TestCase):
    def test_reclaimable_file_cache_does_not_stall_idle_queue(self):
        values = {'/proc/meminfo': 'MemAvailable: 500000 kB',
                  '/sys/fs/cgroup/memory.max': str(640 * 1024**2),
                  '/sys/fs/cgroup/memory.current': str(600 * 1024**2),
                  '/sys/fs/cgroup/memory.stat': 'inactive_file ' + str(400 * 1024**2)}
        with patch.object(Path, 'read_text', lambda path: values[str(path).replace('\\', '/')]):
            self.assertTrue(memory_status()['ready'])
            values['/sys/fs/cgroup/memory.stat'] = 'inactive_file 0'
            self.assertFalse(memory_status()['ready'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
