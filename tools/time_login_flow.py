"""Time each step of the live login flow with a synthetic account to see where the seconds go."""
import asyncio
import secrets
import time

from playwright.async_api import async_playwright


async def main():
    stamp = time.monotonic()
    def mark(label):
        print(f'{time.monotonic() - stamp:6.1f}s  {label}', flush=True)
    async with async_playwright() as runtime:
        browser = await runtime.chromium.launch(channel='msedge', headless=True, args=['--disable-quic'])
        context = await browser.new_context(locale='zh-CN', viewport={'width': 1280, 'height': 800}, service_workers='block')
        page = await context.new_page()
        page.on('response', lambda response: mark(f'response {response.status} {response.url[:70]}') if '/api/user/' in response.url else None)
        page.on('requestfailed', lambda request: mark(f'request failed {request.url[:70]} {request.failure}') if '/api/' in request.url else None)
        mark('browser ready')
        await page.goto('https://anyrouter.top/login', wait_until='domcontentloaded', timeout=30000)
        mark('domcontentloaded')
        close = page.get_by_role('button', name='关闭公告', exact=True)
        while not await close.count():
            await asyncio.sleep(0.2)
        await close.first.click()
        mark('announcement closed')
        await page.get_by_placeholder('请输入您的用户名或邮箱地址', exact=True).fill(f'qa-{secrets.token_hex(4)}@example.invalid')
        await page.get_by_placeholder('请输入您的密码', exact=True).fill('not-a-real-password-' + secrets.token_hex(6))
        mark('form filled')
        async with page.expect_response(lambda response: '/api/user/login' in response.url, timeout=90000) as waiter:
            await page.get_by_role('button', name='继续', exact=True).click()
            mark('submit clicked')
        response = await waiter.value
        body = await response.text()
        mark(f'login response {response.status}: {body[:160]}')
        await browser.close()


asyncio.run(main())
