"""Populate the throwaway dev database with synthetic rows so every page has content to look at.

Runs only against .local/dev; nothing here touches real accounts or the server.
"""
import base64
import sqlite3
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.crypto import Vault  # noqa: E402
from app.db import Store  # noqa: E402

DEV = ROOT / '.local' / 'dev'
key = base64.b64decode((DEV / 'secrets' / 'app.key').read_text().strip(), altchars=b'-_', validate=True)
vault = Vault(key)
store = Store(DEV / 'data', vault)
now = time.time()
H = 3600


def account(username, *, validity='unknown', message='', result=None, quota=None, checkin=None,
            extracted=None, validated=None, last_checkin=None, created=None):
    account_id = uuid.uuid4().hex
    result_enc = vault.seal(result, f'result:{account_id}') if result else None
    store.execute(
        'INSERT INTO accounts(id,login_hash,login_enc,result_enc,validity,message,last_validated,last_extracted,last_checkin,checkin_status,quota,created,updated) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (account_id, vault.fingerprint(username), vault.seal({'username': username, 'password': 'demo-password'}, f'login:{account_id}'), result_enc,
         validity, message, validated, extracted, last_checkin, checkin, quota, created or now, now))
    return account_id


def proxy(name, config, *, status='unknown', message='', latency=None, enabled=1, candidate=None, trusted=None, created=None):
    proxy_id = uuid.uuid4().hex
    store.execute(
        'INSERT INTO proxies(id,name,config_enc,enabled,status,message,latency,trusted_key,candidate_key,candidate_fingerprint,tested_at,failed_until,created) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (proxy_id, name, vault.seal(config, f'proxy:{proxy_id}'), enabled, status, message, latency, trusted,
         'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample' if candidate else None, candidate, now - 600 if status != 'unknown' else None, 0, created or now))
    return proxy_id


def job(kind, account_id=None, *, status='success', message='', source='manual', proxy_id=None, created=None, started=None, finished=None):
    job_id = uuid.uuid4().hex
    store.execute('INSERT INTO jobs(id,account_id,proxy_id,kind,status,source,message,created,started,finished) VALUES (?,?,?,?,?,?,?,?,?,?)',
                  (job_id, account_id, proxy_id, kind, status, source, message, created or now, started, finished))
    return job_id


def log(kind, message, *, level='info', job_id=None, account_id=None, created=None):
    category = 'error' if level == 'error' else 'checkin' if kind in ('checkin', 'script') else 'proxy' if kind == 'proxy_test' else kind
    store.execute('INSERT INTO logs(job_id,account_id,kind,level,message,created,category) VALUES (?,?,?,?,?,?,?)',
                  (job_id, account_id, kind, level, message, created or now, category))


with store.connection() as connection:
    for table in ('logs', 'jobs', 'schedule_accounts', 'schedules', 'proxies', 'accounts'):
        connection.execute(f'DELETE FROM {table}')

store.set_meta('queue_paused', '0')
store.set_meta('pause_reason', '')

