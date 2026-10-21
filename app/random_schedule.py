"""Persist daily per-account random times; never run a missed window as a burst."""
from datetime import datetime, timedelta
from random import SystemRandom

from app.checkin_state import BEIJING


def bounds(schedule, day):
    def at(value):
        hour, minute = map(int, value.split(':'))
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=BEIJING).timestamp()
    return at(schedule['window_start']), at(schedule['window_end'])


def plan_window(connection, schedule, now, *, replan=False, rng=None):
    schedule = dict(schedule)
    today = datetime.fromtimestamp(now, BEIJING).date()
    start, end = bounds(schedule, today)
    if now >= end - 1:
        today += timedelta(days=1)
        start, end = bounds(schedule, today)
    elif connection.execute(
            "SELECT SUM(d.account_id IS NULL OR d.status='planned' OR "
            "(d.status='queued' AND (j.status IN ('pending','running') OR (? AND j.status='cancelled' AND j.started IS NULL)))) AS unfinished "
            "FROM schedule_accounts a LEFT JOIN schedule_dispatches d ON d.schedule_id=a.schedule_id "
            "AND d.account_id=a.account_id AND d.day=? LEFT JOIN jobs j ON j.id=d.job_id WHERE a.schedule_id=?",
            (int(replan), today.isoformat(), schedule['id'])).fetchone()['unfinished'] == 0:
        # Once every member's daily attempt has ended, persist tomorrow's slots
        # even if today's window is still open. Never enqueue them before due_at.
        today += timedelta(days=1)
        start, end = bounds(schedule, today)
    day = today.isoformat()
    connection.execute("UPDATE schedule_dispatches SET status='missed' WHERE schedule_id=? AND status='planned' AND expires_at<=?", (schedule['id'], now))
    if replan:
        connection.execute("DELETE FROM schedule_dispatches WHERE schedule_id=? AND (status='planned' OR (status='missed' AND day=?) OR (status='queued' AND job_id IN (SELECT id FROM jobs WHERE status='cancelled' AND started IS NULL)))", (schedule['id'], day))
    rows = connection.execute('SELECT account_id FROM schedule_accounts WHERE schedule_id=? AND account_id NOT IN '
                              '(SELECT account_id FROM schedule_dispatches WHERE schedule_id=? AND day=?) ORDER BY account_id',
                              (schedule['id'], schedule['id'], day)).fetchall()
    members = [row['account_id'] for row in rows]
    rng = rng or SystemRandom()
    rng.shuffle(members)
    lower = max(start, now + 1)
    # Stratified random slots spread accounts across the whole remaining window.
    for index, identifier in enumerate(members):
        due = lower + (end - lower) * (index + rng.random()) / len(members)
        connection.execute('INSERT OR IGNORE INTO schedule_dispatches(schedule_id,account_id,day,due_at,expires_at) VALUES (?,?,?,?,?)',
                           (schedule['id'], identifier, day, due, end))
    update_next(connection, schedule, now)


def update_next(connection, schedule, now):
    row = connection.execute("SELECT MIN(due_at) FROM schedule_dispatches WHERE schedule_id=? AND status='planned' AND expires_at>?",
                             (schedule['id'], now)).fetchone()
    tomorrow = datetime.fromtimestamp(now, BEIJING).date() + timedelta(days=1)
    next_run = row[0] if row[0] is not None else bounds(schedule, tomorrow)[0]
    connection.execute('UPDATE schedules SET next_run=? WHERE id=?', (next_run, schedule['id']))


def dispatch_window(store, connection, schedule, now):
    plan_window(connection, schedule, now)
    due = connection.execute("SELECT * FROM schedule_dispatches WHERE schedule_id=? AND status='planned' AND due_at<=? ORDER BY due_at", (schedule['id'], now)).fetchall()
    dispatched = False
    for slot in due:
        if slot['expires_at'] <= now:
            connection.execute("UPDATE schedule_dispatches SET status='missed' WHERE schedule_id=? AND account_id=? AND day=?", (schedule['id'], slot['account_id'], slot['day']))
            continue
        job_id, inserted = store.enqueue('checkin', slot['account_id'], source=f'schedule:{schedule["id"]}',
                            dedupe_key=f'random:{schedule["id"]}:{slot["day"]}:{slot["account_id"]}',
                            not_after=slot['expires_at'], connection=connection)
        if not inserted:
            existing = connection.execute('SELECT dedupe_key FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not existing or existing['dedupe_key'] != f'random:{schedule["id"]}:{slot["day"]}:{slot["account_id"]}':
                connection.execute('UPDATE schedule_dispatches SET due_at=? WHERE schedule_id=? AND account_id=? AND day=?',
                                   (min(now + 10, slot['expires_at']), schedule['id'], slot['account_id'], slot['day']))
                continue
        connection.execute("UPDATE schedule_dispatches SET status='queued',job_id=? WHERE schedule_id=? AND account_id=? AND day=?",
                           (job_id, schedule['id'], slot['account_id'], slot['day']))
        dispatched = True
    if dispatched:
        connection.execute('UPDATE schedules SET last_run=? WHERE id=?', (now, schedule['id']))
    update_next(connection, schedule, now)
