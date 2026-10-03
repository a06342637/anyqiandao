"""Screenshot the new UI pieces against the local dev backend using Edge."""
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
        await page.goto(BASE, wait_until='networkidle')
        await page.fill('input[name=password]', PASSWORD)
        await page.click('button[type=submit]')
        await page.wait_for_selector('.app-shell', timeout=15000)
        await page.wait_for_timeout(1000)
        await page.click('.queue-meta .text-button')
        await page.wait_for_timeout(500)
        await page.screenshot(path=OUT / 'confirm-cancel-all.png')
        await page.keyboard.press('Escape')
        await page.click('.sidebar nav button:has-text("账号列表")')
        await page.wait_for_timeout(1200)
        await page.screenshot(path=OUT / 'accounts-copy.png', full_page=False)
        await page.click('.sidebar nav button:has-text("签到管理")')
        await page.wait_for_timeout(1000)
        await page.screenshot(path=OUT / 'schedules-auto.png', full_page=False)
        await page.click('button[aria-label="打开设置"]')
        await page.wait_for_timeout(800)
        await page.screenshot(path=OUT / 'settings-automation.png', full_page=False)
        await page.click('.settings-tabs button:nth-child(2)')
        await page.wait_for_timeout(600)
        await page.click('.proxy-toolbar .button.primary')
        await page.wait_for_timeout(400)
        await page.fill('.paste-field input', 'gate.kookeey.info:1000:8882779-5898d885:03b62e37-US')
        await page.wait_for_timeout(600)
        await page.screenshot(path=OUT / 'proxy-paste.png', full_page=False)
        values = await page.evaluate("() => [...document.querySelectorAll('.form-grid input, .form-grid select')].map(e => e.type === 'password' ? (e.value ? '***' : '') : e.value)")
        print('proxy editor fields:', values)
        await browser.close()


asyncio.run(main())
