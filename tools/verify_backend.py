import asyncio
import hashlib
import json
import secrets
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from argon2 import PasswordHasher

from app.config import Config
from app.crypto import Vault
from app.db import Store
from app.engine import Engine
from app.errors import TaskError
from app.main import create_app
from app.proxy import parse_proxy
from app.responses import checkin_result, is_waf_challenge, profile_result
from app.schemas import RuntimeSettings


ROOT = Path(__file__).resolve().parents[1]
checks = []


def check(condition, label):
    assert condition, label
    checks.append(label)


class FakeService:
    def __init__(self):
        self.active = 0
        self.peak = 0
        self.calls = []

    async def extract(self, username, password, route, settings):
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.calls.append(username)
        try:
            await asyncio.sleep(0.001)
            if username == 'fixture_missing':
                return {'api_user': '1'}
            if username == 'fixture_bad':
                raise TaskError('invalid_credentials', '合成测试：账号错误')
            return {'session': 'synthetic-cookie-' + username, 'api_user': str(1000 + len(self.calls)), 'quota': 1.0}
        finally:
            self.active -= 1

    async def validate(self, credentials, route, settings):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.02)
            if credentials['session'].endswith('fixture_0'):
                raise TaskError('invalid', '合成测试：凭证已失效')
            return {'quota': 2.0, 'profile': {'id': int(credentials['api_user'])}}
        finally:
            self.active -= 1

    async def checkin(self, credentials, route, settings, *, before_submit=None, on_submit=None):
        await self.validate(credentials, route, settings)
        return {'code': 'signed', 'message': '合成测试：签到成功', 'quota': 3.0, 'quota_before': 2.0, 'quota_after': 3.0, 'logs': ['合成脚本测试，无外部请求']}


async def wait_empty(store):
    for attempt in range(4000):
        count = store.one("SELECT COUNT(*) AS count FROM jobs WHERE status IN ('pending','running')")['count']
        if not count:
            return
        await asyncio.sleep(0.02)
    raise AssertionError('Queue did not drain')


