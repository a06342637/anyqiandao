"""Dump the interactive structure of the live login page (no credentials involved)."""
import asyncio
import sys

from playwright.async_api import async_playwright


async def dump(page, label):
    print(f'\n--- {label} | url={page.url} ---')
    print('title:', await page.title())
    items = await page.evaluate('''() => {
      const visible = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
      const out = [];
      for (const el of document.querySelectorAll('input, button, [role=button], a[href]')) {
        if (!visible(el)) continue;
        out.push([el.tagName.toLowerCase(), el.getAttribute('type') || '', (el.getAttribute('placeholder') || '').trim(), (el.innerText || el.value || '').trim().replace(/\\s+/g, ' ').slice(0, 60), el.getAttribute('aria-label') || '', el.getAttribute('name') || '']);
      }
      const dialogs = [...document.querySelectorAll('[role=dialog], .semi-modal, .modal, .ant-modal')].filter(visible).map(d => d.innerText.trim().replace(/\\s+/g, ' ').slice(0, 200));
      const iframes = [...document.querySelectorAll('iframe')].map(f => f.src);
      return { out, dialogs, iframes, text: document.body.innerText.trim().replace(/\\s+/g, ' ').slice(0, 600) };
    }''')
    for row in items['out']:
        print('  ', row)
    print('dialogs:', items['dialogs'])
    print('iframes:', items['iframes'])
    print('text:', items['text'])


async def main():
    async with async_playwright() as runtime:
        browser = await runtime.chromium.launch(channel='msedge', headless=True)
        context = await browser.new_context(locale='zh-CN', viewport={'width': 1280, 'height': 800})
        page = await context.new_page()
        await page.goto('https://anyrouter.top/login', wait_until='domcontentloaded', timeout=30000)
        await page.wait_for_timeout(2500)
        await dump(page, 'after load')
        close = page.get_by_role('button', name='关闭公告', exact=True)
        print('\n关闭公告 buttons:', await close.count())
        if await close.count():
            await close.first.click(timeout=5000)
            await page.wait_for_timeout(800)
            await dump(page, 'after closing announcement')
        option = page.get_by_role('button', name='mail 使用 邮箱或用户名 登录', exact=True)
        print('\nemail option (role):', await option.count(), '| by text:', await page.get_by_text('使用 邮箱或用户名 登录', exact=True).count(), '| contains:', await page.get_by_text('邮箱或用户名', exact=False).count())
        target = option if await option.count() else page.get_by_text('邮箱或用户名', exact=False)
        if await target.count():
            await target.first.click(timeout=5000)
            await page.wait_for_timeout(1200)
            await dump(page, 'after choosing email login')
        print('\nusername placeholder exact:', await page.get_by_placeholder('请输入您的用户名或邮箱地址', exact=True).count(), '| password:', await page.get_by_placeholder('请输入您的密码', exact=True).count(), '| 继续 button:', await page.get_by_role('button', name='继续', exact=True).count())
        await page.screenshot(path=sys.argv[1] if len(sys.argv) > 1 else 'D:/any-signin-assistant/.local/dev/shots/anyrouter-login.png')
        await browser.close()


asyncio.run(main())
