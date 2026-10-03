"""End-to-end automation check on the deployed server using the real test account.

Adds the given proxy (pool mode) and imports the account; then waits for the automatic chain:
extract → join default schedule → check-in. Reports statuses only; never prints secrets.
Usage: verify_live_auto.py <username> <password> <proxy-line>
"""
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = 'https://signin.example.com'
admin = next(line.split('：', 1)[1].strip() for line in (ROOT / '部署信息.txt').read_text(encoding='utf-8').splitlines() if line.startswith('管理员密码'))
username, password, proxy_line = sys.argv[1], sys.argv[2], sys.argv[3]

with httpx.Client(base_url=BASE, timeout=60) as client:
    csrf = client.post('/api/v1/auth/login', json={'username': 'admin', 'password': admin}, headers={'Origin': BASE}).json()['csrf_token']
    mutate = {'Origin': BASE, 'X-CSRF-Token': csrf}
    print('resume queue ->', client.post('/api/v1/queue/resume', headers=mutate).status_code)
    settings = client.get('/api/v1/settings').json()['settings']
    print('settings:', {k: settings[k] for k in ('proxy_mode', 'auto_checkin', 'auto_reextract', 'auto_checkin_interval_minutes')})
    proxies = client.get('/api/v1/proxies').json()['items']
    if not proxies:
        imported = client.post('/api/v1/proxies/import', json={'lines': [proxy_line]}, headers=mutate).json()
        print('proxy import ->', imported)
    else:
        print('existing proxies:', [(p['name'], p['scheme'], p['status']) for p in proxies])
    result = client.post('/api/v1/accounts/import', json={'import_id': 'live-auto-' + str(int(time.time())), 'accounts': [{'username': username, 'password': password, 'row_id': '1'}]}, headers=mutate).json()
    account_id = result['account_ids'][0]
    print('account queued:', result['queued'], 'skipped:', result['skipped'])
    seen = set()
    deadline = time.time() + 420
    while time.time() < deadline:
        jobs = [j for j in client.get('/api/v1/jobs?limit=50').json()['items'] if j['account_id'] == account_id or j['kind'] == 'proxy_test']
        for job in jobs:
            key = (job['id'], job['status'], job['message'])
            if key not in seen:
                seen.add(key)
                print(f"  {time.strftime('%H:%M:%S')} {job['kind']:10} {job['source']:14} {job['status']:18} {job['message']}")
        done = [j for j in jobs if j['account_id'] == account_id]
        if done and all(j['status'] not in ('pending', 'running') for j in done) and any(j['kind'] == 'checkin' for j in done):
            break
        time.sleep(5)
    account = next(a for a in client.get('/api/v1/accounts').json()['items'] if a['id'] == account_id)
    print('account:', {k: account[k] for k in ('validity', 'has_result', 'has_password', 'checkin_status', 'quota', 'message')})
    schedules = client.get('/api/v1/schedules').json()
    print('schedules:', [(s['name'], s['account_count'], s['enabled'], s['interval_minutes']) for s in schedules['items']], 'auto:', bool(schedules['auto_schedule_id']))
    proxies = client.get('/api/v1/proxies').json()['items']
    print('proxies:', [(p['name'], p['scheme'], p['status'], p['latency']) for p in proxies])
    for entry in reversed(client.get(f'/api/v1/logs?limit=30').json()['items']):
        if entry['account_id'] == account_id:
            print('  log:', entry['level'], entry['kind'], entry['message'])
    client.post('/api/v1/auth/logout', headers=mutate)
