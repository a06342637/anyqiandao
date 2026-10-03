import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import uvicorn
from argon2 import PasswordHasher
from app.config import Config
from app.errors import TaskError
from app.main import create_app


class PreviewService:
    async def extract(self, username, password, route, settings):
        await asyncio.sleep(float(os.environ.get('ANY_UI_TASK_DELAY', '0.6')))
        return {'session': f'preview-{username}', 'api_user': '42', 'quota': 120.0}

    async def validate(self, credentials, route, settings):
        await asyncio.sleep(float(os.environ.get('ANY_UI_TASK_DELAY', '0.6')))
        if credentials['session'] == 'expired-preview':
            raise TaskError('invalid', '界面验收：Cookie 已失效')
        return {'quota': 125.75}

    async def checkin(self, credentials, route, settings, *, before_submit=None):
        await self.validate(credentials, route, settings)
        return {'code': 'signed', 'message': '界面验收：签到成功', 'quota': 135.75, 'quota_before': 125.75, 'quota_after': 135.75,
                'logs': ['合成数据验收，未连接 AnyRouter']}


port = int(os.environ.get('ANY_UI_PORT', '18791'))
data_dir = Path(os.environ.get('ANY_UI_DATA_DIR', str(ROOT / '.local' / 'ui-v3' / 'data'))).resolve()
if not data_dir.is_relative_to((ROOT / '.local').resolve()):
    raise RuntimeError('Synthetic preview data must stay inside the project .local directory')
config = Config(bytes(range(32)), PasswordHasher().hash('v3-ui-only-password'), data_dir,
                f'http://127.0.0.1:{port}', start_worker=True)
app = create_app(config, PreviewService())
store = app.state.store
store.set_meta('settings', store.settings().model_copy(update={'proxy_mode': 'direct', 'auto_checkin': False, 'account_gap': 0, 'max_concurrency': 2}).model_dump_json())
if not store.one('SELECT id FROM accounts LIMIT 1'):
    now = time.time()
    identifiers = []
    for index in range(13):
        identifier = uuid.uuid4().hex
        identifiers.append(identifier)
        username = f'示例账号 {index + 1:02d}' if index != 2 else 'long.demo.account@example.invalid'
        credentials = {'session': 'expired-preview' if index == 1 else f'preview-{index}', 'api_user': str(index + 10)}
        before, after = 120.0 + index * 17.35, 130.0 + index * 17.35
        store.execute('INSERT INTO accounts(id,login_hash,login_enc,result_enc,validity,message,quota,created,updated,note,position,last_validated,last_extracted,last_checkin,checkin_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (identifier, store.vault.fingerprint(username), store.vault.seal({'username': username, 'password': 'synthetic-account-only'}, f'login:{identifier}'),
                       store.vault.seal(credentials, f'result:{identifier}'), 'invalid' if index == 1 else 'valid', '合成数据 · 仅用于界面验收', after, now, now,
                       '主账号 · 自动签到' if index == 0 else '测试用备注', index, now, now - 3600, now, 'signed'))
        store.record_checkin(identifier, None, 'signed', before, after)
        store.log('checkin', f'界面验收：签到前 ${before:.4f} → 签到后 ${after:.4f}，新增 $10.0000', account_id=identifier)
    schedule_id = uuid.uuid4().hex
    store.execute('INSERT INTO schedules(id,name,interval_minutes,next_run,created) VALUES (?,?,?,?,?)',
                  (schedule_id, '界面验收计划', 1440, now + 86400, now))
    for identifier in identifiers:
        store.execute('INSERT INTO schedule_accounts VALUES (?,?)', (schedule_id, identifier))
    for index in range(20):
        store.log('system' if index % 3 else 'validate', '合成日志，用于验证分类和分页', level='error' if index % 5 == 0 else 'info')

if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=port, access_log=False)
