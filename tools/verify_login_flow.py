"""Drive RouterService.extract against the live login page with one synthetic (non-existent) account.

Uses the locally installed Edge instead of the bundled Chromium. The expected outcome is that the
announcement dialog is dismissed, the form is filled and submitted, and the site's rejection is
classified as invalid_credentials. No real account is used.
"""
import asyncio
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.async_api import async_playwright as real_playwright  # noqa: E402

import app.router_service as router_service  # noqa: E402
from app.errors import TaskError  # noqa: E402
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


router_service.async_playwright = EdgePlaywright


async def main():
    username = f'qa-{secrets.token_hex(4)}@example.invalid'
    started = time.monotonic()
    try:
        result = await router_service.RouterService().extract(username, 'not-a-real-password-' + secrets.token_hex(6), None, RuntimeSettings())
        print('UNEXPECTED success for a non-existent account:', bool(result))
        raise SystemExit(1)
    except TaskError as error:
        elapsed = round(time.monotonic() - started, 1)
        print(f'classification: {error.code} | {error.message} | {elapsed}s')
        if error.code != 'invalid_credentials':
            raise SystemExit(1)
    print('PASS: live login form is driven to the end and the rejection is classified correctly.')


asyncio.run(main())
