import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.db import SCHEMA_VERSION
parser = argparse.ArgumentParser()
parser.add_argument('--exercise-account', action='store_true')
args = parser.parse_args()
values = {}
for line in (ROOT / '部署信息.txt').read_text(encoding='utf-8').splitlines():
    key, separator, value = line.partition('：')
    if separator:
        values[key.strip()] = value.strip()
report = {}
with httpx.Client(base_url=values.get('访问地址', 'https://signin.example.com'), timeout=45, trust_env=False) as client:
    health = client.get('/healthz')
    health.raise_for_status()
    assert health.json()['version'] == (ROOT / 'VERSION').read_text().strip() and health.json()['schema_version'] == SCHEMA_VERSION
    branding = client.get('/api/v1/branding')
    branding.raise_for_status()
    assert set(branding.json()) == {'site_name', 'site_icon_text'}
    favicon = client.get('/favicon.svg')
    favicon.raise_for_status()
    assert 'image/svg+xml' in favicon.headers['content-type']
    response = client.post('/api/v1/auth/login', json={'username': values.get('管理员账号', 'admin'), 'password': values['管理员密码']})
    response.raise_for_status()
    client.headers['X-CSRF-Token'] = response.json()['csrf_token']
    report['health'] = health.json()
    accounts = client.get('/api/v1/accounts?limit=5').json()
    assert accounts['total'] >= 3 and all('last_balance' in item for item in accounts['items'])
    report['accounts'] = accounts['total']
    for endpoint in ('/dashboard', '/jobs?limit=5', '/schedules?limit=5', '/logs?limit=5', '/settings'):
        checked = client.get('/api/v1' + endpoint)
        checked.raise_for_status()
    queue = client.get('/api/v1/jobs?limit=5').json()
    assert 1 <= queue['max_concurrency'] <= 5 and 1 <= queue['checkin_concurrency'] <= 5
    assert set(queue['counts']) == {'queue_pending', 'queue_running', 'checkin_pending', 'checkin_running'}
    for lane in ('queue', 'checkin'):
        filtered = client.get('/api/v1/jobs', params={'lane': lane, 'state': 'active', 'limit': 5})
        filtered.raise_for_status()
        assert all((item['kind'] == 'checkin') == (lane == 'checkin') and item['status'] in ('pending', 'running') for item in filtered.json()['items'])
    report['queue'] = {field: queue[field] for field in ('max_concurrency', 'checkin_concurrency', 'paused', 'counts')}
    index = client.get('/')
    index.raise_for_status()
    asset = re.search(r'<script[^>]+src="([^"]+\.js)"', index.text)
    assert asset and asset.group(1).startswith('/assets/')
    bundle = client.get(asset.group(1))
    bundle.raise_for_status()
    assert all(label in bundle.text for label in ('执行队列', '签到并发', '保存并发', '停止并删除'))
    assert '默认计划的签到间隔' not in bundle.text and '查看原项目' not in bundle.text
    report['frontend'] = {'asset': asset.group(1), 'queue_ui': True, 'removed_settings': True}
    for span in ('day', 'week', 'month'):
        stats = client.get('/api/v1/stats', params={'range': span})
        stats.raise_for_status()
        assert stats.json()['accounts_total'] == accounts['total']
        statistics = stats.json()
        assert statistics['attempts'] == sum(statistics[field] for field in ('signed', 'already', 'failed', 'uncertain'))
        for field in ('signed', 'already', 'failed', 'uncertain'):
            assert statistics[field] == sum(item[field] for item in statistics['series'])
            assert statistics[field] == sum(item[field] for item in statistics['accounts'])
    for attempt in range(10):
        updates = client.get('/api/v1/updates').json()
        if updates['updater_available']:
            break
        time.sleep(2)
    assert updates['updater_available'], 'Updater heartbeat unavailable'
    report['updates'] = {'available': updates['updater_available'], 'source_configured': bool(updates['repository'])}
    backup = client.post('/api/v1/backup')
    backup.raise_for_status()
    document = backup.json()
    assert len(document['accounts']) == accounts['total'] and document['schema_version'] == SCHEMA_VERSION
    assert all('balance_source' in item for item in document['checkins'])
    assert document['settings']['site_name'] == branding.json()['site_name']
    report['backup'] = {key: len(document[key]) for key in ('accounts', 'schedules', 'proxies', 'checkins')}
    if args.exercise_account:
        target = next(item for item in accounts['items'] if item['has_result'])
        queued = client.post('/api/v1/accounts/actions', json={'ids': [target['id']], 'action': 'checkin'})
        queued.raise_for_status()
        identifier = queued.json()['job_ids'][0]
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            status = client.get('/api/v1/jobs/status', params={'ids': identifier})
            status.raise_for_status()
            job = status.json()['items'][0]
            if job['status'] not in ('pending', 'running'):
                break
            time.sleep(2)
        report['live_checkin'] = {'status': job['status'], 'balance': job.get('balance'), 'message': job['message']}
        assert job['status'] in ('signed', 'already_signed'), json.dumps(report['live_checkin'], ensure_ascii=False)
        assert job['balance'] and job['balance']['balance_source'] == 'live'
        assert job['balance']['quota_before'] is not None and job['balance']['quota_after'] is not None
    client.post('/api/v1/auth/logout').raise_for_status()
print(json.dumps(report, ensure_ascii=False, indent=2))
