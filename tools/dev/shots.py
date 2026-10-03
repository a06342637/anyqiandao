"""Screenshot the dev UI with the locally installed Edge (no browser download)."""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / '.local' / 'dev' / 'shots'
OUT.mkdir(exist_ok=True)
PASSWORD = next(line.split('：', 1)[1].strip() for line in (ROOT / '.local' / 'dev' / '部署信息.txt').read_text(encoding='utf-8').splitlines() if line.startswith('管理员密码'))
BASE = 'http://localhost:5173'
VIEWPORTS = {'desktop': (1440, 900), 'tablet': (900, 1100), 'mobile': (390, 844)}


async def shoot(browser, theme, viewport, pages, full=True):
    width, height = VIEWPORTS[viewport]
    context = await browser.new_context(viewport={'width': width, 'height': height}, locale='zh-CN', device_scale_factor=1)
    await context.add_init_script(f"localStorage.setItem('any-theme','{theme}')")
    page = await context.new_page()
    await page.goto(BASE, wait_until='networkidle')
    if 'login' in pages:
        await page.screenshot(path=OUT / f'login-{theme}-{viewport}.png', full_page=full)
    await page.fill('input[name=password]', PASSWORD)
    await page.click('button[type=submit]')
    await page.wait_for_selector('.app-shell', timeout=15000)
    await page.wait_for_timeout(1200)
    for name in pages:
        if name == 'login':
            continue
        if name == 'settings':
            await page.click('button[aria-label="打开设置"]')
            await page.wait_for_timeout(600)
            await page.screenshot(path=OUT / f'settings-runtime-{theme}-{viewport}.png', full_page=False)
            await page.click('.settings-tabs button:nth-child(2)')
            await page.wait_for_timeout(800)
            await page.screenshot(path=OUT / f'settings-proxies-{theme}-{viewport}.png', full_page=False)
            await page.keyboard.press('Escape')
            await page.wait_for_timeout(300)
            continue
        label = {'extract': '凭证提取', 'accounts': '账号列表', 'schedules': '签到管理', 'logs': '运行日志'}[name]
        await page.click(f'.sidebar nav button:has-text("{label}")')
        await page.wait_for_timeout(1200)
        await page.screenshot(path=OUT / f'{name}-{theme}-{viewport}.png', full_page=full)
    await context.close()


async def main():
    plan = sys.argv[1:] or ['light:desktop:login,extract,accounts,schedules,logs,settings', 'dark:desktop:login,extract,accounts', 'light:mobile:login,extract,accounts', 'light:tablet:accounts']
    async with async_playwright() as runtime:
        browser = await runtime.chromium.launch(channel='msedge', headless=True)
        for item in plan:
            theme, viewport, pages = item.split(':')
            await shoot(browser, theme, viewport, pages.split(','))
            print('done', item, flush=True)
        await browser.close()


asyncio.run(main())
