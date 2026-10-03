import asyncio
import json
import re
from pathlib import Path
from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / '.local/ui-v3/screenshots'
OUTPUT.mkdir(parents=True, exist_ok=True)


async def login(page):
    await page.goto('http://127.0.0.1:18791', wait_until='networkidle')
    await page.get_by_label('管理员账号', exact=True).fill('admin')
    await page.get_by_label('管理员密码', exact=True).fill('v3-ui-only-password')
    await page.get_by_role('button', name='登录', exact=True).click()
    await expect(page.locator('.app-shell')).to_be_visible()


async def main():
    checks, errors = [], []
    async with async_playwright() as runtime:
        browser = await runtime.chromium.launch(channel='msedge', headless=True)
        context = await browser.new_context(viewport={'width': 1600, 'height': 1000}, locale='zh-CN')
        page = await context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            await login(page)
            await page.get_by_role('button', name='账号列表', exact=True).click()
            await expect(page.locator('.account-table tbody tr').first).to_be_visible()
            checkbox = await page.locator('.account-table tbody .check-cell input').first.bounding_box()
            handle = await page.locator('.account-table tbody .drag-handle').first.bounding_box()
            assert abs(checkbox['y'] + checkbox['height'] / 2 - handle['y'] - handle['height'] / 2) < 2
            settings = await page.locator('.sidebar-bottom .nav-settings').bounding_box()
            assert settings['y'] > 850 and settings['y'] + settings['height'] <= 1001, settings
            checks.append('selection/drag alignment and bottom settings')
            await page.screenshot(path=OUTPUT / 'accounts-desktop.png')
            await page.locator('.pagination select').select_option('5')
            await expect(page.locator('.account-table tbody tr')).to_have_count(5)
            first = await page.locator('.account-identity strong').first.inner_text()
            await page.get_by_role('button', name='下一页', exact=True).click()
            await expect(page.locator('.account-identity strong').first).not_to_have_text(first)
            await page.get_by_role('button', name='上一页', exact=True).click()
            await expect(page.locator('.account-identity strong').first).to_have_text(first)
            checks.append('page size and previous/next')
            row = page.locator('.account-table tbody tr').first
            await row.get_by_role('button', name='编辑', exact=True).click()
            await page.get_by_label('备注', exact=True).fill('已编辑 · 界面验收')
            await page.get_by_role('button', name='保存', exact=True).click()
            await expect(row.locator('.account-note')).to_have_text('已编辑 · 界面验收')
            await row.locator('.account-balance button').click()
            await expect(page.locator('.toast.success').filter(has_text='余额 $125.7500')).to_be_visible(timeout=20000)
            checks.append('edit and live balance refresh feedback')
            await page.locator('.account-table tbody tr').nth(1).get_by_role('button', name='签到', exact=True).click()
            await page.get_by_role('button', name='开始签到', exact=True).click()
            await expect(page.locator('.toast.success').filter(has_text='签到前 $125.7500')).to_be_visible(timeout=20000)
            checks.append('check-in recovery and before/after feedback')
            names = await page.locator('.account-identity strong').all_inner_texts()
            await row.locator('.drag-handle').focus()
            await page.keyboard.press('Alt+ArrowDown')
            await expect(page.locator('.account-identity strong').first).to_have_text(names[1])
            await expect(page.locator('.account-table tbody .drag-handle').first).to_be_enabled()
            await page.locator('.account-table tbody .drag-handle').first.drag_to(page.locator('.account-table tbody tr').nth(2), target_position={'x': 100, 'y': 20})
            await expect(page.locator('.account-identity strong').first).not_to_have_text(names[1])
            checks.append('keyboard and drag/drop sorting')
            await page.get_by_role('button', name='仪表盘', exact=True).click()
            await expect(page.locator('.metric-grid')).to_be_visible()
            for label in ('近 7 天', '近 30 天', '今天'):
                await page.get_by_role('button', name=label, exact=True).click()
                await expect(page.locator('.segmented.inline button.selected')).to_have_text(label)
            await page.screenshot(path=OUTPUT / 'dashboard-desktop.png')
            checks.append('day/week/month dashboard')
            await page.locator('.sidebar-bottom .nav-settings').click()
            await expect(page.locator('.toast.error')).to_have_count(0)
            for attempt in range(4):
                if await page.locator('.toast .icon-button').count():
                    await page.locator('.toast .icon-button').first.click()
            await page.get_by_role('button', name='版本更新', exact=True).click()
            await expect(page.get_by_text('未配置更新源', exact=True)).to_be_visible()
            await expect(page.get_by_role('button', name='更新版本', exact=True)).to_be_disabled()
            await page.screenshot(path=OUTPUT / 'settings-updates.png')
            await page.get_by_role('button', name='备份与恢复', exact=True).click()
            await page.screenshot(path=OUTPUT / 'settings-backup.png')
            await page.keyboard.press('Escape')
            checks.append('update safety and backup panels')
            await page.get_by_role('button', name='运行日志', exact=True).click()
            await page.locator('.category-chip').filter(has_text=re.compile('^错误')).click()
            await expect(page.locator('.log-list')).to_be_visible()
            await page.get_by_role('button', name='刷新', exact=True).click()
            await expect(page.locator('.toast.success').filter(has_text='日志已刷新')).to_be_visible()
            await page.screenshot(path=OUTPUT / 'logs-desktop.png')
            checks.append('categorized log refresh')
            phone = await context.new_page()
            await phone.set_viewport_size({'width': 390, 'height': 844})
            await phone.goto('http://127.0.0.1:18791', wait_until='networkidle')
            for navigation in ('账号列表', '仪表盘'):
                await phone.get_by_role('button', name=navigation, exact=True).click()
                await expect(phone.locator('.page-heading h1')).to_have_text(navigation)
                await phone.screenshot(path=OUTPUT / f'mobile-{navigation}.png', full_page=True)
                assert not await phone.evaluate('document.documentElement.scrollWidth > window.innerWidth + 1'), navigation
            await phone.get_by_role('button', name='打开设置', exact=True).click()
            await phone.get_by_role('button', name='版本更新', exact=True).click()
            await phone.screenshot(path=OUTPUT / 'mobile-updates.png')
            checks.append('mobile layouts')
            assert not errors, errors
            print(json.dumps({'checks': checks, 'page_errors': errors}, ensure_ascii=False, indent=2))
        except Exception:
            await page.screenshot(path=OUTPUT / 'failure.png', full_page=True)
            print(json.dumps({'completed': checks, 'errors': errors}, ensure_ascii=False))
            raise
        finally:
            await browser.close()


asyncio.run(main())