result = lambda index: {'session': f'MTc1Nzk5{index:02d}fDE3NTc5OTM4Mzl8sample-session-value', 'api_user': str(24000 + index), 'cookies': {}, 'quota': 12.5}
ids = []
ids.append(account('alice.chen@example.com', validity='valid', message='凭证有效', result=result(1), quota=48.2531, checkin='signed', extracted=now - 26 * H, validated=now - 2 * H, last_checkin=now - 2 * H, created=now - 30 * H))
ids.append(account('bob_router', validity='valid', message='凭证有效', result=result(2), quota=12.0004, checkin='already_signed', extracted=now - 50 * H, validated=now - 26 * H, last_checkin=now - 26 * H, created=now - 52 * H))
ids.append(account('carol.wu@mail.com', validity='valid', message='凭证提取成功', result=result(3), quota=5.5, extracted=now - 40 * 60, validated=now - 40 * 60, created=now - H))
ids.append(account('dave-prod', validity='invalid', message='登录凭证已失效，请重新提取', result=result(4), quota=0.9, checkin='invalid', extracted=now - 120 * H, validated=now - 3 * H, last_checkin=now - 3 * H, created=now - 125 * H))
ids.append(account('erin.zhang@example.org', validity='blocked', message='网站拒绝访问或触发风控，需要人工处理', result=result(5), quota=31.25, checkin='needs_manual', extracted=now - 70 * H, validated=now - 5 * H, last_checkin=now - 5 * H, created=now - 72 * H))
ids.append(account('frank_liu', validity='network_error', message='检测连接失败或超时，未判定凭证失效', result=result(6), quota=7.75, checkin='signed', extracted=now - 90 * H, validated=now - 1 * H, last_checkin=now - 25 * H, created=now - 95 * H))
ids.append(account('grace.ho@example.com', message='网站拒绝了登录，请核对账号密码或账号状态', created=now - 20 * 60))
ids.append(account('henry_new', message='', created=now - 5 * 60))
ids.append(account('ivy.lin@example.net', validity='valid', message='凭证有效', result=result(9), quota=102.0, checkin='signed', extracted=now - 200 * H, validated=now - 20 * H, last_checkin=now - 20 * H, created=now - 210 * H))
ids.append(account('jack-1024', validity='valid', message='凭证有效', result=result(10), quota=0.0, checkin='uncertain', extracted=now - 300 * H, validated=now - 44 * H, last_checkin=now - 44 * H, created=now - 310 * H))

p_http = proxy('香港 HTTP 节点', {'scheme': 'http', 'host': '203.0.113.10', 'port': 8080, 'username': 'user', 'password': 'x'}, status='healthy', message='网络连通（HTTP 200）；连通不等于账号登录成功', latency=286, created=now - 100 * H)
p_socks = proxy('东京 SOCKS5', {'scheme': 'socks5', 'host': '198.51.100.7', 'port': 1080, 'username': 'proxyuser', 'password': 'x'}, status='unavailable', message='代理不可用、认证失败或连接超时', latency=None, created=now - 90 * H)
p_ssh = proxy('自建 SSH 跳板', {'scheme': 'ssh', 'host': 'jump.example.com', 'port': 22, 'username': 'forward', 'password': 'x'}, status='needs_trust', message='请在设置中核对并确认 SSH 主机指纹', candidate='SHA256:9mxQ06sx1J3Ii45BOB7b4ktf3YFGJBpBx5KMb84tCZA', created=now - 80 * H)
proxy('备用节点', {'scheme': 'auto', 'host': '192.0.2.44', 'port': 7890, 'username': '', 'password': ''}, enabled=0, created=now - 10 * H)

schedule_id = uuid.uuid4().hex
store.execute('INSERT INTO schedules(id,name,interval_minutes,enabled,next_run,last_run,created) VALUES (?,?,?,?,?,?,?)',
              (schedule_id, '每日签到', 1440, 1, now + 9.5 * H, now - 14.5 * H, now - 200 * H))
for account_id in ids[:3] + ids[8:]:
    store.execute('INSERT INTO schedule_accounts VALUES (?,?)', (schedule_id, account_id))
paused_id = uuid.uuid4().hex
store.execute('INSERT INTO schedules(id,name,interval_minutes,enabled,next_run,last_run,created) VALUES (?,?,?,?,?,?,?)',
              (paused_id, '测试账号 · 每 12 小时', 720, 0, now + 5 * H, now - 30 * H, now - 60 * H))
store.execute('INSERT INTO schedule_accounts VALUES (?,?)', (paused_id, ids[3]))

