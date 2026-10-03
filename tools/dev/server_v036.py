import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('ANY_UI_PORT', '18796')
os.environ.setdefault('ANY_UI_DATA_DIR', str(ROOT / '.local' / 'ui-v036' / 'data'))

import uvicorn
from fastapi.responses import JSONResponse
from tools.dev.server_v035 import app, port

app.state.preview_failed_ranges = set()


@app.middleware('http')
async def preview_loading(request, call_next):
    if request.url.path in ('/api/v1/stats', '/api/v1/accounts'):
        await asyncio.sleep(2)
    if request.url.path == '/api/v1/stats' and request.query_params.get('range') == 'week':
        if 'week' not in app.state.preview_failed_ranges:
            app.state.preview_failed_ranges.add('week')
            return JSONResponse({'detail': '合成验收：统计暂时读取失败，请重试'}, status_code=503)
    return await call_next(request)


if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=port, access_log=False)
