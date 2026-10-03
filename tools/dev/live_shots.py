"""Screenshot the deployed login page (light and dark) with the locally installed Edge. No login is performed."""
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

OUT = Path(__file__).resolve().parents[2] / 'docs' / 'screenshots'
OUT.mkdir(parents=True, exist_ok=True)


async def main():
    async with async_playwright() as runtime:
        browser = await runtime.chromium.launch(channel='msedge', headless=True)
        for theme in ('light', 'dark'):
            context = await browser.new_context(viewport={'width': 1440, 'height': 900}, locale='zh-CN', color_scheme=theme)
            page = await context.new_page()
            await page.goto('https://signin.example.com/', wait_until='networkidle', timeout=60000)
            await page.wait_for_selector('.login-card', timeout=20000)
            await page.wait_for_timeout(1500)
            await page.screenshot(path=OUT / f'live-login-{theme}.png')
            print('saved', f'live-login-{theme}.png', '| title:', await page.title(), flush=True)
            await context.close()
        await browser.close()


asyncio.run(main())
