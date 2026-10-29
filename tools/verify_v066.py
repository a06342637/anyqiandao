"""Direct check-in attempts followed by one explicitly selected proxy."""
import json
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.errors import TaskError
from app.router_service import CHECKIN_PATH, RouterService
from tools.backup_fixture import export_document
from tools.verify_v3 import ApiTests, account
from tools.verify_serial_routes import FakeBridge, SerialRouteTests


class CheckinFallbackTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ApiTests.asyncSetUp
    asyncTearDown = ApiTests.asyncTearDown
    proxy = SerialRouteTests.proxy
    run_job = SerialRouteTests.run_job

    async def configure(self, *nodes):
        response = await self.client.put('/api/v1/settings/checkin-fallback', json={'ids': list(nodes)})
        self.assertEqual(response.status_code, 200, response.text)

    def browser(self, *, reward='proxy', fail=None):
        self.posts, self.sessions, self.active = [], [], 0
        service = RouterService()
        @asynccontextmanager
        async def signed_in(credentials, route, settings):
            label = route.config['host'] if route else 'direct'
            self.sessions.append(label)
            if fail == 'connect' and route is None:
                raise TaskError('network_error', 'synthetic direct failure', retry_proxy=True)
            self.active += 1
            self.assertEqual(self.active, 1, 'The direct browser must close before the proxy browser starts')
            async def api(path, method='GET', **kwargs):
                if path == CHECKIN_PATH:
                    self.posts.append(label)
                    if fail == 'unknown':
                        raise TaskError('uncertain', 'synthetic lost POST response')
                    return 200, json.dumps({'success': True})
                credited = reward == 'direct' and bool(self.posts)
                credited |= reward == 'proxy' and any(item != 'direct' for item in self.posts)
                credited |= reward == 'delayed' and route is not None
                return 200, json.dumps({'success': True, 'data': {'id': 42, 'quota': (125 if credited else 100) * 500000, 'used_quota': 0}})
            try:
                yield SimpleNamespace(api=api)
            finally:
                self.active -= 1
        service.signed_in = signed_in
        self.engine.service = service

    async def execute(self, identifier=None):
        with patch('app.engine.ProxyBridge', FakeBridge), patch('app.router_service.asyncio.sleep', new=AsyncMock()):
            return await self.run_job('checkin', identifier)

    async def test_two_direct_posts_then_one_selected_proxy_post(self):
        selected = await self.proxy('selected')
        await self.proxy('unselected')
        await self.configure(selected)
        self.browser()
        job = await self.execute()
        self.assertEqual(self.posts, ['direct', 'direct', 'selected.example.invalid'])
        self.assertEqual(self.sessions, ['direct', 'selected.example.invalid'])
        self.assertEqual(job['status'], 'signed')
        self.assertEqual(self.store.checkin_balance(job_id=job['id'])['earned'], 25)

    async def test_direct_reward_stops_without_proxy(self):
        await self.configure(await self.proxy('unused'))
        self.browser(reward='direct')
        self.assertEqual((await self.execute())['status'], 'signed')
        self.assertEqual(self.posts, ['direct'])
        self.assertEqual(self.sessions, ['direct'])

    async def test_delayed_reward_is_verified_on_proxy_without_another_post(self):
        await self.configure(await self.proxy('verify-only'))
        self.browser(reward='delayed')
        job = await self.execute()
        self.assertEqual(self.posts, ['direct', 'direct'])
        self.assertEqual(job['status'], 'signed')
        self.assertEqual(self.store.checkin_balance(job_id=job['id'])['earned'], 25)

    async def test_two_connection_failures_then_proxy(self):
        await self.configure(await self.proxy('available'))
        self.browser(fail='connect')
        self.assertEqual((await self.execute())['status'], 'signed')
        self.assertEqual(self.sessions, ['direct', 'direct', 'available.example.invalid'])
        self.assertEqual(self.posts, ['available.example.invalid'])

    async def test_unknown_post_result_does_not_retry_on_proxy(self):
        await self.configure(await self.proxy('unused'))
        self.browser(fail='unknown')
        self.assertEqual((await self.execute())['status'], 'uncertain')
        self.assertEqual(self.posts, ['direct'])
        self.assertEqual(self.sessions, ['direct'])

    async def test_no_selected_proxy_keeps_three_direct_attempts_per_round(self):
        await self.proxy('unselected')
        self.browser(reward=None)
        self.assertEqual((await self.execute())['status'], 'no_credit')
        self.assertEqual(self.posts, ['direct'] * 6)

    async def test_zero_credit_on_proxy_retries_one_round_then_stops(self):
        await self.configure(await self.proxy('no-credit'))
        self.browser(reward=None)
        job = await self.execute()
        self.assertEqual((job['status'], len(self.posts)), ('no_credit', 6))
        self.assertIsNone(self.store.checkin_balance(job_id=job['id'])['earned'])

    async def test_multiple_checked_proxies_rotate_between_accounts(self):
        a, b = await self.proxy('first'), await self.proxy('second')
        await self.configure(a, b)
        for expected in ('first.example.invalid', 'second.example.invalid'):
            self.browser()
            await self.execute()
            self.assertEqual(self.posts[-1], expected)

    async def test_explicit_account_proxy_keeps_priority(self):
        primary, fallback = await self.proxy('primary'), await self.proxy('fallback')
        await self.configure(fallback)
        identifier = account(self.store)
        self.store.execute('UPDATE accounts SET checkin_route=? WHERE id=?', (json.dumps({'mode': 'proxy', 'proxy_id': primary}), identifier))
        self.browser()
        self.assertEqual((await self.execute(identifier))['status'], 'signed')
        self.assertEqual(self.posts, ['primary.example.invalid'])

    async def test_invalid_or_disabled_proxy_is_rejected(self):
        selected = await self.proxy('disabled')
        self.store.execute('UPDATE proxies SET enabled=0 WHERE id=?', (selected,))
        for ids in ([selected], ['missing']):
            response = await self.client.put('/api/v1/settings/checkin-fallback', json={'ids': ids})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.settings().checkin_fallback_proxy_ids, [])

    async def test_delete_requires_unchecking_fallback(self):
        selected = await self.proxy('retained')
        await self.configure(selected)
        self.assertEqual((await self.client.delete(f'/api/v1/proxies/{selected}')).status_code, 409)
        await self.configure()
        self.assertEqual((await self.client.delete(f'/api/v1/proxies/{selected}')).status_code, 200)

    async def test_backup_remaps_proxy_ids(self):
        selected = await self.proxy('portable')
        await self.configure(selected)
        document = await export_document(self.client)
        await self.configure()
        self.assertEqual((await self.client.delete(f'/api/v1/proxies/{selected}')).status_code, 200)
        response = await self.client.post('/api/v1/backup/restore', json=document)
        self.assertEqual(response.status_code, 200, response.text)
        restored = self.store.settings().checkin_fallback_proxy_ids
        self.assertEqual(len(restored), 1)
        self.assertNotEqual(restored[0], selected)
        self.assertIsNotNone(self.store.one('SELECT id FROM proxies WHERE id=?', (restored[0],)))


if __name__ == '__main__':
    unittest.main(defaultTest=['CheckinFallbackTests'], verbosity=2)
