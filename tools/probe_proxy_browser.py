"""Load the login page through a proxy with the real adapter's browser session and report timings / WAF behaviour."""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.proxy import ProxyBridge, detect_protocol, parse_proxy
from app.responses import is_waf_challenge
from app.router_service import BrowserSession
from app.schemas import RuntimeSettings


async def main():
    config = parse_proxy(sys.argv[1])
    if config['scheme'] == 'auto':
        config['scheme'] = await detect_protocol(config, 10)
    settings = RuntimeSettings(connect_timeout=30, login_timeout=90)
    async with ProxyBridge(config, settings.connect_timeout) as route:
        async with BrowserSession(route, settings) as session:
            page = session.page
            page.on('response', lambda r: print(f'  {time.monotonic() - t0:5.1f}s {r.status} {r.url[:80]}', flush=True) if 'anyrouter.top' in r.url and not r.url.endswith(('.js', '.css', '.png', '.svg', '.woff2')) else None)
            t0 = time.monotonic()
            try:
                await page.goto('https://anyrouter.top/login', wait_until='domcontentloaded', timeout=60000)
                print(f'domcontentloaded at {time.monotonic() - t0:.1f}s; challenge={is_waf_challenge(await page.content())}')
                for _ in range(20):
                    await asyncio.sleep(1)
                    content = await page.content()
                    print(f'  {time.monotonic() - t0:5.1f}s challenge={is_waf_challenge(content)} form={"username" in content}')
                    if not is_waf_challenge(content) and 'username' in content:
                        break
                status, text = await session.api('/api/user/self', headers={'Accept': 'application/json'})
                print('api /api/user/self ->', status, text[:80])
                cookies = await session.cookies()
                print('cookies:', sorted(cookies))
            except Exception as error:
                print('ERROR', type(error).__name__, str(error)[:200])


asyncio.run(main())
