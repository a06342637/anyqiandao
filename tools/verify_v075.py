"""Daily assignment rollover uses synthetic accounts and fixed Beijing times."""
import random
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from app.checkin_state import BEIJING
from app.crypto import Vault
from app.db import Store
from tools import verify_v070 as base


class AssignmentRolloverTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.ApiTests.asyncSetUp
    asyncTearDown = base.ApiTests.asyncTearDown
    create = base.RandomScheduleTests.create
    slots = base.RandomScheduleTests.slots

    def finish_day(self, slots, last_status='signed'):
        self.engine.tick_schedules(slots[-1]['due_at'] + 0.001)
        self.store.execute("UPDATE jobs SET status='signed',started=?,finished=?", (self.start, self.start + 1))
        self.store.execute('UPDATE jobs SET status=? WHERE account_id=?', (last_status, slots[-1]['account_id']))

    async def assignments(self, now, page=1, limit=20):
        with patch('app.settings_api.time.time', return_value=now), patch('app.random_schedule.SystemRandom', return_value=random.Random(31)):
            response = await self.client.get(f'/api/v1/schedules/{self.schedule}/assignments?page={page}&limit={limit}')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_completed_day_shows_tomorrow_before_window_ends_and_survives_restart(self):
        slots = await self.create(3)
        self.finish_day(slots)
        now = slots[-1]['due_at'] + 2
        tomorrow = (datetime.fromtimestamp(now, BEIJING).date() + timedelta(days=1)).isoformat()
        result = await self.assignments(now)
        self.assertEqual((result['day'], result['next_day'], result['total']), (tomorrow, True, 3))
        self.assertTrue(all(r['status'] == 'planned' and self.start + 86400 <= r['due_at'] < self.end + 86400 for r in result['items']))
        self.assertEqual(self.store.one('SELECT next_run FROM schedules')['next_run'], result['items'][0]['due_at'])
        Store(self.config.data_dir, Vault(self.config.key))
        self.assertEqual((await self.assignments(now + 10))['items'], result['items'])
        self.engine.tick_schedules(now + 20)
        self.assertEqual(self.store.one('SELECT COUNT(*) n FROM jobs')['n'], 3)

    async def test_pending_and_running_tasks_keep_today_until_last_task_finishes(self):
        slots = await self.create(3)
        for status in ('pending', 'running'):
            with self.subTest(status=status):
                self.finish_day(slots, status)
                result = await self.assignments(slots[-1]['due_at'] + 2)
                self.assertFalse(result['next_day'])
                self.assertEqual(result['total'], 3)
                self.assertIn(status, {r['status'] for r in result['items']})
        self.store.execute("UPDATE jobs SET status='signed'")
        self.assertTrue((await self.assignments(slots[-1]['due_at'] + 3))['next_day'])

    async def test_unexecuted_slots_keep_today_and_completed_rows_on_same_day(self):
        slots = await self.create(3)
        self.engine.tick_schedules(slots[0]['due_at'] + 0.001)
        self.store.execute("UPDATE jobs SET status='signed'")
        result = await self.assignments(slots[0]['due_at'] + 2)
        self.assertFalse(result['next_day'])
        self.assertEqual({r['status'] for r in result['items']}, {'signed', 'planned'})

    async def test_missed_day_shows_only_next_day_and_paginated_count(self):
        await self.create(25)
        result = await self.assignments(self.end + 1, page=2)
        self.assertTrue(result['next_day'])
        self.assertEqual((result['total'], len(result['items'])), (25, 5))
        self.assertTrue(all(r['status'] == 'planned' for r in result['items']))
        self.assertEqual(self.store.one('SELECT COUNT(*) n FROM jobs')['n'], 0)

    async def test_paused_completed_schedule_does_not_create_new_slots(self):
        slots = await self.create(3)
        self.finish_day(slots)
        self.store.execute('UPDATE schedules SET enabled=0')
        result = await self.assignments(slots[-1]['due_at'] + 2)
        self.assertFalse(result['next_day'])
        self.assertEqual(len(self.slots()), 3)

    async def test_removed_members_and_past_days_are_not_in_next_day_count(self):
        slots = await self.create(3)
        self.finish_day(slots)
        self.store.execute('DELETE FROM schedule_accounts WHERE account_id=?', (slots[0]['account_id'],))
        result = await self.assignments(slots[-1]['due_at'] + 2)
        self.assertEqual(result['total'], 2)
        self.assertNotIn(slots[0]['account_id'], {r['account_id'] for r in result['items']})
        result = await self.assignments(self.now + 86400)
        self.assertFalse(result['next_day'])
        self.assertEqual(result['total'], 2)

    async def test_next_day_dispatches_only_when_due_and_replan_preserves_completed_day(self):
        slots = await self.create(3)
        self.finish_day(slots)
        now = slots[-1]['due_at'] + 2
        with patch('app.settings_api.time.time', return_value=now):
            response = await self.client.post(f'/api/v1/schedules/{self.schedule}/run')
        self.assertEqual(response.status_code, 200)
        upcoming = await self.assignments(now)
        self.assertTrue(upcoming['next_day'])
        self.engine.tick_schedules(upcoming['items'][0]['due_at'] + 0.001)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM jobs WHERE status='pending'")['n'], 1)
        self.assertEqual(self.store.one('SELECT COUNT(*) n FROM schedule_dispatches WHERE day=?', (slots[0]['day'],))['n'], 3)


    async def test_resume_replans_cancelled_unstarted_accounts_within_today(self):
        slots = await self.create(1)
        now = slots[0]['due_at'] + 0.001
        self.engine.tick_schedules(now)
        with patch('app.settings_api.time.time', return_value=now):
            await self.client.post(f'/api/v1/schedules/{self.schedule}/pause')
            response = await self.client.post(f'/api/v1/schedules/{self.schedule}/resume')
        self.assertEqual(response.status_code, 200)
        result = await self.assignments(now)
        self.assertFalse(result['next_day'])
        self.assertEqual(result['items'][0]['status'], 'planned')
        self.assertTrue(now < result['items'][0]['due_at'] < self.end)


if __name__ == '__main__':
    unittest.main(verbosity=2)
