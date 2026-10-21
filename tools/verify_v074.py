"""Update progress remains readable across requests and application restarts."""
import time
import unittest

import httpx

from app.main import create_app
from app.update_common import atomic_json
from tools import verify_v3 as fixtures


class UpdateProgressTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ApiTests.asyncSetUp
    asyncTearDown = fixtures.ApiTests.asyncTearDown

    async def restarted_status(self):
        restarted = create_app(self.config, self.service)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url='https://testserver', cookies=self.client.cookies) as client:
            response = await client.get('/api/v1/updates')
            self.assertEqual(response.status_code, 200)
            return response.json()['operation']

    async def test_current_progress_survives_repeated_reads_and_app_restart(self):
        moment = time.time()
        state = {'id': '1' * 32, 'version': '0.7.4', 'stage': 'building', 'message': '正在构建镜像', 'progress': 25,
                 'started_at': moment - 60, 'updated_at': moment, 'history': [{'at': moment, 'stage': 'building', 'message': '正在构建镜像'}]}
        atomic_json(self.root / 'updates/status/state.json', state)
        for _ in range(2):
            self.assertEqual((await self.client.get('/api/v1/updates')).json()['operation'], state)
        self.assertEqual(await self.restarted_status(), state)

    async def test_terminal_results_and_step_history_survive_restart(self):
        for stage in ('complete', 'failed', 'rolled_back'):
            with self.subTest(stage=stage):
                moment = time.time()
                state = {'id': '2' * 32, 'version': '0.7.4', 'stage': stage, 'message': stage, 'progress': 100,
                         'finished_at': moment, 'backup_path': '/opt/assistant/backups/synthetic-update',
                         'history': [{'at': moment - index, 'stage': 'checking', 'message': str(index)} for index in range(9)]}
                atomic_json(self.root / 'updates/status/state.json', state)
                self.assertEqual(await self.restarted_status(), state)
                await self.client.put('/api/v1/updates/source', json={'repository': 'owner/assistant'})
                self.assertEqual((await self.client.get('/api/v1/updates')).json()['operation'], state)

    async def test_pending_request_survives_restart_then_follows_executor(self):
        moment = time.time()
        atomic_json(self.root / 'updates/status/state.json', {'id': '3' * 32, 'stage': 'failed', 'progress': 100})
        request = {'id': '4' * 32, 'version': '0.7.4', 'requested_at': moment}
        atomic_json(self.root / 'updates/requests/request.json', request)
        queued = await self.restarted_status()
        self.assertEqual((queued['id'], queued['stage'], queued['progress']), (request['id'], 'queued', 0))
        atomic_json(self.root / 'updates/status/state.json', {'id': request['id'], 'stage': 'downloading', 'progress': 12})
        self.assertEqual((await self.restarted_status())['stage'], 'downloading')


if __name__ == '__main__':
    unittest.main()
