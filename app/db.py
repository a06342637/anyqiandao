import json
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal

from app.crypto import Vault
from app.checkin_state import DailyCheckinState, observe_balance
from app.schemas import RuntimeSettings

SCHEMA_VERSION = 9


def checkin_earned(row):
    if row['code'] != 'signed' or row['balance_source'] != 'live':
        return None
    if 'reward_amount' in row.keys() and row['reward_amount'] is not None:
        return Decimal(str(row['reward_amount']))
    if row['quota_before'] is None or row['quota_after'] is None:
        return None
    gain = Decimal(str(row['quota_after'])) - Decimal(str(row['quota_before']))
    if row['used_before'] is not None and row['used_after'] is not None:
        if row['used_after'] < row['used_before']:
            return None
        gain += Decimal(str(row['used_after'])) - Decimal(str(row['used_before']))
    return max(Decimal(0), gain)


def balance_view(row):
    if row is None:
        return None
    result = {key: row[key] for key in ('code', 'quota_before', 'quota_after', 'used_before', 'used_after', 'created', 'balance_source')}
    result['delta'] = round(row['quota_after'] - row['quota_before'], 4) if row['quota_before'] is not None and row['quota_after'] is not None else None
    earned = checkin_earned(row)
    result['earned'] = round(float(earned), 4) if earned is not None else None
    return result


SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (
 id TEXT PRIMARY KEY, login_hash TEXT UNIQUE NOT NULL, login_enc TEXT NOT NULL,
 result_enc TEXT, validity TEXT NOT NULL DEFAULT 'unknown', message TEXT NOT NULL DEFAULT '',
 last_validated REAL, last_extracted REAL, last_checkin REAL, checkin_status TEXT,
 quota REAL, proxy_id TEXT, created REAL NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS proxies (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, config_enc TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
 status TEXT NOT NULL DEFAULT 'unknown', message TEXT NOT NULL DEFAULT '', latency REAL,
 trusted_key TEXT, candidate_key TEXT, candidate_fingerprint TEXT, tested_at REAL,
 failed_until REAL NOT NULL DEFAULT 0, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS schedules (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, interval_minutes INTEGER NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1, next_run REAL NOT NULL, last_run REAL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS schedule_accounts (
 schedule_id TEXT REFERENCES schedules(id) ON DELETE CASCADE,
 account_id TEXT REFERENCES accounts(id) ON DELETE CASCADE,
 PRIMARY KEY(schedule_id, account_id)
);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, account_id TEXT REFERENCES accounts(id) ON DELETE CASCADE,
 proxy_id TEXT REFERENCES proxies(id) ON DELETE CASCADE, kind TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', source TEXT NOT NULL, payload_enc TEXT,
 dedupe_key TEXT UNIQUE, message TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
 started REAL, finished REAL
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, created);
CREATE INDEX IF NOT EXISTS jobs_account ON jobs(account_id, kind, status);
CREATE TABLE IF NOT EXISTS logs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, account_id TEXT, kind TEXT NOT NULL,
 level TEXT NOT NULL, message TEXT NOT NULL, created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS logs_created ON logs(created);
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, expires REAL NOT NULL);
PRAGMA user_version=1;
'''


class Store:
    def __init__(self, data_dir, vault: Vault):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / 'assistant.sqlite3'
        self.vault = vault
        existing = self.path.exists() and self.path.stat().st_size > 0
        with self.connection() as connection:
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError('数据库版本比当前程序新，请恢复匹配的程序版本')
            if existing:
                try:
                    sentinel = connection.execute("SELECT value FROM meta WHERE key='key_check'").fetchone()
                    if not sentinel:
                        raise ValueError('Missing key sentinel')
                    if vault.open(sentinel['value'], 'key_check') != 'any-assistant':
                        raise ValueError('Invalid key')
                except Exception as error:
                    raise RuntimeError('数据库解密失败：请恢复原加密密钥，现有数据未被清除') from error
            connection.execute('PRAGMA journal_mode=WAL')
            if version < 1:
                connection.executescript(SCHEMA)
            if version < 2:
                connection.executescript('''
                    CREATE TABLE IF NOT EXISTS exports (
                        token_hash TEXT PRIMARY KEY, session_hash TEXT NOT NULL,
                        selection_enc TEXT NOT NULL, expires REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS accounts_order ON accounts(created);
                    CREATE INDEX IF NOT EXISTS accounts_validity ON accounts(validity,created);
                    PRAGMA user_version=2;
                ''')
            if version < 3:
                connection.executescript('''
                    ALTER TABLE accounts ADD COLUMN note TEXT NOT NULL DEFAULT '';
                    ALTER TABLE accounts ADD COLUMN position INTEGER NOT NULL DEFAULT 0;
                    UPDATE accounts SET position=rowid WHERE position=0;
                    ALTER TABLE logs ADD COLUMN category TEXT NOT NULL DEFAULT '';
                    UPDATE logs SET category=CASE WHEN level='error' THEN 'error' WHEN kind IN ('checkin','script') THEN 'checkin' WHEN kind='proxy_test' THEN 'proxy' ELSE kind END WHERE category='';
                    CREATE TABLE IF NOT EXISTS checkins (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT REFERENCES accounts(id) ON DELETE CASCADE,
                        job_id TEXT, code TEXT NOT NULL, quota_before REAL, quota_after REAL, created REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS checkins_created ON checkins(created);
                    CREATE INDEX IF NOT EXISTS checkins_account ON checkins(account_id, created);
                    CREATE INDEX IF NOT EXISTS accounts_position ON accounts(position, created);
                    PRAGMA user_version=3;
                ''')
            if version < 4:
                connection.executescript('''
                    ALTER TABLE checkins ADD COLUMN balance_source TEXT NOT NULL DEFAULT 'legacy';
                    CREATE INDEX IF NOT EXISTS checkins_job ON checkins(job_id);
                    PRAGMA user_version=4;
                ''')
            if version < 5:
                connection.executescript('''
                    ALTER TABLE jobs ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0;
                    PRAGMA user_version=5;
                ''')
            if version < 6:
                columns = {row['name'] for row in connection.execute('PRAGMA table_info(checkins)')}
                for column in ('used_before', 'used_after'):
                    if column not in columns:
                        connection.execute(f'ALTER TABLE checkins ADD COLUMN {column} REAL')
                connection.execute('PRAGMA user_version=6')
            if version < 7:
                connection.executescript('''
                    CREATE TABLE IF NOT EXISTS console_results (
                        job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
                        account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                        result_enc TEXT NOT NULL, credential_hash TEXT NOT NULL, expires REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS console_results_expires ON console_results(expires);
                    PRAGMA user_version=7;
                ''')
            if version < 8:
                columns = {row['name'] for row in connection.execute('PRAGMA table_info(accounts)')}
                if 'checkin_state_enc' not in columns:
                    connection.execute('ALTER TABLE accounts ADD COLUMN checkin_state_enc TEXT')
                columns = {row['name'] for row in connection.execute('PRAGMA table_info(checkins)')}
                if 'reward_amount' not in columns:
                    connection.execute('ALTER TABLE checkins ADD COLUMN reward_amount REAL')
                connection.execute('PRAGMA user_version=8')
            if version < 9:
                if 'checkin_route' not in {row['name'] for row in connection.execute('PRAGMA table_info(accounts)')}:
                    connection.execute('ALTER TABLE accounts ADD COLUMN checkin_route TEXT NOT NULL DEFAULT \'{"mode":"inherit"}\'')
                if 'network_route' not in {row['name'] for row in connection.execute('PRAGMA table_info(schedules)')}:
                    connection.execute('ALTER TABLE schedules ADD COLUMN network_route TEXT NOT NULL DEFAULT \'{"mode":"inherit"}\'')
                connection.execute('PRAGMA user_version=9')
            if not existing:
                connection.execute('INSERT INTO meta VALUES (?, ?)', ('key_check', vault.seal('any-assistant', 'key_check')))
            connection.execute('INSERT OR IGNORE INTO meta VALUES (?, ?)', ('settings', RuntimeSettings().model_dump_json()))
            connection.execute('INSERT OR IGNORE INTO meta VALUES (?, ?)', ('queue_paused', '0'))
            connection.execute('INSERT OR IGNORE INTO meta VALUES (?, ?)', ('pause_reason', ''))
            settings = json.loads(connection.execute("SELECT value FROM meta WHERE key='settings'").fetchone()['value'])
            if 'queue_retention_days' not in settings:
                settings['queue_retention_days'] = settings.get('log_retention_days', 7)
                connection.execute("UPDATE meta SET value=? WHERE key='settings'", (json.dumps(settings),))
            if version < 9:
                settings['max_concurrency'] = settings['checkin_concurrency'] = 1
                settings['proxy_mode'] = 'direct'
                connection.execute("UPDATE meta SET value=? WHERE key='settings'", (RuntimeSettings.model_validate(settings).model_dump_json(),))

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA secure_delete=ON')
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        with self.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            yield connection

    def one(self, query, values=()):
        with self.connection() as connection:
            row = connection.execute(query, values).fetchone()
            return dict(row) if row else None

    def all(self, query, values=()):
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(query, values).fetchall()]

    def execute(self, query, values=()):
        with self.connection() as connection:
            return connection.execute(query, values).rowcount

    def meta(self, key, default=''):
        row = self.one('SELECT value FROM meta WHERE key=?', (key,))
        return row['value'] if row else default

    def set_meta(self, key, value):
        self.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)', (key, str(value)))

    def settings(self):
        values = json.loads(self.meta('settings'))
        values.setdefault('queue_retention_days', values.get('log_retention_days', 7))
        return RuntimeSettings.model_validate(values)

    def enough_space(self):
        return shutil.disk_usage(self.path.parent).free >= 64 * 1024 * 1024

    def log(self, kind, message, *, level='info', job_id=None, account_id=None, category=None):
        cleaned = ''.join(character for character in str(message) if character.isprintable() or character == '\n')[:4000]
        if not category:
            category = 'error' if level == 'error' else 'checkin' if kind in ('checkin', 'script') else 'proxy' if kind == 'proxy_test' else kind
        self.execute('INSERT INTO logs(job_id, account_id, kind, level, message, created, category) VALUES (?, ?, ?, ?, ?, ?, ?)',
                     (job_id, account_id, kind, level, cleaned, time.time(), category))

    def record_checkin(self, account_id, job_id, code, quota_before, quota_after, connection=None, *, used_before=None, used_after=None, reward_amount=None):
        def insert(current):
            if job_id and current.execute('SELECT 1 FROM checkins WHERE job_id=? LIMIT 1', (job_id,)).fetchone():
                return
            current.execute('INSERT INTO checkins(account_id, job_id, code, quota_before, quota_after, created, balance_source, used_before, used_after, reward_amount) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                            (account_id, job_id, code, quota_before, quota_after, time.time(), 'live', used_before, used_after, reward_amount))
        if connection is not None:
            insert(connection)
        else:
            with self.transaction() as current:
                insert(current)

    def checkin_balance(self, account_id=None, job_id=None):
        column, value = ('job_id', job_id) if job_id else ('account_id', account_id)
        row = self.one(f'SELECT code,quota_before,quota_after,used_before,used_after,created,balance_source,reward_amount FROM checkins WHERE {column}=? AND created<=? ORDER BY created DESC,id DESC LIMIT 1', (value, time.time()))
        return balance_view(row)

    def daily_state(self, row):
        if not row['checkin_state_enc'] or not row['result_enc']:
            return None
        state = DailyCheckinState.model_validate(self.vault.open(row['checkin_state_enc'], f'checkin-state:{row["id"]}'))
        credentials = self.vault.open(row['result_enc'], f'result:{row["id"]}')
        return state if state.api_user == str(credentials.get('api_user')) else None

    def observe_account(self, account_id, api_user, quota, used_quota=None, *, at=None, confirm=False, evidence='receipt', connection=None):
        now = time.time() if at is None else at
        def save(current):
            row = current.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
            if not row or not row['result_enc']:
                return None
            credentials = self.vault.open(row['result_enc'], f'result:{account_id}')
            if str(credentials.get('api_user')) != str(api_user):
                return None
            previous = self.daily_state(row)
            state = observe_balance(previous, api_user, quota, used_quota, now, confirm=confirm, evidence=evidence)
            if previous and state.observed_at < previous.observed_at:
                return previous
            current.execute('UPDATE accounts SET checkin_state_enc=? WHERE id=?',
                            (self.vault.seal(state.model_dump(), f'checkin-state:{account_id}'), account_id))
            return state
        if connection is not None:
            return save(connection)
        with self.transaction() as current:
            return save(current)

    def account(self, account_id):
        account = self.one('SELECT * FROM accounts WHERE id=?', (account_id,))
        if account:
            account['login'] = self.vault.open(account['login_enc'], f'login:{account_id}')
            account['result'] = self.vault.open(account['result_enc'], f'result:{account_id}') if account['result_enc'] else None
        return account

    def public_account(self, row):
        login = self.vault.open(row['login_enc'], f'login:{row["id"]}')
        return {key: row[key] for key in ('id', 'validity', 'message', 'last_validated', 'last_extracted', 'last_checkin',
                                         'checkin_status', 'quota', 'created', 'updated', 'note', 'position')} | {
            'username': login['username'], 'has_password': bool(login.get('password')), 'has_result': bool(row['result_enc']),
            'credential_source': 'password' if login.get('password') else 'session' if row['result_enc'] else 'missing',
            'last_balance': self.checkin_balance(account_id=row['id']), 'checkin_route': json.loads(row['checkin_route'])}

    def selection_where(self, selection, *, exportable=False):
        clauses, values = [], []
        if not selection.all_matching:
            identifiers = list(dict.fromkeys(selection.ids))
            if not identifiers:
                return '0', []
            clauses.append('id IN (' + ','.join('?' for identifier in identifiers) + ')')
            values.extend(identifiers)
        if selection.exclude_ids:
            clauses.append('id NOT IN (' + ','.join('?' for identifier in selection.exclude_ids) + ')')
            values.extend(selection.exclude_ids)
        if selection.validity:
            clauses.append('validity=?')
            values.append(selection.validity)
        if selection.only_extracted or exportable:
            clauses.append('result_enc IS NOT NULL')
        if exportable:
            clauses.append("validity!='invalid'")
        return ' AND '.join(clauses) or '1', values

    def iter_selected(self, connection, selection, *, columns='*', exportable=False):
        where, values = self.selection_where(selection, exportable=exportable)
        cursor = connection.execute('SELECT ' + columns + ' FROM accounts WHERE ' + where + ' ORDER BY position,created,rowid', values)
        while rows := cursor.fetchmany(100):
            yield from rows

    def selection_count(self, selection, *, exportable=False, connection=None):
        where, values = self.selection_where(selection, exportable=exportable)
        query = 'SELECT COUNT(*) AS count FROM accounts WHERE ' + where
        if connection is not None:
            return connection.execute(query, values).fetchone()['count']
        return self.one(query, values)['count']

    def enqueue(self, kind, account_id=None, *, proxy_id=None, source='manual', payload=None, dedupe_key=None, connection=None):
        def insert(current):
            if dedupe_key:
                existing = current.execute('SELECT id FROM jobs WHERE dedupe_key=?', (dedupe_key,)).fetchone()
                if existing:
                    return existing['id'], False
            existing = current.execute("SELECT id FROM jobs WHERE kind=? AND account_id IS ? AND proxy_id IS ? AND status IN ('pending','running')",
                                       (kind, account_id, proxy_id)).fetchone()
            if existing:
                return existing['id'], False
            job_id = uuid.uuid4().hex
            encoded = self.vault.seal(payload, f'job:{job_id}') if payload else None
            current.execute('INSERT INTO jobs(id,account_id,proxy_id,kind,source,payload_enc,dedupe_key,created) VALUES (?,?,?,?,?,?,?,?)',
                            (job_id, account_id, proxy_id, kind, source, encoded, dedupe_key, time.time()))
            return job_id, True
        if connection is not None:
            return insert(connection)
        with self.transaction() as current:
            return insert(current)

    def recover(self):
        now = time.time()
        with self.transaction() as connection:
            interrupted = connection.execute("SELECT id,account_id FROM jobs WHERE kind='checkin' AND status='running'").fetchall()
            for job in interrupted:
                if job['account_id']:
                    self.record_checkin(job['account_id'], job['id'], 'uncertain', None, None, connection=connection)
                    connection.execute("UPDATE accounts SET checkin_status='uncertain',last_checkin=?,updated=? WHERE id=?", (now, now, job['account_id']))
                    connection.execute('INSERT INTO logs(job_id,account_id,kind,level,message,created,category) VALUES (?,?,?,?,?,?,?)',
                                       (job['id'], job['account_id'], 'checkin', 'warning', '服务中断，签到结果未确认，不自动重发；已计入待确认统计', now, 'checkin'))
            connection.execute("UPDATE jobs SET status='uncertain',message='服务中断，签到结果未确认，不自动重发',payload_enc=NULL,finished=? WHERE kind='checkin' AND status='running'", (now,))
            connection.execute("UPDATE jobs SET status='pending',started=NULL,message='服务恢复，等待重新执行' WHERE status='running'")
            connection.execute('DELETE FROM sessions WHERE expires<?', (now,))

    def stored_password(self, account_id, connection=None):
        row = (connection.execute if connection is not None else self.one)('SELECT login_enc FROM accounts WHERE id=?', (account_id,))
        if connection is not None:
            row = row.fetchone()
        return self.vault.open(row['login_enc'], f'login:{account_id}').get('password') if row else None

    def auto_schedule(self, connection, interval_minutes):
        """Return the id of the default schedule that new accounts join automatically, creating it when missing."""
        row = connection.execute("SELECT value FROM meta WHERE key='auto_schedule_id'").fetchone()
        if row and connection.execute('SELECT id FROM schedules WHERE id=?', (row['value'],)).fetchone():
            return row['value']
        schedule_id = uuid.uuid4().hex
        now = time.time()
        connection.execute('INSERT INTO schedules(id,name,interval_minutes,enabled,next_run,created) VALUES (?,?,?,?,?,?)',
                           (schedule_id, '自动签到（默认）', interval_minutes, 1, now + interval_minutes * 60, now))
        connection.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('auto_schedule_id', schedule_id))
        return schedule_id

    def cleanup(self):
        now = time.time()
        settings = self.settings()
        cutoff = now - settings.log_retention_days * 86400
        with self.transaction() as connection:
            removed = connection.execute('DELETE FROM logs WHERE created<? AND (job_id IS NULL OR job_id NOT IN (SELECT id FROM jobs WHERE status IN (?,?)))',
                                         (cutoff, 'pending', 'running')).rowcount
            jobs_removed = connection.execute("DELETE FROM jobs WHERE COALESCE(finished,created)<? AND status NOT IN ('pending','running')",
                                              (now - settings.queue_retention_days * 86400,)).rowcount
            connection.execute('DELETE FROM sessions WHERE expires<?', (now,))
            connection.execute('DELETE FROM exports WHERE expires<?', (now,))
            connection.execute('DELETE FROM console_results WHERE expires<?', (now,))
            stats_removed = connection.execute("DELETE FROM checkins WHERE created<? AND (job_id IS NULL OR job_id NOT IN (SELECT id FROM jobs WHERE status IN ('pending','running')))",
                                               (now - settings.stats_retention_days * 86400,)).rowcount
            connection.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('last_log_cleanup', str(now)))
        self.log('system', f'按保留策略清理了 {removed} 条运行/账号日志、{jobs_removed} 条已结束队列记录、{stats_removed} 条账号签到明细；账号、凭证及等待/运行任务不受影响')
        return {'removed': removed + jobs_removed + stats_removed, 'logs': removed, 'jobs': jobs_removed, 'checkins': stats_removed}
