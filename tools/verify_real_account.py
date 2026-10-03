"""Run the real browser adapter against anyrouter.top with a real account, locally via Edge.

Usage: python tools/verify_real_account.py <username> <password> [proxy-line] [--checkin]
Prints classifications and timings only; never prints the password or the session cookie.
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.async_api import async_playwright as real_playwright  # noqa: E402

import app.router_service as router_service  # noqa: E402
from app.errors import TaskError  # noqa: E402
from app.proxy import ProxyBridge, detect_protocol, parse_proxy  # noqa: E402
from app.schemas import RuntimeSettings  # noqa: E402


class EdgeRuntime:
    def __init__(self, inner):
        self.inner = inner

    @property
    def chromium(self):
        inner = self.inner.chromium

        class Launcher:
            async def launch(self, **kwargs):
                return await inner.launch(channel='msedge', **kwargs)
        return Launcher()


class EdgePlaywright:
    async def __aenter__(self):
        self.manager = real_playwright()
        return EdgeRuntime(await self.manager.__aenter__())

    async def __aexit__(self, *args):
        return await self.manager.__aexit__(*args)


if "--edge" in sys.argv:
    router_service.async_playwright = EdgePlaywright


async def main():
    username, password = sys.argv[1], sys.argv[2]
    proxy_line = next((arg for arg in sys.argv[3:] if not arg.startswith('--')), None)
    do_checkin = '--checkin' in sys.argv
    settings = RuntimeSettings()
    service = router_service.RouterService()

    async def run(route, label):
        started = time.monotonic()
        try:
            result = await service.extract(username, password, route, settings)
        except TaskError as error:
            print(f'[{label}] extract -> {error.code}: {error.message} ({time.monotonic() - started:.1f}s)')
            return
        print(f'[{label}] extract -> success in {time.monotonic() - started:.1f}s | api_user={result["api_user"]} | session={len(result["session"])} chars | '
              f'cookies={sorted(result["cookies"])} | quota=${result["quota"]}')
        credentials = {'session': result['session'], 'api_user': result['api_user']}
        started = time.monotonic()
        try:
            validated = await service.validate(credentials, route, settings)
            print(f'[{label}] validate -> valid in {time.monotonic() - started:.1f}s | quota=${validated["quota"]}')
        except TaskError as error:
            print(f'[{label}] validate -> {error.code}: {error.message} ({time.monotonic() - started:.1f}s)')
            return
        if do_checkin:
            started = time.monotonic()
            try:
                checked = await service.checkin(credentials, route, settings)
                print(f'[{label}] checkin -> {checked["code"]}: {checked["message"]} in {time.monotonic() - started:.1f}s | quota=${checked["quota"]}')
                for line in checked['logs']:
                    print('    ', line)
            except TaskError as error:
                print(f'[{label}] checkin -> {error.code}: {error.message} ({time.monotonic() - started:.1f}s)')

    if proxy_line:
        config = parse_proxy(proxy_line)
        if config['scheme'] == 'auto':
            config['scheme'] = await detect_protocol(config, 10)
        async with ProxyBridge(config, settings.connect_timeout) as route:
            await run(route, f'proxy:{config["scheme"]}')
    else:
        await run(None, 'direct')


asyncio.run(main())
