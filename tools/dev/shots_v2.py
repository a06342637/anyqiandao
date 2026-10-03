"""Screenshot the v0.2.0 pages against the local dev backend using Edge."""
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / '.local' / 'dev' / 'shots'
OUT.mkdir(parents=True, exist_ok=True)
PASSWORD = next(line.split('：', 1)[1].strip() for line in (ROOT / '.local' / 'dev' / '部署信息.txt').read_text(encoding='utf-8').splitlines() if line.startswith('管理员密码'))
BASE = 'http://127.0.0.1:8000'


async def main():
    async with async_playwright() as runtime:
        browser = await runtime.chromium.launch(channel='msedge', headless=True)
        context = await browser.new_context(viewport={'width': 1440, 'height': 900}, locale='zh-CN')
        page = await context.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        await page.goto(BASE, wait_until='networkidle')
        await page.fill('input[name=password]', PASSWORD)
        await page.click('button[type=submit]')
        await page.wait_for_selector('.app-shell', timeout=15000)
        await page.wait_for_timeout(1200)
        await page.screenshot(path=OUT / 'v2-dashboard.png', full_page=True)
        await page.click('.sidebar nav button:has-text("仪表盘")'); await page.wait_for_timeout(1500); await page.screenshot(path=OUT / 'v2-dashboard-page.png', full_page=True)
        await page.click('.sidebar nav button:has-text("账号列表")')
        await page.wait_for_timeout(1200)
        await page.screenshot(path=OUT / 'v2-accounts.png', full_page=True)
        edit = page.locator('button[aria-label^="编辑"]').first
        if await edit.count():
            await edit.click(); await page.wait_for_timeout(500)
            await page.screenshot(path=OUT / 'v2-edit.png')
            await page.keyboard.press('Escape')
        await page.click('.sidebar nav button:has-text("运行日志")')
        await page.wait_for_timeout(1000)
        await page.screenshot(path=OUT / 'v2-logs.png', full_page=True)
        await page.click('.sidebar nav button:has-text("设置")')
        await page.wait_for_timeout(800)
        await page.screenshot(path=OUT / 'v2-settings.png')
        tabs = page.locator('.settings-tabs button')
        print('settings tabs:', [await tabs.nth(i).inner_text() for i in range(await tabs.count())])
        for i in range(await tabs.count()):
            text = await tabs.nth(i).inner_text()
            if '备份' in text:
                await tabs.nth(i).click(); await page.wait_for_timeout(500)
                await page.screenshot(path=OUT / 'v2-backup.png')
        print('page errors:', errors)
        await browser.close()


asyncio.run(main())
