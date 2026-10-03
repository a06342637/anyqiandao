import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('ANY_UI_PORT', '18795')
os.environ.setdefault('ANY_UI_DATA_DIR', str(ROOT / '.local' / 'ui-v035' / 'data'))

import uvicorn
from tools.dev.server_v034 import PreviewServiceV034, app, port, store


class PreviewServiceV035(PreviewServiceV034):
    async def validate(self, credentials, route, settings):
        await super().validate(credentials, route, settings)
        return {'quota': 341.2, 'profile': {'used_quota': 751900000}}


app.state.engine.service = PreviewServiceV035()
if store.meta('ui_v035_seeded') != '1':
    row = store.account(store.one('SELECT id FROM accounts ORDER BY position,created LIMIT 1')['id'])
    store.execute("UPDATE accounts SET checkin_status='uncertain',last_checkin=?,quota=? WHERE id=?",
                  (time.time() - 120, 316.2, row['id']))
    store.observe_account(row['id'], row['result']['api_user'], 316.2, 1503.8)
    store.set_meta('ui_v035_seeded', '1')

if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=port, access_log=False)
