"""Check whether the proxy's exit IP is sticky across separate connections, and how the WAF cookie behaves."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.proxy import parse_proxy

config = parse_proxy(sys.argv[1])
scheme = sys.argv[2] if len(sys.argv) > 2 else 'socks5'
url = f"{scheme}://{config['username']}:{config['password']}@{config['host']}:{config['port']}"


async def main():
    ips = []
    for attempt in range(4):
        async with httpx.AsyncClient(proxy=url, timeout=30, trust_env=False) as client:
            response = await client.get('https://api.ipify.org?format=json')
            ips.append(response.json()['ip'])
    print('exit IPs over 4 fresh connections:', ips, '| sticky:', len(set(ips)) == 1)


asyncio.run(main())
