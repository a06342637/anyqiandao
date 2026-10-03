"""Smoke-test the deployed site over HTTPS. Reads the admin password from 部署信息.txt and never prints it."""
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = 'https://signin.example.com'
password = next(line.split('：', 1)[1].strip() for line in (ROOT / '部署信息.txt').read_text(encoding='utf-8').splitlines() if line.startswith('管理员密码'))
results = []


def check(condition, label):
    assert condition, label
    results.append(label)
    print('  ok:', label, flush=True)


with httpx.Client(base_url=BASE, timeout=30, http2=True) as client:
    home = client.get('/')
    check(home.status_code == 200 and '<div id="root">' in home.text and 'any签到助手' in home.text, 'front page served over HTTPS')
    headers = home.headers
    check(headers.get('strict-transport-security', '').startswith('max-age='), 'HSTS header present')
    check(headers.get('cache-control') == 'no-store' and headers.get('x-frame-options') == 'DENY' and 'content-security-policy' in headers, 'no-store / frame-deny / CSP headers present')
    asset = next(part.split('"')[0] for part in home.text.split('src="')[1:] if part.startswith('/assets/'))
    check(client.get(asset).status_code == 200, 'built frontend bundle is served')
    check(client.get('/api/v1/accounts').status_code == 401, 'API refuses anonymous access')
    wrong = client.post('/api/v1/auth/login', json={'username': 'admin', 'password': 'definitely-not-the-password'}, headers={'Origin': BASE})
    check(wrong.status_code == 401, 'wrong admin password rejected')
    login = client.post('/api/v1/auth/login', json={'username': 'admin', 'password': password}, headers={'Origin': BASE})
    check(login.status_code == 200 and 'csrf_token' in login.json(), 'admin login with the generated password succeeds')
    cookie = login.headers.get('set-cookie', '')
    check('HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=strict' in cookie.lower().replace('samesite=strict', 'SameSite=strict'), 'session cookie is HttpOnly + Secure + SameSite=Strict')
    csrf = login.json()['csrf_token']
    me = client.get('/api/v1/auth/me')
    check(me.status_code == 200 and me.json()['version'] == '0.1.0', 'authenticated /auth/me works (v0.1.0)')
    dashboard = client.get('/api/v1/dashboard').json()
    check(dashboard['accounts']['total'] == 0 and dashboard['proxy_mode'] == 'pool' and not dashboard['storage_error'], 'fresh dashboard: 0 accounts, proxy mode, storage healthy')
    settings = client.get('/api/v1/settings').json()
    check(settings['settings']['connect_timeout'] == 10 and settings['settings']['login_timeout'] == 60 and settings['settings']['account_gap'] == 2, 'default timeouts 10s / 60s / 2s')
    no_csrf = client.post('/api/v1/queue/pause', headers={'Origin': BASE})
    check(no_csrf.status_code == 403, 'mutation without CSRF token is refused')
    paused = client.post('/api/v1/queue/pause', headers={'Origin': BASE, 'X-CSRF-Token': csrf})
    resumed = client.post('/api/v1/queue/resume', headers={'Origin': BASE, 'X-CSRF-Token': csrf})
    check(paused.status_code == 200 and resumed.status_code == 200, 'queue pause / resume round-trip with CSRF token')
    logout = client.post('/api/v1/auth/logout', headers={'Origin': BASE, 'X-CSRF-Token': csrf})
    check(logout.status_code == 200 and client.get('/api/v1/auth/me').status_code == 401, 'logout revokes the session')
print(f'PASS: {len(results)} live checks against {BASE}')