j1 = job('checkin', ids[0], status='signed', message='签到成功', source=f'schedule:{schedule_id}', proxy_id=p_http, created=now - 2 * H - 30, started=now - 2 * H - 20, finished=now - 2 * H)
j2 = job('validate', ids[5], status='network_error', message='检测连接失败或超时，未判定凭证失效', created=now - H - 60, started=now - H - 50, finished=now - H)
j3 = job('extract', ids[2], status='success', message='凭证提取成功，已加入账号列表；本次登录密码已移除', proxy_id=p_http, created=now - 45 * 60, started=now - 41 * 60, finished=now - 40 * 60)
j4 = job('extract', ids[6], status='invalid_credentials', message='网站拒绝了登录，请核对账号密码或账号状态', proxy_id=p_http, created=now - 25 * 60, started=now - 21 * 60, finished=now - 20 * 60)
j5 = job('extract', ids[7], status='running', message='正在尝试代理 1/2', proxy_id=p_http, created=now - 4 * 60, started=now - 30)
j6 = job('validate', ids[1], status='pending', message='', created=now - 3 * 60)
j7 = job('checkin', ids[8], status='pending', message='', created=now - 2 * 60)
j8 = job('proxy_test', proxy_id=p_socks, status='proxy_error', message='代理不可用、认证失败或连接超时', created=now - 6 * H, started=now - 6 * H + 5, finished=now - 6 * H + 15)

log('system', '服务启动，队列执行器已就绪；数据库迁移版本 2', created=now - 30 * H)
log('proxy_test', '任务开始执行', job_id=j8, created=now - 6 * H + 5)
log('proxy_test', '代理不可用、认证失败或连接超时', level='warning', job_id=j8, created=now - 6 * H + 15)
log('checkin', '任务开始执行', job_id=j1, account_id=ids[0], created=now - 2 * H - 20)
log('script', '已执行固定版本的内置签到脚本', job_id=j1, account_id=ids[0], created=now - 2 * H - 5)
log('script', '网站签到接口返回 HTTP 200', job_id=j1, account_id=ids[0], created=now - 2 * H - 4)
log('checkin', '签到成功', job_id=j1, account_id=ids[0], created=now - 2 * H)
log('validate', '检测连接失败或超时，未判定凭证失效', level='warning', job_id=j2, account_id=ids[5], created=now - H)
log('extract', '任务开始执行', job_id=j3, account_id=ids[2], created=now - 41 * 60)
log('extract', '凭证提取成功，已加入账号列表；本次登录密码已移除', job_id=j3, account_id=ids[2], created=now - 40 * 60)
log('extract', '网站拒绝了登录，请核对账号密码或账号状态', level='warning', job_id=j4, account_id=ids[6], created=now - 20 * 60)
log('system', '运行参数已更新；新参数对后续任务生效', created=now - 15 * 60)
log('extract', '任务开始执行', job_id=j5, account_id=ids[7], created=now - 30)
log('system', '任务引擎暂时异常（TimeoutError），已停止当前轮询', level='error', created=now - 10)

# Check-in history for the dashboard: 14 days of mixed results with small balance gains.
import random
random.seed(7)
for account_id in ids[:6] + ids[8:]:
    balance = random.uniform(5, 120)
    for day in range(14, 0, -1):
        when = now - day * 24 * H + random.uniform(0, 3 * H)
        roll = random.random()
        code = 'signed' if roll < 0.75 else 'already_signed' if roll < 0.9 else 'invalid'
        gain = random.choice([0.25, 0.5, 1.0]) if code == 'signed' else 0
        store.execute('INSERT INTO checkins(account_id,job_id,code,quota_before,quota_after,created) VALUES (?,?,?,?,?,?)',
                      (account_id, None, code, round(balance, 4), round(balance + gain, 4) if code != 'invalid' else None, when))
        balance += gain
for index, account_id in enumerate(ids):
    store.execute('UPDATE accounts SET position=?,note=? WHERE id=?', (index + 1, ['主号', '', '备用', '', '测试', '', '', '新加', '', ''][index], account_id))

# The engine will otherwise pick up the pending rows and try to reach the fake proxies.
store.set_meta('queue_paused', '1')
store.set_meta('pause_reason', '已手动暂停，当前任务结束后不再启动新任务')
print('seeded', store.one('SELECT COUNT(*) AS c FROM accounts')['c'], 'accounts')
