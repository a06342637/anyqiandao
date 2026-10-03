"""Browser-side adapter for anyrouter.top.

Every request to the site — login, profile check and the sign-in call made by the vendored
script — is issued from inside a real Chromium page with `fetch`, so it shares the page's
connection, cookies and exit IP. The site sits behind a WAF whose JS challenge cookie is bound
to the client IP; requests made from a separate HTTP client (or through a rotating proxy) would
be challenged again, which is exactly what the in-page approach avoids.
"""
import asyncio
import json
import math
import secrets
import sys
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import urlsplit

from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright

from app.config import TARGET_ORIGIN
from app.checkin_state import balance_credit, fixed_reward
from app.console_data import read_console
from app.errors import TaskError
from app.proxy import allowed_destination
from app.responses import checkin_result, is_waf_challenge, profile_result
from app.vendor import anyrouter_checkin

anyrouter_checkin.print = lambda *args, **kwargs: None
DEBUG = bool(__import__('os').environ.get('ANY_BROWSER_DEBUG'))
CHECKIN_PATH = '/api/user/sign_in'
REQUEST_MARKER = 'x-any-assistant-checkin'

FETCH = '''async ({path, method, headers, body, timeout}) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(path, {method, headers, body: body ?? undefined, credentials: 'include', cache: 'no-store', redirect: method === 'GET' ? 'follow' : 'manual', signal: controller.signal});
    const text = await response.text();
    return {status: response.status, type: response.type, text: text.slice(0, 1048576)};
  } finally {
    clearTimeout(timer);
  }
}'''


def quota_of(profile, field='quota'):
    quota = profile.get(field)
    if not isinstance(quota, (int, float)) or isinstance(quota, bool):
        return None
    try:
        amount = quota / 500000
        return amount if math.isfinite(amount) else None
    except OverflowError:
        return None


def api_headers(credentials):
    return {'New-Api-User': str(credentials['api_user']), 'Accept': 'application/json', 'Cache-Control': 'no-store'}


def checkin_gain(before, after):
    return balance_credit({'quota': quota_of(before), 'used_quota': quota_of(before, 'used_quota')},
                          {'quota': quota_of(after), 'used_quota': quota_of(after, 'used_quota')})


def navigation_error(error):
    text = str(error)
    return any(marker in text for marker in ('Execution context was destroyed', 'Cannot find context', 'navigating', 'detached', 'Frame was detached'))


def translate(error, stage=''):
    text = str(error)
    if DEBUG:
        print(f'[browser error during {stage}] {text[:300]}', flush=True)
    if 'Executable doesn' in text or 'playwright install' in text:
        return TaskError('browser_missing', '浏览器运行环境未安装，请运行 python -m playwright install chromium')
    if any(marker in text for marker in ('net::ERR_', 'ECONNRESET', 'ECONNREFUSED', 'ETIMEDOUT', 'Failed to fetch', 'NetworkError')):
        return TaskError('network_error', '浏览器网络连接中断，可更换代理', retry_proxy=True)
    return TaskError('login_error', '登录页面发生变化或浏览器操作失败，请检查网站')


