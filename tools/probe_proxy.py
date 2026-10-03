"""Probe the user's proxy line: parse it, detect its protocol without credentials, then tunnel to anyrouter.top through it."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.proxy import ProxyBridge, detect_protocol, parse_proxy

LINE = sys.argv[1]


async def main():
    config = parse_proxy(LINE)
    print('parsed:', {k: (v if k != 'password' else '***') for k, v in config.items()})
    if config['scheme'] == 'auto':
        config['scheme'] = await detect_protocol(config, 10)
        print('detected scheme:', config['scheme'])
    async with ProxyBridge(config, 15) as route:
        async with httpx.AsyncClient(proxy=route.url, timeout=30, trust_env=False) as client:
            for url in ('https://anyrouter.top/login', 'https://anyrouter.top/api/user/self'):
                response = await client.get(url, headers={'Accept': 'application/json'})
                body = response.text
                print(f'{url} -> HTTP {response.status_code}, {len(body)} bytes, waf-markers:',
                      [m for m in ('acw_sc__', 'arg1', 'captcha', 'cloudflare', 'cdn_sec_tc') if m in body],
                      '| head:', body[:120].replace('\n', ' '))
                print('   cookies set:', list(response.cookies.keys()))


asyncio.run(main())