async def verify():
    password = 'synthetic-only-admin-password-123456'
    account_password = '  synthetic----,\r\n"中文😃!  '
    key = secrets.token_bytes(32)
    (ROOT / '.local').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='backend-qa-', dir=ROOT / '.local') as directory:
        config = Config(key, PasswordHasher().hash(password), Path(directory), start_worker=False)
        service = FakeService()
        app = create_app(config, service)
        store, engine = app.state.store, app.state.engine
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=config.public_url) as client:
            check((await client.get('/api/v1/accounts')).status_code == 401, 'anonymous account access denied')
            response = await client.post('/api/v1/auth/login', json={'username': 'admin', 'password': password})
            check(response.status_code == 200, 'administrator login')
            check('httponly' in response.headers['set-cookie'].lower() and 'samesite=strict' in response.headers['set-cookie'].lower(), 'session cookie protections')
            csrf = response.json()['csrf_token']
            settings = RuntimeSettings(proxy_mode='direct', account_gap=0).model_dump()
            check((await client.put('/api/v1/settings', json=settings)).status_code == 403, 'CSRF mutation denied')
            client.headers['X-CSRF-Token'] = csrf
            check((await client.put('/api/v1/settings', json=settings, headers={'Origin': 'https://evil.invalid'})).status_code == 403, 'cross origin denied')
            oversized = await client.post('/api/v1/accounts/import', content=b'x' * 1048577)
            check(oversized.status_code == 413, 'request body memory limit')
            bad = await client.post('/api/v1/accounts/import', json={'import_id': 'bad', 'accounts': [{'row_id': '1', 'username': 'fixture', 'password': account_password, 'unexpected': True}]})
            check(bad.status_code == 422 and account_password not in bad.text, 'unknown import fields rejected without leaking input')
            rows = [{'row_id': str(index), 'username': f'fixture_{index}', 'password': account_password} for index in range(103)]
            rows.extend([{'row_id': 'missing', 'username': 'fixture_missing', 'password': account_password}, {'row_id': 'bad', 'username': 'fixture_bad', 'password': account_password}])
            identifiers = []
            for offset in range(0, len(rows), 100):
                response = await client.post('/api/v1/accounts/import', json={'import_id': 'fixture-import', 'accounts': rows[offset:offset + 100]})
                check(response.status_code == 200, 'chunk import ' + str(offset))
                identifiers.extend(response.json()['account_ids'])
            repeated = await client.post('/api/v1/accounts/import', json={'import_id': 'fixture-import', 'accounts': rows[:100]})
            check(repeated.json()['queued'] == 0 and repeated.json()['skipped'] == 100, 'idempotent chunk retry')
            pending = store.one("SELECT * FROM jobs WHERE status='pending' ORDER BY created LIMIT 1")
            check(store.vault.open(pending['payload_enc'], f'job:{pending["id"]}')['password'] == account_password, 'special password bytes preserved')
            store.set_meta('queue_paused', '1')
            check(engine.claim() is None, 'paused queue does not start browser tasks')
            pending = store.one('SELECT * FROM jobs WHERE id=?', (pending['id'],))
            check(pending['status'] == 'pending' and pending['payload_enc'], 'waiting for proxies retains encrypted input')
            check((await client.put('/api/v1/settings', json=settings)).status_code == 200, 'explicit direct mode setting')
            await client.post('/api/v1/queue/resume')
            await engine.start()
            second = Engine(store, service)
            try:
                await second.start()
                raise AssertionError('Second worker was not rejected')
            except RuntimeError:
                check(True, 'exclusive global worker lock')
            await wait_empty(store)
            await engine.stop()
            check(service.peak == 1, 'all browser operations share one global serial execution slot')
            first_extractions = list(dict.fromkeys(service.calls))
            check(len(first_extractions) == 105 and service.calls.count('fixture_0') == 2, 'imports run once and an expired first check-in retries extraction once')
            check(first_extractions[:103] == [f'fixture_{index}' for index in range(103)], 'FIFO import order preserved independently of automatic credential recovery')
            check(store.one('SELECT COUNT(*) AS count FROM jobs WHERE payload_enc IS NOT NULL')['count'] == 0, 'terminal jobs drop their payload copy')
            check(store.stored_password(identifiers[0]) == account_password, 'account password kept encrypted for later use')
            auto_id = store.meta('auto_schedule_id')
            check(bool(auto_id) and store.one('SELECT COUNT(*) AS count FROM schedule_accounts WHERE schedule_id=?', (auto_id,))['count'] == 103, 'extracted accounts auto-join the default schedule')
            check(store.one("SELECT COUNT(*) AS count FROM jobs WHERE kind='checkin' AND source='auto:extract' AND status='signed'")['count'] == 102, 'automatic first check-in after extraction')
            reextracts = store.one("SELECT COUNT(*) AS count FROM jobs WHERE kind='extract' AND source='auto:reextract'")['count']
            invalid_checkins = store.one("SELECT COUNT(*) AS count FROM jobs WHERE kind='checkin' AND status='invalid'")['count']
            check(reextracts == 0 and invalid_checkins == 1, 'a check-in that follows an automatic extraction never triggers another re-extraction')
            check(store.one("SELECT COUNT(*) AS count FROM jobs WHERE status='missing_session'")['count'] == 1, 'missing session is not false success')
            check(store.one("SELECT COUNT(*) AS count FROM jobs WHERE status='invalid_credentials'")['count'] == 1, 'account errors are distinct')
            page = await client.get('/api/v1/accounts?page=3&limit=50&validity=')
            check(page.status_code == 200 and page.json()['total'] == 105 and len(page.json()['items']) == 5, 'paginated account listing')
            check('synthetic-cookie' not in page.text and account_password not in page.text, 'list response hides credentials')
            result = await client.post('/api/v1/accounts/export', json={'all_matching': True})
            exported = result.json()
            check(result.status_code == 200 and len(exported) == 102 and '\n' not in result.text, 'export only successful entries as single line array')
            check(all(set(item) == {'cookies', 'api_user'} and set(item['cookies']) == {'session'} for item in exported), 'upstream compatible export schema')
            single = await client.post('/api/v1/accounts/export', json={'ids': [identifiers[1]]})
            check(len(single.json()) == 1 and single.json()[0] == exported[0], 'single account array and matching ID/session')
            logins = await client.post('/api/v1/accounts/export-logins', json={'ids': identifiers[:2]})
            check(logins.status_code == 200 and logins.text == f'fixture_0----{account_password}\nfixture_1----{account_password}\n', 'account----password lines export')
            login = (await client.get(f'/api/v1/accounts/{identifiers[0]}/login')).json()
            check(login == {'username': 'fixture_0', 'password': account_password}, 'single login copy endpoint')
            selected = await client.post('/api/v1/accounts/export', json={'all_matching': True, 'exclude_ids': [identifiers[1]]})
            check(len(selected.json()) == 101, 'cross page exclusion')
            ticket = (await client.post('/api/v1/accounts/export-link', json={'all_matching': True})).json()
            downloaded = await client.get(ticket['url'])
            check(len(downloaded.json()) == 102 and 'attachment' in downloaded.headers['content-disposition'], 'native streamed download')
            check((await client.get(ticket['url'])).status_code == 410, 'download ticket is single use')
            check(downloaded.headers['cache-control'] == 'no-store', 'credential responses prohibit caching')
            response = await client.post('/api/v1/accounts/actions', json={'ids': [identifiers[0]], 'action': 'validate', 'repair_invalid': False})
            check(response.json()['queued'] == 1, 'validation action queues')
            await engine.process(engine.claim())
            check(store.account(identifiers[0])['validity'] == 'invalid', 'expired credentials marked invalid')
            check((await client.get(f'/api/v1/accounts/{identifiers[0]}/credentials')).status_code == 409, 'expired credentials cannot be copied')
            check(len((await client.post('/api/v1/accounts/export', json={'all_matching': True})).json()) == 102, 'export excludes expired entries')
            with patch.object(service, 'validate', new=AsyncMock(side_effect=[TaskError('invalid', '合成测试：凭证已失效'), {'quota': 2.0}])):
                repair = (await client.post('/api/v1/accounts/actions', json={'ids': [identifiers[0]], 'action': 'validate', 'repair_invalid': True})).json()
                await engine.process(engine.claim())
            repair_job = store.one('SELECT * FROM jobs WHERE id=?', (repair['job_ids'][0],))
            check(repair_job['status'] == 'success', 'invalid credential repair is completed and tracked in the original validation job')
            check(store.account(identifiers[0])['validity'] == 'valid', 'verified re-extraction replaces expired result')
            followup = store.one("SELECT * FROM jobs WHERE kind='checkin' AND account_id=? AND status='pending'", (identifiers[0],))
            check(not followup, 'validation repair does not unexpectedly trigger a check-in')
            store.execute('UPDATE accounts SET login_enc=? WHERE id=?', (store.vault.seal({'username': 'fixture_0'}, f'login:{identifiers[0]}'), identifiers[0]))
            reextract = (await client.post('/api/v1/accounts/actions', json={'ids': [identifiers[0]], 'action': 'extract'})).json()
            check(reextract['queued'] == 0 and reextract['needs_password_count'] == 1, 're-extraction without any stored password asks for one')
            await client.post('/api/v1/accounts/actions', json={'ids': [identifiers[0]], 'action': 'extract', 'password': account_password})
            check(store.stored_password(identifiers[0]) == account_password, 'newly supplied password is stored encrypted')
            await engine.process(engine.claim())
            await engine.process(engine.claim())
            await client.post(f'/api/v1/schedules/{auto_id}/pause')
            plan = (await client.post('/api/v1/schedules', json={'name': '合成测试计划', 'interval_minutes': 1440, 'all_matching': True, 'only_extracted': True})).json()
            detail = (await client.get('/api/v1/schedules/' + plan['id'])).json()
            check(detail['account_count'] == 103 and detail['next_run'] > time.time(), 'schedule selection snapshot and delayed first run')
            now = detail['next_run'] + 3 * 86400
            engine.tick_schedules(now=now)
            count = store.one("SELECT COUNT(*) AS count FROM jobs WHERE kind='checkin' AND status='pending'")['count']
            engine.tick_schedules(now=now)
            check(count == 103 and store.one("SELECT COUNT(*) AS count FROM jobs WHERE kind='checkin' AND status='pending'")['count'] == count, 'scheduler deduplicates ticks and includes invalid credentials for automatic recovery')
            await client.post('/api/v1/schedules/' + plan['id'] + '/pause')
            check(store.one("SELECT COUNT(*) AS count FROM jobs WHERE status='pending'")['count'] == 0, 'pause schedule cancels its waiting jobs')
            await client.post('/api/v1/accounts/actions', json={'ids': [identifiers[1]], 'action': 'checkin'})
            await engine.process(engine.claim())
            check(store.account(identifiers[1])['checkin_status'] == 'signed', 'manual checkin records verified success')
            extract_job, _ = store.enqueue('extract', identifiers[1], payload={'password': account_password})
            checkin_job, _ = store.enqueue('checkin', identifiers[2])
            store.execute("UPDATE jobs SET status='running' WHERE id IN (?,?)", (extract_job, checkin_job))
            store.recover()
            check(store.one('SELECT status FROM jobs WHERE id=?', (extract_job,))['status'] == 'pending', 'restart recovers interrupted extraction')
            check(store.stored_password(identifiers[1]) == account_password, 'restart keeps stored passwords')
            check(store.one('SELECT status FROM jobs WHERE id=?', (checkin_job,))['status'] == 'uncertain', 'restart never repeats uncertain checkin')
            await client.post('/api/v1/jobs/' + extract_job + '/cancel')
            check(store.one('SELECT payload_enc FROM jobs WHERE id=?', (extract_job,))['payload_enc'] is None, 'cancel removes pending password')
            before = store.one('SELECT COUNT(*) AS count FROM accounts')['count']
            store.log('system', 'synthetic-expired-log')
            store.execute('UPDATE logs SET created=0 WHERE message=?', ('synthetic-expired-log',))
            store.cleanup()
            check(store.one('SELECT COUNT(*) AS count FROM accounts')['count'] == before and not store.one('SELECT id FROM logs WHERE message=?', ('synthetic-expired-log',)), 'log expiry preserves accounts and credentials')
            with patch.object(store, 'enough_space', return_value=False):
                check((await client.post('/api/v1/accounts/import', json={'import_id': 'disk', 'accounts': rows[:1]})).status_code == 507, 'disk pressure blocks imports without partial writes')
            check((await client.get('/api/v1/logs')).status_code == 200, 'paginated safe logs API')
            logs_page = (await client.get('/api/v1/logs?limit=5&page=2')).json()
            check(len(logs_page['items']) == 5 and logs_page['limit'] == 5 and logs_page['page'] == 2 and 'checkin' in logs_page['categories'], 'log pagination with page size and category counts')
            invalid_logs = (await client.get('/api/v1/logs?category=invalid')).json()
            check(invalid_logs['total'] >= 1 and all(entry['category'] == 'invalid' for entry in invalid_logs['items']), 'invalid-credential events have their own log category')
            status = (await client.get('/api/v1/jobs/status?ids=' + ','.join([extract_job, checkin_job, 'missing']))).json()['items']
            check({row['id'] for row in status} == {extract_job, checkin_job} and all(row['username'] for row in status), 'job status endpoint returns tracked jobs with account names')
            edited = await client.put(f'/api/v1/accounts/{identifiers[2]}', json={'username': 'fixture_2_renamed', 'password': 'new-secret-2', 'note': '测试备注'})
            check(edited.status_code == 200 and set(edited.json()['changed']) == {'username', 'password', 'note'}, 'account edit updates username, password and note')
            check(store.stored_password(identifiers[2]) == 'new-secret-2' and store.account(identifiers[2])['login']['username'] == 'fixture_2_renamed', 'edited login is stored encrypted')
            check((await client.put(f'/api/v1/accounts/{identifiers[3]}', json={'username': 'fixture_2_renamed'})).status_code == 409, 'rename to an existing username is refused')
            first_page = (await client.get('/api/v1/accounts?limit=3')).json()['items']
            await client.post('/api/v1/accounts/reorder', json={'ids': [first_page[2]['id'], first_page[0]['id'], first_page[1]['id']]})
            reordered = (await client.get('/api/v1/accounts?limit=3')).json()['items']
            check([row['id'] for row in reordered] == [first_page[2]['id'], first_page[0]['id'], first_page[1]['id']], 'drag order persists through the reorder endpoint')
            checked = store.one("SELECT COUNT(*) AS count FROM checkins")['count']
            check(checked >= 103, 'check-in statistics are recorded per attempt')
            stats = (await client.get('/api/v1/stats?range=week')).json()
            check(stats['signed'] >= 102 and stats['earned'] > 0 and len(stats['series']) == 7 and stats['accounts'] and stats['accounts'][0]['earned'] >= stats['accounts'][-1]['earned'], 'dashboard statistics: totals, 7-day series, per-account table')
            check((await client.get('/api/v1/stats?range=year')).status_code == 400, 'unknown statistics range rejected')
            schedules_page = (await client.get('/api/v1/schedules?limit=1')).json()
            check(schedules_page['total'] >= 2 and len(schedules_page['items']) == 1 and schedules_page['items'][0]['id'] == auto_id, 'schedule pagination puts the default plan first')
            archive = await client.post('/api/v1/backup')
            check(archive.status_code == 200 and 'attachment' in archive.headers['content-disposition'], 'one-click backup downloads a JSON archive')
            document = archive.json()
            check(document['app'] == 'any-signin-assistant' and len(document['accounts']) == store.one('SELECT COUNT(*) AS c FROM accounts')['c'] and next(a for a in document['accounts'] if a['username'] == 'fixture_1')['password'] == account_password and next(a for a in document['accounts'] if a['username'] == 'fixture_2_renamed')['password'] == 'new-secret-2' and document['checkins'], 'backup contains accounts with passwords, credentials, schedules and check-in history')
            await client.post('/api/v1/accounts/delete', json={'ids': [identifiers[4]]})
            before_restore = store.one('SELECT COUNT(*) AS c FROM accounts')['c']
            restored = await client.post('/api/v1/backup/restore', content=archive.content, headers={'Content-Type': 'application/json'})
            check(restored.status_code == 200 and restored.json()['accounts_added'] == 1 and store.one('SELECT COUNT(*) AS c FROM accounts')['c'] == before_restore + 1, 'restore re-creates the deleted account and merges the rest')
            revived = next(row for row in store.all('SELECT id,login_enc FROM accounts') if store.vault.open(row['login_enc'], f'login:{row["id"]}')['username'] == 'fixture_4')
            check(store.account(revived['id'])['result']['session'] == 'synthetic-cookie-fixture_4' and store.stored_password(revived['id']) == account_password, 'restored account keeps its password and credential')
            check((await client.post('/api/v1/backup/restore', content=b'{"app":"other"}', headers={'Content-Type': 'application/json'})).status_code == 400, 'foreign files are refused by restore')
            settings_now = (await client.get('/api/v1/settings')).json()['settings']
            check(settings_now['max_concurrency'] == 1 and settings_now['stats_retention_days'] == 90, 'concurrency and statistics retention settings exist with safe defaults')
            await client.put('/api/v1/settings', json=settings_now | {'max_concurrency': 3, 'account_gap': 0, 'auto_checkin': False, 'auto_reextract': False})
            await client.put('/api/v1/queue/settings', json={'max_concurrency': 3, 'checkin_concurrency': 1})
            service.peak = 0
            batch = (await client.post('/api/v1/accounts/actions', json={'ids': identifiers[5:25], 'action': 'validate'})).json()
            check(batch['queued'] == 20 and len(batch['job_ids']) == 20, 'actions return the ids of the jobs they queued')
            await client.post('/api/v1/accounts/actions', json={'ids': identifiers[5:8], 'action': 'checkin'})
            await client.post('/api/v1/queue/resume')
            await engine.start()
            await wait_empty(store)
            await engine.stop()
            check(service.peak == 1, 'legacy concurrency values cannot override global serial execution')
            overlaps = store.one("SELECT COUNT(*) AS c FROM jobs a JOIN jobs b ON a.account_id=b.account_id AND a.id<b.id WHERE a.started IS NOT NULL AND b.started IS NOT NULL AND a.finished IS NOT NULL AND b.finished IS NOT NULL AND a.started<b.finished AND b.started<a.finished AND a.kind!=b.kind")['c']
            check(overlaps == 0, 'the same account is never processed by two workers at once')
            await client.put('/api/v1/settings', json=settings_now)
            await client.put('/api/v1/queue/settings', json={'max_concurrency': 1, 'checkin_concurrency': 1})
            with store.connection() as connection:
                connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            database = store.path.read_bytes()
            check(account_password.encode() not in database and b'synthetic-cookie-' not in database and b'fixture_1' not in database, 'database has no plaintext login or session')
            original = hashlib.sha256(database).hexdigest()
            try:
                Store(config.data_dir, Vault(secrets.token_bytes(32)))
                raise AssertionError('Wrong key accepted')
            except RuntimeError:
                check(hashlib.sha256(store.path.read_bytes()).hexdigest() == original, 'wrong key refuses startup without changing database')
            await client.post('/api/v1/auth/logout')
            check((await client.get('/api/v1/accounts')).status_code == 401, 'logout revokes administrator session')

    for status, text, expected in [(401, '{}', 'invalid'), (403, '{}', 'needs_manual'), (429, '{}', 'needs_manual'), (503, '{}', 'upstream_error'), (200, '<html>success</html>', 'upstream_error')]:
        try:
            checkin_result(status, text)
            raise AssertionError('Unsafe success classification')
        except TaskError as error:
            check(error.code == expected, 'response classification ' + str(status) + expected)
    check(checkin_result(200, '{"success":true}')[0] == 'accepted', 'empty success receipt requires balance verification')
    check(checkin_result(200, '{"success":true,"message":"签到成功"}')[0] == 'signed', 'explicit sign-in receipt confirms success')
    check(checkin_result(200, '{"success":false,"message":"already checked"}')[0] == 'already_signed', 'already signed is not failure')
    try:
        profile_result(200, '{"success":true,"data":{"id":7}}', '8')
        raise AssertionError('Mismatched ID accepted')
    except TaskError as error:
        check(error.code == 'invalid', 'profile ID must match session owner')
    check(parse_proxy('s5://name:p%40ss@127.0.0.1:1080')['password'] == 'p@ss', 'authenticated SOCKS URL decoding')
    check(parse_proxy('127.0.0.1:443:name:p:a:ss')['scheme'] == 'auto', 'no port-based protocol guessing')
    challenge = "<html><script>var arg1='92F6A9882045E05FEDF95F928639A27F25A8C24A';(function(a,c){})();document.location.reload();</script></html>"
    check(is_waf_challenge(challenge) and not is_waf_challenge('{"success":true}') and not is_waf_challenge('<html><body>login</body></html>'), 'WAF challenge page recognised without false positives')
    try:
        checkin_result(200, challenge)
        raise AssertionError('Challenge page accepted')
    except TaskError as error:
        check(error.code == 'upstream_error' and error.retry_proxy, 'WAF challenge is retryable, never a fake success or a failed credential')
    try:
        checkin_result(200, '<html>请完成人机验证 captcha</html>')
        raise AssertionError('Captcha page accepted')
    except TaskError as error:
        check(error.code == 'needs_manual', 'captcha page needs manual handling')
    try:
        checkin_result(200, '<html>success synthetic-cookie</html>')
        raise AssertionError('HTML success accepted')
    except TaskError as error:
        check(error.code == 'upstream_error', 'HTML success cannot fake successful checkin')
    (ROOT / 'tools' / 'verification-backend.json').write_text(json.dumps({'passed': len(checks), 'checks': checks}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'PASS: {len(checks)} backend checks. All accounts and credentials were synthetic; no real AnyRouter login or sign-in occurred.')


asyncio.run(verify())
