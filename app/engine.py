import asyncio
import hashlib
import sqlite3
import time
from datetime import datetime

import httpx

from app.config import TARGET_ORIGIN
from app.checkin_state import BEIJING, beijing_day, confirmed_today, fixed_reward
from app.db import checkin_earned
from app.errors import HostKeyRequired, TaskError
from app.proxy import ProxyBridge, detect_protocol
from app.router_service import RouterService, quota_of
from app.worker_lock import WorkerLock
from app.network_routes import OPERATION_LABELS, route_for
from app.resources import memory_status


class Engine:
    def __init__(self, store, service=None):
        self.store = store
        self.service = service or RouterService()
        self.stopping = False
        self.worker_task = None
        self.clock_task = None
        self.running = {}
        self.storage_error = False
        self.resource_wait = ''
        self.loop = None
        self.lock = WorkerLock(store.path.parent / '.worker.lock')

    async def start(self):
        self.lock.acquire()
        self.loop = asyncio.get_running_loop()
        self.stopping = False
        try:
            self.store.recover()
        except BaseException:
            self.lock.release()
            raise
        self.worker_task = asyncio.create_task(self.worker(), name='account-worker')
        self.clock_task = asyncio.create_task(self.clock(), name='persistent-scheduler')

    async def stop(self):
        self.stopping = True
        tasks = [task for _, task in list(self.running.values())] + [task for task in (self.worker_task, self.clock_task) if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.lock.release()

    def cancel(self, job_id):
        entry = self.running.get(job_id)
        if entry:
            self.loop.call_soon_threadsafe(entry[1].cancel)
        else:
            self.store.execute("UPDATE jobs SET status='cancelled',message='已取消',payload_enc=NULL,finished=? WHERE id=? AND status='pending'", (time.time(), job_id))

    async def cancel_and_wait(self, job_id):
        job = self.store.one('SELECT * FROM jobs WHERE id=?', (job_id,))
        if not job:
            return None
        if self.store.execute("UPDATE jobs SET status='cancelled',message='已取消等待任务，未执行',payload_enc=NULL,finished=? WHERE id=? AND status='pending'", (time.time(), job_id)):
            self.log_job(job, '已取消等待任务，未执行')
        entry = self.running.get(job_id)
        if entry:
            task = entry[1]
            if not task.cancelling():
                task.cancel()
            await asyncio.wait({task}, timeout=15)
        return self.store.one('SELECT id,status,message FROM jobs WHERE id=?', (job_id,))

    def log_job(self, job, message, *, kind=None, level='info', category=None):
        route = job.get('_route_label')
        self.store.log(kind or job['kind'], f'[{route}] {message}' if route else message, level=level,
                       job_id=job['id'], account_id=job['account_id'], category=category)

    def finish(self, job, status, message):
        now = time.time()
        with self.store.transaction() as connection:
            changed = connection.execute("UPDATE jobs SET status=?,message=?,payload_enc=NULL,finished=? WHERE id=? AND status IN ('pending','running')", (status, message, now, job['id'])).rowcount
            if not changed:
                return
            if job['kind'] == 'checkin' and job['account_id']:
                connection.execute('UPDATE accounts SET checkin_status=?,last_checkin=?,updated=? WHERE id=?', (status, now, now, job['account_id']))
                self.store.record_checkin(job['account_id'], job['id'], status, None, None, connection=connection)
        level = 'info' if status in ('success', 'signed', 'already_signed', 'cancelled') else 'warning' if status in ('invalid', 'uncertain') else 'error'
        self.log_job(job, message, level=level, category='invalid' if status == 'invalid' else 'error' if level == 'error' else None)

    def claim(self):
        with self.store.transaction() as connection:
            maintenance = connection.execute("SELECT value FROM meta WHERE key='maintenance'").fetchone()
            if maintenance and maintenance['value'] == '1':
                return None
            paused = connection.execute("SELECT value FROM meta WHERE key='queue_paused'").fetchone()['value'] == '1'
            active = {row['id']: dict(row) for row in connection.execute("SELECT id,kind,account_id FROM jobs WHERE status='running'")}
            active.update({identifier: job for identifier, (job, _) in self.running.items()})
            if active:
                return None
            query = "SELECT * FROM jobs WHERE status='pending'" + (" AND kind='proxy_test'" if paused else '')
            job = connection.execute(query + ' ORDER BY created,rowid LIMIT 1').fetchone()
            if not job:
                return None
            connection.execute("UPDATE jobs SET status='running',started=?,message='正在执行' WHERE id=?", (time.time(), job['id']))
            return dict(job)

    async def worker(self):
        last_started = 0
        while not self.stopping:
            try:
                if self.storage_error:
                    await asyncio.sleep(2)
                    continue
                if not self.store.enough_space():
                    self.store.set_meta('queue_paused', '1')
                    self.store.set_meta('pause_reason', '数据盘可用空间不足 64MB，请释放空间后继续')
                    await asyncio.sleep(5)
                    continue
                if not self.running:
                    resources = memory_status()
                    self.resource_wait = '' if resources['ready'] else '内存暂时不足，等待释放后自动继续；不会启动新的浏览器'
                    if self.resource_wait:
                        await asyncio.sleep(3)
                        continue
                settings = self.store.settings()
                gap = settings.account_gap - (time.monotonic() - last_started)
                if gap > 0:
                    await asyncio.sleep(min(0.25, gap))
                    continue
                job = self.claim()
                if not job:
                    await asyncio.sleep(0.5)
                    continue
                self.running[job['id']] = (job, asyncio.create_task(self.run_job(job)))
                last_started = time.monotonic()
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except sqlite3.Error:
                self.storage_error = True
            except Exception as error:
                try:
                    self.store.log('system', f'任务引擎暂时异常（{type(error).__name__}），已停止当前轮询', level='error')
                except sqlite3.Error:
                    self.storage_error = True
                await asyncio.sleep(5)

    async def run_job(self, job):
        try:
            await self.process(job)
        except asyncio.CancelledError:
            if self.stopping:
                raise
            uncertain = job['kind'] == 'checkin' and job.get('_checkin_started')
            self.finish(job, 'uncertain' if uncertain else 'cancelled',
                        '已取消；签到是否已提交需人工确认，不自动重发' if uncertain else '任务已取消')
        except sqlite3.Error:
            self.storage_error = True
        finally:
            self.running.pop(job['id'], None)

    async def process(self, job):
        self.store.log(job['kind'], '任务开始执行', job_id=job['id'], account_id=job['account_id'])
        try:
            if job['kind'] == 'proxy_test':
                await self.test_proxy(job)
                return
            account = self.store.account(job['account_id'])
            if not account:
                self.finish(job, 'cancelled', '账号已删除')
                return
            payload = self.store.vault.open(job['payload_enc'], f'job:{job["id"]}') if job['payload_enc'] else {}
            if job['kind'] == 'insights':
                job['_insight_options'] = payload
            password = payload.get('password') or account['login'].get('password')
            if job['kind'] == 'extract' and payload.get('only_if_invalid') and account['validity'] != 'invalid':
                self.finish(job, 'success', '账号已不再标记为失效，跳过重复提取')
                return
            if job['kind'] == 'extract' and not password:
                raise TaskError('password_required', '没有保存登录密码，请重新输入后提取')
            settings = self.store.settings()
            job['_repair_invalid'] = payload.get('repair_invalid', settings.auto_reextract)
            job['_route_operation'] = payload.get('route_operation', 'extract')
            await self.perform(job, account, password, None, settings, None)

        except TaskError as error:
            if job['account_id']:
                now = time.time()
                if job['kind'] == 'insights':
                    if error.code == 'invalid':
                        login = self.store.account(job['account_id'])['login']
                        message = error.message if login.get('password') else 'Session 凭证已失效，请手动更新 session 与 api_user；不会自动登录'
                        self.store.execute("UPDATE accounts SET validity='invalid',message=?,last_validated=?,updated=? WHERE id=?", (message, now, now, job['account_id']))
                elif job['kind'] in ('validate', 'checkin'):
                    validity = {'invalid': 'invalid', 'needs_manual': 'blocked', 'network_error': 'network_error', 'proxy_error': 'network_error'}.get(error.code)
                    if validity:
                        self.store.execute('UPDATE accounts SET validity=?,message=?,last_validated=?,updated=? WHERE id=?',
                                           (validity, error.message, now, now, job['account_id']))
                    else:
                        self.store.execute('UPDATE accounts SET message=?,updated=? WHERE id=?', (error.message, now, job['account_id']))
                else:
                    self.store.execute('UPDATE accounts SET message=?,updated=? WHERE id=?', (error.message, now, job['account_id']))
            self.finish(job, error.code, error.message)
        except sqlite3.Error:
            self.storage_error = True
        except Exception as error:
            self.finish(job, 'error', f'任务执行异常（{type(error).__name__}）；未覆盖已有凭证')

    async def routed(self, job, account, operation, settings, callback):
        selected = route_for(self.store, settings, job, account, operation)
        label = OPERATION_LABELS[operation]
        async def invoke(route):
            if not memory_status()['ready']:
                raise TaskError('resource_busy', '可用内存不足，本次停止启动浏览器；等待资源释放后重试')
            try:
                async with asyncio.timeout(settings.login_timeout + (90 if operation == 'checkin' else 0)):
                    return await callback(route)
            except TimeoutError:
                if job.get('_checkin_submitted'):
                    raise TaskError('uncertain', '签到请求已发出但操作超时，停止重试，不重复提交') from None
                raise TaskError('network_error', label + '网络操作超时', retry_proxy=True) from None
            except (TaskError, httpx.TransportError, OSError) as error:
                if job.get('_checkin_submitted') and (not isinstance(error, TaskError) or error.retry_proxy):
                    raise TaskError('uncertain', '签到请求已发出但结果未确认，停止重试，不重复提交') from None
                raise
        if selected.mode == 'direct':
            job['_route_label'] = f'{label} · 直连'
            self.log_job(job, '使用服务器网络出口执行')
            return await invoke(None), None
        proxy = self.store.one('SELECT * FROM proxies WHERE id=? AND enabled=1', (selected.proxy_id,))
        if not proxy:
            raise TaskError('proxy_error', '指定代理已删除或停用，请重新选择；未使用其他代理')
        for attempt in range(1, 6):
            job['_route_label'] = f'{label} · 代理：{proxy["name"]}（{proxy["id"][:8]}）'
            self.log_job(job, f'指定代理第 {attempt}/5 次尝试')
            self.store.execute('UPDATE jobs SET message=? WHERE id=?', (f'{label}：代理第 {attempt}/5 次尝试', job['id']))
            try:
                config = await self.proxy_config(proxy, settings)
                async with ProxyBridge(config, settings.connect_timeout, proxy['trusted_key']) as route:
                    result = await invoke(route)
                self.store.execute("UPDATE proxies SET status='healthy',message='连接正常',failed_until=0 WHERE id=?", (proxy['id'],))
                return result, proxy['id']
            except HostKeyRequired as error:
                self.require_host_key(proxy, error)
                raise
            except (TaskError, httpx.TransportError, OSError) as error:
                if isinstance(error, TaskError) and not error.retry_proxy:
                    raise
                if job.get('_checkin_submitted'):
                    raise TaskError('uncertain', '签到请求已发出但结果未确认，停止代理重试与直连补发') from None
                message = error.message if isinstance(error, TaskError) else '代理连接失败或连接中断'
                self.log_job(job, f'第 {attempt}/5 次代理失败：{message}', level='warning')
                if attempt < 5:
                    await asyncio.sleep(min(attempt, 3))
        self.mark_proxy_failure(proxy, '指定代理连续 5 次网络失败，本次已转直连')
        job['_route_label'] = f'{label} · 直连替补'
        self.log_job(job, '指定代理 5 次网络尝试均失败，使用直连完成本次操作', level='warning')
        return await invoke(None), None

    def after_extract(self, job, account):
        settings = self.store.settings()
        if not settings.auto_checkin and job['source'] != 'auto:reextract':
            return
        joined = False
        with self.store.transaction() as connection:
            if settings.auto_checkin:
                schedule_id = self.store.auto_schedule(connection, settings.auto_checkin_interval_minutes)
                joined = connection.execute('INSERT OR IGNORE INTO schedule_accounts VALUES (?,?)', (schedule_id, account['id'])).rowcount
            _, inserted = self.store.enqueue('checkin', account['id'], source='auto:extract', connection=connection)
        if joined:
            self.store.log('extract', '已自动加入默认签到计划', job_id=job['id'], account_id=account['id'])
        if inserted:
            self.store.log('extract', '已自动加入一次签到任务', job_id=job['id'], account_id=account['id'])

    async def proxy_config(self, proxy, settings):
        config = self.store.vault.open(proxy['config_enc'], f'proxy:{proxy["id"]}')
        if config['scheme'] == 'auto':
            config['scheme'] = await detect_protocol(config, settings.connect_timeout)
            self.store.execute('UPDATE proxies SET config_enc=? WHERE id=?', (self.store.vault.seal(config, f'proxy:{proxy["id"]}'), proxy['id']))
        return config

    def require_host_key(self, proxy, error):
        self.store.execute("UPDATE proxies SET status='needs_trust',candidate_key=?,candidate_fingerprint=?,message=?,tested_at=? WHERE id=?",
                           (error.public_key, error.fingerprint, error.message, time.time(), proxy['id']))

    def mark_proxy_failure(self, proxy, message):
        self.store.execute("UPDATE proxies SET status='unavailable',message=?,tested_at=?,failed_until=? WHERE id=?",
                           (message, time.time(), time.time() + 300, proxy['id']))

    async def test_proxy(self, job):
        proxy = self.store.one('SELECT * FROM proxies WHERE id=?', (job['proxy_id'],))
        if not proxy:
            self.finish(job, 'cancelled', '代理已删除')
            return
        settings = self.store.settings()
        started = time.monotonic()
        try:
            config = await self.proxy_config(proxy, settings)
            job['_route_label'] = f'代理：{proxy["name"]} · {config["scheme"]}://{config["host"]}:{config["port"]}'
            self.log_job(job, '开始检测代理连通性')
            async with ProxyBridge(config, settings.connect_timeout, proxy['trusted_key']) as route:
                async with httpx.AsyncClient(proxy=route.url, timeout=settings.connect_timeout, trust_env=False) as client:
                    async with client.stream('GET', TARGET_ORIGIN + '/login') as response:
                        message = f'网络连通（HTTP {response.status_code}）；连通不等于账号登录成功'
            latency = round((time.monotonic() - started) * 1000)
            self.store.execute("UPDATE proxies SET status='healthy',message=?,latency=?,tested_at=?,failed_until=0 WHERE id=?", (message, latency, time.time(), proxy['id']))
            self.finish(job, 'success', message)
        except HostKeyRequired as error:
            self.require_host_key(proxy, error)
            self.finish(job, error.code, error.message)
        except (TaskError, httpx.TransportError, OSError):
            self.mark_proxy_failure(proxy, '代理不可用、认证失败或连接超时')
            self.finish(job, 'proxy_error', '代理不可用、认证失败或连接超时')

    async def perform(self, job, account, password, route, settings, proxy_id):
        if job['kind'] == 'insights':
            operation = 'tokens' if job['_insight_options']['view'] == 'tokens' else 'dashboard'
            result, proxy_id = await self.routed(job, account, operation, settings, lambda active_route: self.service.insights(account['result'], active_route, settings, job['_insight_options']))
            now = time.time()
            label = 'API 令牌' if job['_insight_options']['view'] == 'tokens' else '数据看板'
            with self.store.transaction() as connection:
                profile = result.get('profile') or {}
                if profile.get('quota') is not None:
                    self.store.observe_account(account['id'], account['result']['api_user'], profile['quota'], profile.get('used_quota'),
                                               at=result.get('fetched_at', now), connection=connection)
                    connection.execute('UPDATE accounts SET quota=? WHERE id=?', (profile['quota'], account['id']))
                connection.execute('DELETE FROM console_results WHERE expires<?', (now,))
                connection.execute('INSERT OR REPLACE INTO console_results VALUES (?,?,?,?,?)',
                                   (job['id'], account['id'], self.store.vault.seal(result, f'console:{job["id"]}'), hashlib.sha256(account['result_enc'].encode()).hexdigest(), now + 600))
                connection.execute("UPDATE jobs SET status='success',message=?,payload_enc=NULL,finished=? WHERE id=?", (f'{label}已读取；只读查询，未登录、未签到、未修改令牌', now, job['id']))
            self.log_job(job, f'{label}查询完成，结果加密暂存 10 分钟；日志不包含令牌或 Cookie')
        elif job['kind'] == 'extract':
            result, proxy_id = await self.routed(job, account, job.get('_route_operation', 'extract'), settings, lambda active_route: self.service.extract(account['login']['username'], password, active_route, settings))
            if not result.get('session') or not str(result.get('api_user', '')).isdigit():
                raise TaskError('missing_session', '登录没有返回完整 session 和用户 ID，不判定为成功')
            now = time.time()
            self.complete_account(job, "UPDATE accounts SET result_enc=?,validity='valid',message='凭证提取成功',last_extracted=?,last_validated=?,quota=?,proxy_id=?,updated=? WHERE id=?",
                (self.store.vault.seal(result, f'result:{account["id"]}'), now, now, result.get('quota'), proxy_id, now, account['id']),
                'success', '凭证提取成功，已加入账号列表')
            self.after_extract(job, account)
        elif job['kind'] == 'validate':
            refreshed = False
            try:
                if not account['result']:
                    raise TaskError('invalid', '缺少登录凭证，请先提取')
                result, proxy_id = await self.routed(job, account, 'validate', settings, lambda active_route: self.service.validate(account['result'], active_route, settings))
            except TaskError as error:
                if error.code != 'invalid' or not job.get('_repair_invalid', settings.auto_reextract):
                    raise
                self.store.execute("UPDATE accounts SET validity='invalid',message=?,last_validated=?,updated=? WHERE id=?",
                                   (error.message, time.time(), time.time(), account['id']))
                await self.refresh_credentials(job, account, password, route, settings, proxy_id)
                result, proxy_id = await self.routed(job, account, 'validate', settings, lambda active_route: self.service.validate(account['result'], active_route, settings))
                refreshed = True
            now = time.time()
            message = '失效凭证已自动重新提取并验证有效' if refreshed else '凭证有效'
            self.complete_account(job, "UPDATE accounts SET validity='valid',message=?,last_validated=?,quota=COALESCE(?,quota),proxy_id=?,updated=? WHERE id=?",
                (message, now, result.get('quota'), proxy_id, now, account['id']), 'success', f'检测完成：{message}；未触发签到')
            if result.get('quota') is not None:
                self.store.observe_account(account['id'], account['result']['api_user'], result['quota'], quota_of(result.get('profile') or {}, 'used_quota'), at=now)
        elif job['kind'] == 'checkin':
            refreshed = False
            if not account['result'] or account['validity'] == 'invalid':
                await self.refresh_for_checkin(job, account, password, route, settings, proxy_id)
                refreshed = True
            try:
                result, proxy_id = await self.routed(job, account, 'checkin', settings, lambda active_route: self.checkin_or_observed(job, account, active_route, settings))
            except TaskError as error:
                if error.code != 'invalid' or refreshed:
                    raise
                job['_checkin_started'] = False
                job['_checkin_submitted'] = False
                await self.refresh_for_checkin(job, account, password, route, settings, proxy_id)
                result, proxy_id = await self.routed(job, account, 'checkin', settings, lambda active_route: self.checkin_or_observed(job, account, active_route, settings))
            result = self.resolve_checkin_receipt(account, result, settings)
            now = time.time()
            before, after = result.get('quota_before'), result.get('quota_after')
            message = result['message']
            if before is not None and after is not None:
                message += f'；签到前 ${before:.4f} → 签到后 ${after:.4f}，变化 {after - before:+.4f}'
            elif result['code'] == 'signed' and result.get('reward_amount') is not None:
                message += '；余额未完整读取，网站明确回执确认的签到奖励按固定 $25.0000 记录'
            elif result['code'] in ('signed', 'already_signed'):
                message += '；余额未完整读取，新增金额暂不计入统计'
            self.complete_account(job, "UPDATE accounts SET validity='valid',message=?,last_validated=?,last_checkin=?,checkin_status=?,quota=COALESCE(?,quota),proxy_id=?,updated=? WHERE id=?",
                (result['message'], now, now, result['code'], result.get('quota'), proxy_id, now, account['id']), result['code'], message,
                balance=(before, after), usage=(result.get('used_before'), result.get('used_after')), reward_amount=result.get('reward_amount'))
            for line in result.get('logs', [])[:40]:
                self.log_job(job, line, kind='script')

    async def checkin_or_observed(self, job, account, route, settings):
        state = self.store.daily_state(self.store.account(account['id']))
        if confirmed_today(state, account['result']['api_user'], time.time()):
            result = await self.service.validate(account['result'], route, settings)
            observed = self.checkin_precheck(account, result.get('quota'), quota_of(result.get('profile') or {}, 'used_quota'), time.time())
            if observed:
                return observed
        job['_checkin_started'] = True
        return await self.service.checkin(account['result'], route, settings,
                    before_submit=lambda profile, observed_at: self.checkin_precheck(account, quota_of(profile), quota_of(profile, 'used_quota'), observed_at),
                    on_submit=lambda: job.update(_checkin_submitted=True))

    def checkin_precheck(self, account, quota, used, now):
        identity = account['result']['api_user']
        state = self.store.observe_account(account['id'], identity, quota, used, at=now)
        if not confirmed_today(state, identity, now):
            return None
        checked_at = datetime.fromtimestamp(state.confirmed_at, BEIJING).strftime('%Y-%m-%d %H:%M:%S')
        return {'code': 'already_signed', 'message': '北京时间今日已有签到依据，仅刷新余额，未重复提交签到请求',
                'quota': quota, 'quota_before': quota, 'quota_after': quota, 'used_before': used, 'used_after': used,
                'logs': [f'今日签到依据：{checked_at}（Asia/Shanghai）；{state.evidence}；余额消费不会清除今日已签到记录']}

    def resolve_checkin_receipt(self, account, result, settings):
        now = time.time()
        identity = account['result']['api_user']
        if result.get('receipt_code'):
            before_at = result.get('observed_before', now)
            after_at = result.get('observed_after', now)
            self.store.observe_account(account['id'], identity, result.get('quota_before'), result.get('used_before'), at=before_at)
            confirm = result['code'] in ('signed', 'already_signed') and beijing_day(result.get('claimed_at', before_at)) == beijing_day(after_at)
            self.store.observe_account(account['id'], identity, result.get('quota_after'), result.get('used_after'), at=after_at,
                                       confirm=confirm, evidence='receipt')
        if (result['code'] != 'uncertain' or result.get('receipt_code') != 'accepted'
                or beijing_day(result.get('claimed_at', now)) != beijing_day(now)):
            return result
        state = self.store.daily_state(self.store.account(account['id']))
        if confirmed_today(state, identity, now):
            return {**result, 'code': 'already_signed', 'message': '北京时间今日已观察到固定 $25 到账或明确签到回执，本轮不重复累计',
                    'logs': [*result.get('logs', []), '复核独立每日签到依据；消费导致余额减少不影响今日已签到状态，清理日志也不清除该依据']}
        gain = result.get('verified_gain')
        if gain is None or gain > 0 or not account.get('last_extracted'):
            return result
        start = datetime.fromtimestamp(now, BEIJING).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        since = max(start, account['last_extracted'])
        previous = self.store.all("SELECT * FROM checkins WHERE account_id=? AND code='signed' AND balance_source='live' AND created>=? AND created<=? ORDER BY created DESC,id DESC",
                                  (account['id'], since, now))
        for record in previous:
            earned = checkin_earned(record)
            if earned is None or not fixed_reward(float(earned)):
                continue
            checked_at = datetime.fromtimestamp(record['created'], BEIJING).strftime('%Y-%m-%d %H:%M:%S')
            message = '本轮未新增；今日已有确认到账记录，不重复计为签到成功'
            evidence = f'复核到账记录：{checked_at}（Asia/Shanghai）签到新增 ${earned:.4f}；本轮接口空响应且未新增，记为已签过，不重复累计'
            self.store.observe_account(account['id'], identity, result.get('quota_after'), result.get('used_after'), at=now, confirm=True, evidence='history')
            return {**result, 'code': 'already_signed', 'message': message, 'logs': [*result.get('logs', []), evidence]}
        return result

    async def refresh_for_checkin(self, job, account, password, route, settings, proxy_id):
        await self.refresh_credentials(job, account, password, route, settings, proxy_id)

    async def refresh_credentials(self, job, account, password, route, settings, proxy_id):
        checking_in = job['kind'] == 'checkin'
        operation = '签到' if checking_in else '检测'
        enabled = settings.auto_reextract if checking_in else job.get('_repair_invalid', settings.auto_reextract)
        self.log_job(job, f'{operation}发现 Cookie 失效或缺失', level='warning', category='invalid')
        if job.get('_credential_refresh_attempted'):
            raise TaskError('invalid', '本轮已自动重新提取一次，凭证仍无效；停止重试避免循环登录')
        if not password:
            raise TaskError('invalid', 'Session 导入账号未保存登录密码：凭证已失效，已停止自动处理；请手动更新 session 与 api_user')
        if not enabled:
            reason = '未启用自动重新提取'
            raise TaskError('invalid', f'Cookie 已失效，无法自动重新提取：{reason}')
        self.store.execute('UPDATE jobs SET message=? WHERE id=?',
                           (f'Cookie 已失效，正在自动重新提取，随后继续本次{operation}', job['id']))
        job['_credential_refresh_attempted'] = True
        self.log_job(job, f'开始自动重新提取凭证，本次{operation}最多进行一轮自动凭证修复', kind='extract', category='invalid')
        credentials, proxy_id = await self.routed(job, account, 'refresh', settings, lambda active_route: self.service.extract(account['login']['username'], password, active_route, settings))
        if not credentials.get('session') or not str(credentials.get('api_user', '')).isdigit():
            raise TaskError('missing_session', '自动重新提取未获得完整凭证，未提交签到')
        now = time.time()
        self.store.execute("UPDATE accounts SET result_enc=?,validity='valid',message='自动重新提取成功',last_extracted=?,last_validated=?,quota=?,proxy_id=?,updated=? WHERE id=?",
                           (self.store.vault.seal(credentials, f'result:{account["id"]}'), now, now, credentials.get('quota'), proxy_id, now, account['id']))
        account.update(result=credentials, validity='valid', last_extracted=now)
        self.log_job(job, f'自动重新提取成功，继续执行原{operation}任务', kind='extract', category='invalid')

    def complete_account(self, job, query, values, status, message, balance=None, usage=(None, None), reward_amount=None):
        with self.store.transaction() as connection:
            connection.execute(query, values)
            if balance is not None:
                self.store.record_checkin(job['account_id'], job['id'], status, *balance, connection=connection,
                                          used_before=usage[0], used_after=usage[1], reward_amount=reward_amount)
            connection.execute('UPDATE jobs SET status=?,message=?,payload_enc=NULL,finished=? WHERE id=?',
                               (status, message, time.time(), job['id']))
        self.log_job(job, message)

    def tick_schedules(self, now=None):
        if self.store.meta('maintenance') == '1':
            return
        now = now or time.time()
        with self.store.transaction() as connection:
            due = connection.execute('SELECT * FROM schedules WHERE enabled=1 AND next_run<=? ORDER BY next_run', (now,)).fetchall()
            for schedule in due:
                interval = schedule['interval_minutes'] * 60
                next_run = schedule['next_run'] + (int((now - schedule['next_run']) // interval) + 1) * interval
                accounts = connection.execute("SELECT account_id,validity,login_enc FROM schedule_accounts JOIN accounts ON accounts.id=account_id WHERE schedule_id=?", (schedule['id'],)).fetchall()
                for account in accounts:
                    if account['validity'] in ('invalid', 'blocked') and not self.store.vault.open(account['login_enc'], f'login:{account["account_id"]}').get('password'):
                        continue
                    self.store.enqueue('checkin', account['account_id'], source=f'schedule:{schedule["id"]}',
                        dedupe_key=f'schedule:{schedule["id"]}:{schedule["next_run"]}:{account["account_id"]}', connection=connection)
                connection.execute('UPDATE schedules SET next_run=?,last_run=? WHERE id=?', (next_run, now, schedule['id']))

    async def clock(self):
        while not self.stopping:
            try:
                if self.storage_error or not self.store.enough_space():
                    await asyncio.sleep(5)
                    continue
                self.tick_schedules()
                self.store.execute('DELETE FROM console_results WHERE expires<?', (time.time(),))
                settings = self.store.settings()
                previous = float(self.store.meta('last_log_cleanup', '0'))
                if not previous:
                    self.store.set_meta('last_log_cleanup', time.time())
                elif settings.log_cleanup_enabled and time.time() - previous >= settings.log_cleanup_hours * 3600:
                    self.store.cleanup()
            except asyncio.CancelledError:
                raise
            except sqlite3.Error:
                self.storage_error = True
            except Exception as error:
                self.store.log('system', f'定时调度异常（{type(error).__name__}），稍后重试', level='error')
            await asyncio.sleep(5)