class BrowserSession:
    def __init__(self, route, settings):
        self.route = route
        self.settings = settings
        self.manager = None
        self.browser = None
        self.context = None
        self.page = None
        self.user_agent = ''
        self.request_marker = secrets.token_urlsafe(24)

    async def __aenter__(self):
        self.manager = async_playwright()
        runtime = await self.manager.__aenter__()
        try:
            proxy = self.route.browser_proxy if self.route else None
            arguments = ['--disable-quic', '--disable-background-networking', '--disable-component-update',
                         '--disable-blink-features=AutomationControlled', '--force-webrtc-ip-handling-policy=disable_non_proxied_udp']
            if proxy:
                arguments.append('--host-resolver-rules=MAP * ~NOTFOUND,EXCLUDE localhost,EXCLUDE 127.0.0.1')
            self.browser = await runtime.chromium.launch(headless=True, proxy=proxy, args=arguments)
            major = self.browser.version.split('.')[0]
            platform = 'Windows NT 10.0; Win64; x64' if sys.platform == 'win32' else 'X11; Linux x86_64'
            self.user_agent = f'Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36'
            self.context = await self.browser.new_context(locale='zh-CN', viewport={'width': 1280, 'height': 800}, proxy=proxy,
                                                          service_workers='block', accept_downloads=False, user_agent=self.user_agent)
            self.context.set_default_timeout(self.settings.connect_timeout * 1000)

            await self.context.route('**/*', self.protect_destination)
            self.page = await self.context.new_page()
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def protect_destination(self, request_route):
        request = request_route.request
        address = urlsplit(request.url)
        if address.hostname and not allowed_destination(address.hostname):
            await request_route.abort()
        elif address.hostname == urlsplit(TARGET_ORIGIN).hostname and address.path == CHECKIN_PATH:
            headers = dict(request.headers)
            marker = headers.pop(REQUEST_MARKER, None)
            if request.method != 'POST' or marker != self.request_marker:
                await request_route.abort()
            else:
                await request_route.continue_(headers=headers)
        else:
            await request_route.continue_()

    async def __aexit__(self, *args):
        # Closing must never stall the serial queue: each stage is bounded, and stopping the driver kills the browser regardless.
        try:
            if self.context:
                await asyncio.wait_for(self.context.unroute('**/*'), 5)
            if self.browser:
                await asyncio.wait_for(self.browser.close(), 15)
        except (Exception, asyncio.TimeoutError):
            pass
        finally:
            self.browser = None
            self.context = None
            if self.manager:
                try:
                    await asyncio.wait_for(self.manager.__aexit__(None, None, None), 15)
                except (Exception, asyncio.TimeoutError):
                    pass

    async def open(self, path, deadline):
        # Navigation covers the whole page load through the proxy, so it gets the remaining login budget rather than the connect timeout.
        budget = max(5.0, min(deadline - time.monotonic(), self.settings.login_timeout))
        try:
            await self.page.goto(TARGET_ORIGIN + path, wait_until='domcontentloaded', timeout=budget * 1000)
        except BrowserTimeout:
            if await self.challenge_visible():
                raise TaskError('needs_manual', '网站要求人工验证，未更换代理') from None
            raise TaskError('network_error', '网站页面连接超时，可切换代理', retry_proxy=True) from None
        await self.settle(deadline)

    async def settle(self, deadline):
        # The WAF challenge page reloads itself once its script has set the cookie; wait until the real page is showing.
        while True:
            try:
                if not is_waf_challenge(await self.page.content()):
                    return
            except BrowserError as error:
                if not navigation_error(error):
                    raise
            if time.monotonic() > deadline:
                raise TaskError('upstream_error', '网站验证页面未在规定时间内完成，可更换代理重试', retry_proxy=True)
            await asyncio.sleep(0.3)

    async def quietly(self, operation):
        try:
            return await operation()
        except BrowserError as error:
            if navigation_error(error):
                return None
            raise

    async def challenge_visible(self):
        for selector in ('iframe[src*="captcha"]', 'iframe[src*="challenges.cloudflare.com"]', '#nocaptcha', '.nc-container'):
            locator = self.page.locator(selector)
            count = await self.quietly(locator.count) or 0
            for position in range(count):
                if await self.quietly(locator.nth(position).is_visible):
                    return True
        for text in ('请完成验证', '验证您是真人', 'Verify you are human', '人机验证'):
            if await self.quietly(self.page.get_by_text(text, exact=False).count):
                return True
        return False

    async def dismiss_dialogs(self):
        # The site opens a "系统公告" dialog over the login form; anything left open swallows the submit click.
        for locator in (self.page.get_by_role('button', name='关闭公告', exact=True),
                        self.page.get_by_role('button', name='今日关闭', exact=True),
                        self.page.locator('[role="dialog"] button[aria-label="close"]')):
            if await self.quietly(locator.count) and await self.quietly(locator.first.is_visible):
                try:
                    await locator.first.click(timeout=3000)
                except (BrowserTimeout, BrowserError):
                    continue
                await asyncio.sleep(0.4)
                return True
        return False

    async def api(self, path, *, method='GET', headers=None, body=None, timeout=30000, deadline=None):
        deadline = deadline or time.monotonic() + self.settings.login_timeout
        request_headers = dict(headers or {})
        if path == CHECKIN_PATH and method == 'POST':
            request_headers[REQUEST_MARKER] = self.request_marker
        payload = {'path': path, 'method': method, 'headers': request_headers, 'body': body, 'timeout': timeout}
        for attempt in range(4):
            try:
                result = await self.page.evaluate(FETCH, payload)
            except BrowserError as error:
                if method != 'GET':
                    raise TaskError('uncertain', '签到请求可能已发送，但响应未确认；不会立即重复提交') from None
                if navigation_error(error) and attempt < 3:
                    await self.settle(deadline)
                    continue
                raise translate(error) from None
            if result.get('type') == 'opaqueredirect' or 300 <= result['status'] < 400:
                raise TaskError('uncertain', '签到请求返回重定向，未确认领取；不会自动跟随或重复提交')
            if is_waf_challenge(result['text']):
                if method != 'GET':
                    raise TaskError('uncertain', '签到请求返回验证页面，结果未确认；不会自动重发或切换代理')
                await self.page.reload(wait_until='domcontentloaded')
                await self.settle(deadline)
                continue
            return result['status'], result['text']
        raise TaskError('upstream_error', '网站反复返回验证页面，本次未判定凭证失效；可更换代理', retry_proxy=True)

    async def cookies(self):
        cookie_list = await self.context.cookies([TARGET_ORIGIN])
        return {cookie['name']: cookie['value'] for cookie in cookie_list if cookie.get('value')}


