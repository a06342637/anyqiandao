"""Get WAF cookies with a real browser (direct connection), then reuse them from httpx directly and via the rotating proxy."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from playwright.async_api import async_playwright

from app.proxy import parse_proxy

config = parse_proxy(sys.argv[1])
proxy_url = f"socks5://{config['username']}:{config['password']}@{config['host']}:{config['port']}"
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'


def classify(body):
    if body.lstrip().startswith('{'):
        return 'JSON ' + body[:80]
    markers = [m for m in ('acw_sc__', 'arg1', 'captcha', '人机验证') if m in body]
    return f'HTML({len(body)}b) markers={markers}'


async def main():
    async with async_playwright() as runtime:
        browser = await runtime.chromium.launch(channel='msedge', headless=True)
        context = await browser.new_context(user_agent=UA, locale='zh-CN')
        page = await context.new_page()
        await page.goto('https://anyrouter.top/login', wait_until='domcontentloaded')
        await page.wait_for_selector('input[name=username]', timeout=30000)
        cookies = {c['name']: c['value'] for c in await context.cookies(['https://anyrouter.top'])}
        print('browser cookies:', {k: (v[:12] + '…') for k, v in cookies.items()})
        print('browser UA:', await page.evaluate('navigator.userAgent'))
        inpage = await page.evaluate("async () => { const r = await fetch('/api/user/self', {headers: {'Accept': 'application/json'}}); return {status: r.status, body: (await r.text()).slice(0, 100)}; }")
        print('in-page fetch /api/user/self ->', inpage)
        await browser.close()
    headers = {'User-Agent': UA, 'Accept': 'application/json'}
    async with httpx.AsyncClient(cookies=cookies, headers=headers, timeout=30, trust_env=False) as client:
        r = await client.get('https://anyrouter.top/api/user/self')
        print('httpx direct (same IP as browser)      ->', r.status_code, classify(r.text))
    for attempt in range(2):
        async with httpx.AsyncClient(proxy=proxy_url, cookies=cookies, headers=headers, timeout=30, trust_env=False) as client:
            r = await client.get('https://anyrouter.top/api/user/self')
            print(f'httpx via rotating proxy (attempt {attempt + 1}) ->', r.status_code, classify(r.text))
    async with httpx.AsyncClient(cookies={k: v for k, v in cookies.items() if k != 'acw_sc__v2'}, headers=headers, timeout=30, trust_env=False) as client:
        r = await client.get('https://anyrouter.top/api/user/self')
        print('httpx direct WITHOUT acw_sc__v2         ->', r.status_code, classify(r.text))
    async with httpx.AsyncClient(cookies=cookies, timeout=30, trust_env=False) as client:
        r = await client.get('https://anyrouter.top/api/user/self')
        print('httpx direct with cookies, default UA   ->', r.status_code, classify(r.text))


asyncio.run(main())
