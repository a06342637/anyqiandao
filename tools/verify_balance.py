"""Balance refresh selection, routing and failure checks using synthetic accounts only."""
import json
import unittest
from unittest.mock import AsyncMock, patch

from app.errors import TaskError
from tools import verify_v3 as base
from tools import verify_serial_routes as routes


class BalanceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.ApiTests.asyncSetUp
    asyncTearDown = routes.SerialRouteTests.asyncTearDown
    proxy = routes.SerialRouteTests.proxy
    routes = routes.SerialRouteTests.routes
    perform = base.ApiTests.perform

    async def test_bulk_all_pages_respects_exclusions_and_skips_missing_credentials(self):
        identifiers = [base.account(self.store, f'balance-{index}', password='' if index % 2 else 'synthetic') for index in range(13)]
        self.store.execute('UPDATE accounts SET result_enc=NULL WHERE id=?', (identifiers[-1],))
        payload = {'action': 'balance', 'all_matching': True, 'exclude_ids': [identifiers[0]]}
        response = await self.client.post('/api/v1/accounts/actions', json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual((result['queued'], result['skipped']), (11, 1))
        self.assertEqual({row['account_id'] for row in self.store.all('SELECT account_id FROM jobs')}, set(identifiers[1:-1]))
        repeated = (await self.client.post('/api/v1/accounts/actions', json=payload)).json()
        self.assertEqual(repeated['queued'], 0)
        self.assertEqual(set(repeated['job_ids']), set(result['job_ids']))
        while job := self.engine.claim():
            await self.engine.process(job)
        self.assertEqual(len(self.store.all("SELECT id FROM accounts WHERE quota=10")), 11)
        self.assertEqual(self.store.account(identifiers[0])['quota'], 999)
        self.assertEqual((self.service.extracts, self.service.checkins), (0, 0))

    async def test_bulk_filter_and_empty_selection(self):
        valid = base.account(self.store, 'valid')
        invalid = base.account(self.store, 'invalid', validity='invalid')
        response = await self.client.post('/api/v1/accounts/actions', json={'action': 'balance', 'all_matching': True, 'validity': 'invalid'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.all('SELECT account_id FROM jobs'), [{'account_id': invalid}])
        self.assertEqual(self.store.account(valid)['quota'], 999)
        response = await self.client.post('/api/v1/accounts/actions', json={'action': 'balance', 'ids': []})
        self.assertEqual(response.status_code, 400)

    async def test_balance_uses_own_route_and_can_switch_back_to_direct(self):
        chosen = await self.proxy('balance')
        validation = await self.proxy('validation')
        await self.routes(balance={'mode': 'proxy', 'proxy_id': chosen}, validate={'mode': 'proxy', 'proxy_id': validation})
        identifier = base.account(self.store, password='')
        before = self.store.account(identifier)
        self.service.validate = AsyncMock(return_value={'quota': 0, 'profile': {'used_quota': 500000}})
        with patch('app.engine.ProxyBridge', routes.FakeBridge):
            job = await self.perform(identifier, 'balance')
        self.assertEqual(job['status'], 'success')
        self.assertEqual(self.service.validate.call_args.args[1].config['host'], 'balance.example.invalid')
        after = self.store.account(identifier)
        self.assertEqual(after['quota'], 0)
        for key in ('result_enc', 'last_extracted', 'last_checkin', 'checkin_status'):
            self.assertEqual(after[key], before[key])
        self.assertEqual(self.store.all('SELECT * FROM checkins'), [])
        self.assertEqual((self.service.extracts, self.service.checkins), (0, 0))
        status = (await self.client.get('/api/v1/jobs/status', params={'ids': job['id']})).json()['items'][0]
        self.assertEqual((status['kind'], status['quota']), ('balance', 0))
        logs = (await self.client.get('/api/v1/logs', params={'category': 'balance'})).json()['items']
        self.assertTrue(any('检测余额 · 代理' in entry['message'] for entry in logs))
        await self.routes(balance={'mode': 'direct'})
        self.service.validate.reset_mock()
        self.assertEqual((await self.perform(identifier, 'balance'))['status'], 'success')
        self.assertIsNone(self.service.validate.call_args.args[1])
        self.assertEqual(self.store.settings().operation_routes.validation.proxy_id, validation)

    async def test_failure_retains_balance_and_never_logs_in_or_checks_in(self):
        chosen = await self.proxy('failure')
        await self.routes(balance={'mode': 'proxy', 'proxy_id': chosen})
        settings = self.store.settings().model_copy(update={'auto_reextract': True, 'auto_checkin': True})
        self.store.set_meta('settings', settings.model_dump_json())
        cases = [('invalid', TaskError('invalid', 'synthetic expired credential')),
                 ('network_error', TaskError('network_error', 'synthetic network failure')),
                 ('upstream_error', {'quota': None})]
        for code, response in cases:
            with self.subTest(code=code):
                identifier = base.account(self.store, code)
                before = self.store.account(identifier)
                self.service.validate = AsyncMock(side_effect=response) if isinstance(response, Exception) else AsyncMock(return_value=response)
                with patch('app.engine.ProxyBridge', routes.FakeBridge):
                    job = await self.perform(identifier, 'balance')
                self.assertEqual(job['status'], code)
                self.assertEqual(self.service.validate.await_count, 1)
                after = self.store.account(identifier)
                self.assertEqual(after['quota'], 999)
                self.assertEqual(after['result_enc'], before['result_enc'])
                self.assertEqual(after['last_checkin'], before['last_checkin'])
        self.assertEqual((self.service.extracts, self.service.checkins), (0, 0))
        self.assertEqual(self.store.all("SELECT id FROM jobs WHERE kind!='balance' AND status='pending'"), [])

    async def test_proxy_network_failure_retries_then_uses_direct(self):
        chosen = await self.proxy('retry')
        await self.routes(balance={'mode': 'proxy', 'proxy_id': chosen})
        async def read_balance(credentials, route, settings):
            if route:
                raise TaskError('network_error', 'synthetic failure', retry_proxy=True)
            return {'quota': 123.4567}
        self.service.validate = AsyncMock(side_effect=read_balance)
        with patch('app.engine.ProxyBridge', routes.FakeBridge), patch('app.engine.asyncio.sleep', new=AsyncMock()):
            job = await self.perform(base.account(self.store), 'balance')
        self.assertEqual(job['status'], 'success')
        self.assertEqual([call.args[1] is None for call in self.service.validate.call_args_list], [False] * 5 + [True])
        self.assertEqual(self.store.account(job['account_id'])['quota'], 123.4567)

    async def test_old_settings_default_to_direct_and_route_validation_applies(self):
        chosen = await self.proxy('existing')
        await self.routes(validate={'mode': 'proxy', 'proxy_id': chosen})
        old = self.store.settings().model_dump()
        del old['operation_routes']['balance']
        self.store.set_meta('settings', json.dumps(old))
        current = (await self.client.get('/api/v1/settings')).json()['settings']['operation_routes']
        self.assertEqual(current['balance']['mode'], 'direct')
        self.assertEqual(current['validate']['proxy_id'], chosen)
        for route, expected in [({'mode': 'inherit'}, 422), ({'mode': 'proxy', 'proxy_id': 'missing'}, 400)]:
            response = await self.client.put('/api/v1/settings/routes', json={**current, 'balance': route})
            self.assertEqual(response.status_code, expected, response.text)
        await self.routes(balance={'mode': 'proxy', 'proxy_id': chosen}, validate={'mode': 'direct'})
        self.assertEqual((await self.client.delete(f'/api/v1/proxies/{chosen}')).status_code, 409)


if __name__ == '__main__':
    unittest.main(verbosity=2)