class PageClient:
    """Minimal httpx-like client handed to the vendored script; the request is performed by the page."""

    def __init__(self, session, loop):
        self.session = session
        self.loop = loop
        self.response = None
        self.sent = False

    def post(self, url, *, headers, timeout):
        address = urlsplit(url)
        if address.scheme != 'https' or address.hostname != urlsplit(TARGET_ORIGIN).hostname or address.path != '/api/user/sign_in':
            raise TaskError('script_error', '脚本目标地址不在允许范围内')
        self.sent = True
        future = asyncio.run_coroutine_threadsafe(
            self.session.api(address.path, method='POST', headers=dict(headers), timeout=int(timeout * 1000)), self.loop)
        status, text = future.result(timeout + 15)
        self.response = SimpleNamespace(status_code=status, text=text, json=lambda: json.loads(text))
        return self.response


class RouterService:
    @asynccontextmanager
    async def signed_in(self, credentials, route, settings):
        if not credentials or not credentials.get('session') or not str(credentials.get('api_user', '')).isdigit():
            raise TaskError('invalid', '尚未获取凭证，请先提取')
        try:
            async with BrowserSession(route, settings) as session:
                await session.context.add_cookies([{'name': 'session', 'value': credentials['session'], 'domain': urlsplit(TARGET_ORIGIN).hostname,
                                                    'path': '/', 'secure': True, 'httpOnly': True, 'sameSite': 'Lax'}])
                deadline = time.monotonic() + settings.login_timeout
                await session.open('/login', deadline)
                yield session
        except TaskError:
            raise
        except BrowserTimeout:
            raise TaskError('network_error', '网站页面加载超时，可切换代理', retry_proxy=True) from None
        except BrowserError as error:
            raise translate(error, 'extract') from None

    async def validate(self, credentials, route, settings):
        async with self.signed_in(credentials, route, settings) as session:
            status, text = await session.api('/api/user/self', headers=api_headers(credentials))
            profile = profile_result(status, text, credentials['api_user'])
            return {'profile': profile, 'quota': quota_of(profile)}

    async def insights(self, credentials, route, settings, options):
        async with self.signed_in(credentials, route, settings) as session:
            return await read_console(session, credentials, settings, options)

    async def checkin(self, credentials, route, settings, *, before_submit=None):
        async with self.signed_in(credentials, route, settings) as session:
            status, text = await session.api('/api/user/self', headers=api_headers(credentials))
            before = profile_result(status, text, credentials['api_user'])
            observed_before = time.time()
            if before_submit:
                observed = before_submit(before, observed_before)
                if observed:
                    return observed
            client = PageClient(session, asyncio.get_running_loop())
            provider = SimpleNamespace(domain=TARGET_ORIGIN, sign_in_path='/api/user/sign_in')
            headers = api_headers(credentials) | {'Origin': TARGET_ORIGIN, 'Referer': TARGET_ORIGIN + '/console'}
            claimed_at = time.time()
            try:
                async with asyncio.timeout(75):
                    await asyncio.to_thread(anyrouter_checkin.execute_check_in, client, 'selected-account', provider, headers)
            except TimeoutError:
                if client.sent:
                    raise TaskError('uncertain', '脚本运行超时，签到结果未确认，不自动重发') from None
                raise TaskError('script_error', '内置签到脚本未正常结束') from None
            except TaskError:
                raise
            except Exception:
                raise TaskError('uncertain' if client.sent else 'script_error', '签到请求已发送但结果未确认；不会立即重复提交' if client.sent else '内置签到脚本执行异常') from None
            if client.response is None:
                raise TaskError('script_error', '脚本没有返回响应')
            receipt_code, message = checkin_result(client.response.status_code, client.response.text)
            code = receipt_code
            receipt = json.loads(client.response.text)
            receipt_message = str(receipt.get('message', receipt.get('msg', ''))).strip()
            for value in (credentials.get('session'), str(credentials.get('api_user', ''))):
                if value:
                    receipt_message = receipt_message.replace(value, '[已隐藏]')
            receipt_message = ''.join(character for character in receipt_message if character.isprintable())[:300]
            logs = ['已在浏览器会话中执行固定版本的内置签到脚本；仅提交一次领取请求',
                    f'网站签到接口返回 HTTP {client.response.status_code}；success={receipt.get("success")}；message={receipt_message or "（空）"}']
            quota_before = quota_of(before)
            quota_after = None
            after = {}
            observed_after = observed_before
            gain = None
            for attempt in range(3):
                try:
                    status, text = await session.api('/api/user/self', headers=api_headers(credentials))
                    after = profile_result(status, text, credentials['api_user'])
                    observed_after = time.time()
                    quota_after = quota_of(after)
                    gain = checkin_gain(before, after)
                except TaskError:
                    logs.append('领取请求已返回，但最新余额读取失败；不会重发签到请求')
                    break
                if code in ('already_signed', 'signed') or fixed_reward(gain):
                    break
                if attempt < 2:
                    await asyncio.sleep(1)
            if code == 'accepted':
                if fixed_reward(gain):
                    code, message = 'signed', '签到成功，余额变化加回期间消耗后确认新增 $25.0000'
                else:
                    code, message = 'uncertain', '接口已返回，未确认本轮固定 $25 到账；将复核北京时间今日记录，其他金额增加不直接视为签到'
            logs.append(message)
            if quota_before is not None and quota_after is not None:
                delta = round(quota_after - quota_before, 4)
                logs.append(f'签到前 ${quota_before:.4f} → 签到后 ${quota_after:.4f}，余额变化 {delta:+.4f}')
            used_before, used_after = quota_of(before, 'used_quota'), quota_of(after, 'used_quota')
            if used_before is not None and used_after is not None:
                logs.append(f'累计消耗 ${used_before:.4f} → ${used_after:.4f}；期间消耗 {used_after - used_before:+.4f}')
            if code == 'signed':
                logs.append('签到奖励按固定 $25.0000 记录；其他余额划转不混入签到统计')
            return {'code': code, 'message': message, 'quota': quota_after if quota_after is not None else quota_before,
                    'quota_before': quota_before, 'quota_after': quota_after,
                    'used_before': used_before, 'used_after': used_after, 'logs': logs,
                    'receipt_code': receipt_code, 'verified_gain': gain,
                    'reward_amount': 25.0 if code == 'signed' else None,
                    'observed_before': observed_before, 'observed_after': observed_after, 'claimed_at': claimed_at}

    async def extract(self, username, password, route, settings):
        try:
            async with BrowserSession(route, settings) as session:
                page = session.page
                observed = {'login': None, 'profile': None, 'login_status': None}

                async def capture(response):
                    address = urlsplit(response.url)
                    if address.hostname != urlsplit(TARGET_ORIGIN).hostname or address.path not in ('/api/user/login', '/api/user/self'):
                        return
                    try:
                        body = await response.text()
                        if len(body) > 1048576 or is_waf_challenge(body):
                            return
                        if address.path == '/api/user/login':
                            observed['login_status'] = response.status
                            observed['login'] = json.loads(body)
                        elif response.status == 200:
                            observed['profile'] = profile_result(response.status, body, None)
                    except Exception:
                        return
                page.on('response', capture)
                deadline = time.monotonic() + settings.login_timeout
                await session.open('/login', deadline)
                username_box = page.get_by_placeholder('请输入您的用户名或邮箱地址', exact=True)
                while True:
                    if time.monotonic() > deadline:
                        raise TaskError('login_error', '未找到登录表单，网站可能调整了页面或要求人工验证')
                    if await session.challenge_visible():
                        raise TaskError('needs_manual', '登录页面需要验证码或人工验证，已跳过')
                    if await session.dismiss_dialogs():
                        continue
                    if await session.quietly(username_box.is_visible):
                        break
                    email_option = page.get_by_role('button', name='mail 使用 邮箱或用户名 登录', exact=True)
                    if not await session.quietly(email_option.count):
                        email_option = page.get_by_text('使用 邮箱或用户名 登录', exact=True)
                    if await session.quietly(email_option.count) and await session.quietly(email_option.first.is_visible):
                        await email_option.first.click()
                    await asyncio.sleep(0.25)
                await username_box.fill(username)
                await page.get_by_placeholder('请输入您的密码', exact=True).fill(password)
                submit = page.get_by_role('button', name='继续', exact=True)
                try:
                    await submit.click(timeout=5000)
                except BrowserTimeout:
                    await session.dismiss_dialogs()
                    await submit.click()
                deadline = time.monotonic() + settings.login_timeout
                while time.monotonic() < deadline:
                    if await session.challenge_visible():
                        raise TaskError('needs_manual', '登录触发验证码或风控，已跳过')
                    login = observed['login']
                    if observed['login_status'] in (403, 429):
                        raise TaskError('needs_manual', '网站拒绝登录或限制请求频率，需要人工处理')
                    if isinstance(login, dict) and login.get('success') is False:
                        message = str(login.get('message', login.get('msg', '')))
                        if any(marker in message for marker in ('验证', '风控', '频繁', '次数')):
                            raise TaskError('needs_manual', '网站要求验证或限制登录频率，已跳过')
                        raise TaskError('invalid_credentials', '网站拒绝了登录，请核对账号密码或账号状态')
                    user_id = None
                    if observed['profile']:
                        user_id = observed['profile']['id']
                    elif isinstance(login, dict) and login.get('success') is True:
                        candidate = login.get('data')
                        if isinstance(candidate, dict) and str(candidate.get('id', '')).isdigit():
                            user_id = candidate['id']
                    if user_id is not None:
                        status, text = await session.api('/api/user/self', headers={'New-Api-User': str(user_id), 'Accept': 'application/json'}, deadline=deadline)
                        profile = profile_result(status, text, user_id)
                        cookies = await session.cookies()
                        if cookies.get('session'):
                            return {'session': cookies['session'], 'api_user': str(profile['id']), 'cookies': cookies,
                                    'quota': quota_of(profile), 'user_agent': session.user_agent}
                        raise TaskError('missing_session', '用户资料已返回，但缺少 session Cookie，不判定为提取成功')
                    await asyncio.sleep(0.3)
                raise TaskError('login_error', '登录未在规定时间内完成，请检查网站状态或是否启用了二次验证')
        except TaskError:
            raise
        except BrowserTimeout:
            raise TaskError('login_error', '登录表单或资料校验超时，未确认成功；不会因页面变化盲目切换代理') from None
        except BrowserError as error:
            raise translate(error, 'extract') from None
