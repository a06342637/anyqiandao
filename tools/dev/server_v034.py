import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('ANY_UI_PORT', '18794')
os.environ.setdefault('ANY_UI_DATA_DIR', str(ROOT / '.local' / 'ui-v034' / 'data'))

import uvicorn
from app.console_data import profile_view, token_view, usage_view
from tools.dev.server_v3 import PreviewService, app, port, store


class PreviewServiceV034(PreviewService):
    async def insights(self, credentials, route, settings, options):
        await self.validate(credentials, route, settings)
        now = int(time.time())
        profile = profile_view({'username': '合成看板账号', 'quota': 170600000, 'used_quota': 751900000, 'request_count': 11970, 'group': 'default', 'status': 1})
        result = {'view': options['view'], 'profile': profile, 'fetched_at': now}
        if options['view'] == 'tokens':
            rows = [token_view({'id': index + 1, 'name': f'合成令牌 {index + 1:02d}', 'user_id': int(credentials['api_user']),
                                'key': f'synthetic-preview-key-{index + 1:04d}', 'status': 1 if index % 3 else 2, 'used_quota': 125000 * (index + 1),
                                'remain_quota': 5000000, 'unlimited_quota': index % 2 == 0, 'created_time': now - 86400 * (index + 1),
                                'expired_time': -1, 'accessed_time': now - 3600, 'group': 'default', 'model_limits_enabled': index % 2 == 1,
                                'model_limits': 'model-alpha,model-beta', 'allow_ips': '192.0.2.0/24' if index % 2 else ''}, credentials['api_user']) for index in range(13)]
            start = (options['page'] - 1) * options['limit']
            return {**result, 'items': rows[start:start + options['limit']], 'page': options['page'], 'limit': options['limit'], 'total': 13,
                    'has_more': start + options['limit'] < len(rows)}
        since = now - {'day': 1, 'week': 7, 'month': 30}[options['range']] * 86400
        bucket = 'hour' if options['range'] == 'day' else 'day'
        rows = [{'user_id': int(credentials['api_user']), 'model_name': 'model-alpha' if index % 2 else 'model-beta',
                 'count': 9 + index, 'token_used': 1600 * (index + 1), 'quota': 25000 * (index + 1), 'created_at': now - index * 3600} for index in range(14)]
        return {**result, 'since': since, 'until': now, 'range': options['range'], 'usage': usage_view(rows, since, now, bucket, settings.timezone, credentials['api_user']), 'usage_error': ''}


app.state.engine.service = PreviewServiceV034()
store.set_meta('settings', store.settings().model_copy(update={'auto_checkin': False, 'account_gap': 0}).model_dump_json())
if store.meta('ui_v034_seeded') != '1':
    rows = store.all('SELECT * FROM accounts ORDER BY position,created')
    for index in (3, 4):
        identifier = rows[index]['id']
        username = '手动 Session 账号' if index == 3 else '等待更新凭证账号'
        store.execute('UPDATE accounts SET login_hash=?,login_enc=?,validity=?,message=? WHERE id=?',
                      (store.vault.fingerprint(username), store.vault.seal({'username': username, 'password': None}, f'login:{identifier}'),
                       'valid' if index == 3 else 'invalid', 'Session 导入 · 只签到；失效等待手动更新', identifier))
    store.execute('UPDATE accounts SET note=? WHERE id=?', ('这是一段较长的测试备注，用于确认手机、平板与桌面上的换行和截断都不会挤压操作按钮。' * 2, rows[2]['id']))
    for index in range(28):
        identifier = rows[index % len(rows)]['id']
        job_id, _ = store.enqueue('validate', identifier)
        store.execute("UPDATE jobs SET status='success',message='合成任务，仅用于界面验收',finished=? WHERE id=?", (time.time(), job_id))
    store.set_meta('ui_v034_seeded', '1')

if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=port, access_log=False)
