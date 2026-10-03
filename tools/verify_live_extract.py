"""Push one synthetic, non-existent account through the real extraction flow on the deployed server.

Temporarily selects direct mode (there are no proxies yet), imports the account, waits for the
job to finish, reports the classification, then deletes the account and restores pool mode.
Expected result: the live login page is driven to the end and the site rejects the credentials
(invalid_credentials). needs_manual would mean a captcha; login_error would mean the page changed.
"""
import secrets
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = 'https://signin.example.com'
password = next(line.split('：', 1)[1].strip() for line in (ROOT / '部署信息.txt').read_text(encoding='utf-8').splitlines() if line.startswith('管理员密码'))

with httpx.Client(base_url=BASE, timeout=30) as client:
    csrf = client.post('/api/v1/auth/login', json={'username': 'admin', 'password': password}, headers={'Origin': BASE}).json()['csrf_token']
    mutate = {'Origin': BASE, 'X-CSRF-Token': csrf}
    settings = client.get('/api/v1/settings').json()['settings']
    original_mode = settings['proxy_mode']
    account_id = None
    try:
        print('resume queue ->', client.post('/api/v1/queue/resume', headers=mutate).status_code, flush=True)
        client.put('/api/v1/settings', json=settings | {'proxy_mode': 'direct'}, headers=mutate).raise_for_status()
        username = f'qa-{secrets.token_hex(4)}@example.invalid'
        imported = client.post('/api/v1/accounts/import', json={'import_id': 'live-qa-' + secrets.token_hex(4), 'accounts': [{'username': username, 'password': 'not-a-real-password-' + secrets.token_hex(6), 'row_id': '1'}]}, headers=mutate).json()
        account_id = imported['account_ids'][0]
        print('queued:', imported['queued'], 'account', username, flush=True)
        deadline = time.time() + 150
        job = None
        while time.time() < deadline:
            jobs = client.get('/api/v1/jobs?limit=20').json()['items']
            job = next((item for item in jobs if item['account_id'] == account_id), None)
            if job and job['status'] not in ('pending', 'running'):
                break
            print('  waiting…', job['status'] if job else 'no job yet', '|', (job or {}).get('message', ''), flush=True)
            time.sleep(5)
        print('job status:', job and job['status'], '|', job and job['message'])
        account = next(item for item in client.get('/api/v1/accounts').json()['items'] if item['id'] == account_id)
        print('account:', account['validity'], '| has_result:', account['has_result'], '|', account['message'])
        logs = client.get(f'/api/v1/logs?job_id={job["id"]}').json()['items'] if job else []
        for entry in reversed(logs):
            print('  log:', entry['level'], entry['message'])
        elapsed = (job['finished'] - job['started']) if job and job['finished'] and job['started'] else None
        print('elapsed seconds:', round(elapsed, 1) if elapsed else None)
    finally:
        if account_id:
            print('cleanup: delete account ->', client.post('/api/v1/accounts/delete', json={'ids': [account_id]}, headers=mutate).status_code)
        print('cleanup: restore mode ->', client.put('/api/v1/settings', json=settings | {'proxy_mode': original_mode}, headers=mutate).status_code)
        print('mode now:', client.get('/api/v1/settings').json()['settings']['proxy_mode'])
        client.post('/api/v1/auth/logout', headers=mutate)
