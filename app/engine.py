import asyncio
import hashlib
import sqlite3
import time

import httpx

from app.config import TARGET_ORIGIN
from app.providers import verified_reward
from app.random_schedule import dispatch_window, plan_window
from app.checkin_state import balance_credit, beijing_day, submitted_today, fixed_reward
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
        self.resource_lock = asyncio.Lock()
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
            connection.execute("UPDATE jobs SET status='cancelled',message='随机签到窗口已结束，未执行，不补发',payload_enc=NULL,finished=? WHERE status='pending' AND not_after IS NOT NULL AND not_after<=?", (time.time(), time.time()))
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
                async with self.resource_lock:
                    if not memory_status()['ready']:
                        continue
                    job = self.claim()
                    if job:
                        task = asyncio.create_task(self.run_job(job))
                        self.running[job['id']] = (job, task)
                        last_started = time.monotonic()
                        await asyncio.wait({task})
                if not job:
                    await asyncio.sleep(0.5)
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
            uncertain = job['kind'] == 'checkin' and job.get('_checkin_submitted')
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
            job['_retry_unconfirmed'] = payload.get('retry_unconfirmed', False)
            job['_route_operation'] = payload.get('route_operation', 'extract')
            await self.perform(job, account, password, None, settings, None)

        except TaskError as error:
            if error.code == 'resource_busy' and not job.get('_checkin_submitted'):
                self.resource_wait = '内存暂时不足，任务保留在队列，资源释放后自动继续'
                self.store.execute("UPDATE jobs SET status='pending',started=NULL,message=? WHERE id=? AND status='running'", (self.resource_wait, job['id']))
                self.log_job(job, self.resource_wait, level='warning')
                return
            if job['account_id']:
                now = time.time()
                if job['kind'] == 'insights':
                    if error.code == 'invalid':
                        login = self.store.account(job['account_id'])['login']
                        message = error.message if login.get('password') else 'Session 凭证已失效，请手动更新 session 与 api_user；不会自动登录'
                        self.store.execute("UPDATE accounts SET validity='invalid',message=?,last_validated=?,updated=? WHERE id=?", (message, now, now, job['account_id']))
                elif job['kind'] in ('validate', 'balance', 'checkin'):
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
            if operation == 'checkin' and settings.checkin_fallback_proxy_ids:
                return await self.direct_checkin_with_fallback(job, settings, invoke)
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

    async def direct_checkin_with_fallback(self, job, settings, invoke):
        nodes = {row['id']: row for row in self.store.all('SELECT * FROM proxies WHERE enabled=1')}
        candidates = [nodes[identifier] for identifier in settings.checkin_fallback_proxy_ids if identifier in nodes]
        if not candidates:
            self.log_job(job, '没有可用的已勾选备用代理，继续按直连最多三次核验')
            return await invoke(None), None
        previous = self.store.meta('checkin_fallback_last_id')
        index = next((i + 1 for i, node in enumerate(candidates) if node['id'] == previous), 0) % len(candidates)
        proxy = candidates[index]
        direct = None
        job['_checkin_attempt_limit'], job['_checkin_attempt_offset'] = 2, 0
        for connection_attempt in range(2):
            try:
                direct = await invoke(None)
                break
            except (TaskError, httpx.TransportError, OSError) as error:
                if job.get('_checkin_submitted') or isinstance(error, TaskError) and not error.retry_proxy:
                    raise
                self.log_job(job, f'直连第 {connection_attempt + 1}/2 次网络连接失败', level='warning')
                job['_checkin_attempt_limit'], job['_checkin_attempt_offset'] = 1, 1
        if direct is not None and not direct.get('can_fallback'):
            return direct, None
        for line in (direct or {}).get('logs', [])[:40]:
            self.log_job(job, line, kind='script')
        job['_checkin_continuation'] = direct
        job['_checkin_attempt_limit'], job['_checkin_attempt_offset'] = 1, 2
        job['_route_label'] = f'签到 · 备用代理：{proxy["name"]}（{proxy["id"][:8]}）'
        self.log_job(job, '直连两次未完成签到，选择已勾选备用代理；先复核余额，最多再签到一次')
        self.store.set_meta('checkin_fallback_last_id', proxy['id'])
        try:
            config = await self.proxy_config(proxy, settings)
            async with ProxyBridge(config, settings.connect_timeout, proxy['trusted_key']) as route:
                result = await invoke(route)
        except HostKeyRequired as error:
            self.require_host_key(proxy, error)
            raise
        except TaskError as error:
            if direct is None:
                raise
            self.log_job(job, error.message, level='warning')
            result = {**direct, 'code': 'uncertain', 'reward_amount': None,
                      'message': '直连未到账，备用代理未完成核验；保留待确认', 'logs': []}
        except (httpx.TransportError, OSError):
            if direct is None:
                raise
            result = {**direct, 'code': 'uncertain', 'reward_amount': None,
                      'message': '备用代理连接失败；保留待确认', 'logs': []}
        return result, proxy['id']

    def after_extract(self, job, account):
        settings = self.store.settings()
        if not settings.auto_checkin and job['source'] != 'auto:reextract':
            return
        joined = False
        random_plan = False
        inserted = False
        with self.store.transaction() as connection:
            if settings.auto_checkin:
                schedule_id = self.store.auto_schedule(connection, settings.auto_checkin_interval_minutes)
                joined = connection.execute('INSERT OR IGNORE INTO schedule_accounts VALUES (?,?)', (schedule_id, account['id'])).rowcount
                schedule = connection.execute('SELECT * FROM schedules WHERE id=?', (schedule_id,)).fetchone()
                if schedule['mode'] == 'random_window':
                    plan_window(connection, schedule, time.time())
                    random_plan = True
            if not random_plan:
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
        if account.get('provider') == 'agentrouter' and job['kind'] in ('extract', 'checkin'):
            await self.perform_agentrouter(job, account, password, settings)
            return
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
                notice = 'AgentRouter 查询可能触发网站自动发放；未修改令牌' if account.get('provider') == 'agentrouter' else '只读查询，未登录、未签到、未修改令牌'
                connection.execute("UPDATE jobs SET status='success',message=?,payload_enc=NULL,finished=? WHERE id=?", (f'{label}已读取；{notice}', now, job['id']))
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
        elif job['kind'] == 'balance':
            if not account['result']:
                raise TaskError('invalid', '缺少登录凭证，请先提取或导入凭证后检测余额')
            result, proxy_id = await self.routed(job, account, 'balance', settings, lambda active_route: self.service.validate(account['result'], active_route, settings))
            if result.get('quota') is None:
                raise TaskError('upstream_error', '网站未返回有效余额，已保留上次余额，请稍后重试')
            now = time.time()
            self.complete_account(job, "UPDATE accounts SET validity='valid',message='余额已更新',last_validated=?,quota=?,proxy_id=?,updated=? WHERE id=?",
                (now, result['quota'], proxy_id, now, account['id']), 'success', '余额检测完成，已更新余额')
            self.store.observe_account(account['id'], account['result']['api_user'], result['quota'], quota_of(result.get('profile') or {}, 'used_quota'), at=now)
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
                (message, now, result.get('quota'), proxy_id, now, account['id']), 'success', f'检测完成：{message}；' + ('AgentRouter 查询可能触发网站自动发放' if account.get('provider') == 'agentrouter' else '未触发签到'))
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
                if job.get('_submission_at'):
                    self.store.clear_rejected_submission(account['id'], job['_submission_at'])
                job['_checkin_started'] = False
                job['_checkin_submitted'] = False
                await self.refresh_for_checkin(job, account, password, route, settings, proxy_id)
                result, proxy_id = await self.routed(job, account, 'checkin', settings, lambda active_route: self.checkin_or_observed(job, account, active_route, settings))
            result = self.resolve_checkin_receipt(account, result, settings)
            if result['code'] in ('signed', 'no_credit') and job.get('_submission_at'):
                self.store.clear_rejected_submission(account['id'], job['_submission_at'])
            now = time.time()
            before, after = result.get('quota_before'), result.get('quota_after')
            message = result['message']
            if before is not None and after is not None:
                message += f'；签到前 ${before:.4f} → 签到后 ${after:.4f}，变化 {after - before:+.4f}'
            elif result['code'] in ('signed', 'already_signed'):
                message += '；余额未完整读取，新增金额暂不计入统计'
            self.complete_account(job, "UPDATE accounts SET validity='valid',message=?,last_validated=?,last_checkin=?,checkin_status=?,quota=COALESCE(?,quota),proxy_id=?,updated=? WHERE id=?",
                (result['message'], now, now, result['code'], result.get('quota'), proxy_id, now, account['id']), result['code'], message,
                balance=(before, after), usage=(result.get('used_before'), result.get('used_after')), reward_amount=result.get('reward_amount'))
            for line in result.get('logs', [])[:40]:
                self.log_job(job, line, kind='script')

    async def checkin_or_observed(self, job, account, route, settings):
        current = self.store.account(account['id'])
        state = self.store.daily_state(current)
        continuation = job.get('_checkin_continuation')
        if not continuation and submitted_today(state, account['result']['api_user'], time.time()):
            resolved = current['checkin_status'] in ('signed', 'already_signed', 'manual_confirmed', 'no_credit')
            if not resolved and not job.get('_retry_unconfirmed'):
                observed = await self.service.validate(account['result'], route, settings)
                now = time.time()
                quota, used = observed.get('quota'), quota_of(observed.get('profile') or {}, 'used_quota')
                gain = balance_credit(state.model_dump(), {'quota': quota, 'used_quota': used})
                if fixed_reward(gain):
                    job['_submission_at'] = state.submitted_at
                    return {'code': 'signed', 'receipt_code': 'accepted', 'message': '只读复核已确认上次请求延迟到账 $25；本次未重复提交',
                            'quota': quota, 'quota_before': state.quota, 'quota_after': quota,
                            'used_before': state.used_quota, 'used_after': used, 'verified_gain': gain, 'reward_amount': 25,
                            'observed_before': state.observed_at, 'observed_after': now, 'claimed_at': state.submitted_at, 'logs': []}
                if state.quota is None:
                    self.store.observe_account(account['id'], account['result']['api_user'], quota, used, at=now)
                raise TaskError('uncertain', '上次请求结果仍不明；请手动重新核验。本次未提交，也未计为成功')
            self.store.clear_rejected_submission(account['id'], state.submitted_at)
            self.log_job(job, '已结束的签到记录不限制本轮领取；重新读取余额并请求网站核验')
        job['_checkin_started'] = True
        def precheck(profile, observed_at):
            if continuation:
                gain = balance_credit({'quota': continuation.get('quota_before'), 'used_quota': continuation.get('used_before')},
                                      {'quota': quota_of(profile), 'used_quota': quota_of(profile, 'used_quota')})
                same_day = beijing_day(continuation['observed_before']) == beijing_day(observed_at)
                if fixed_reward(gain) and same_day:
                    return {**continuation, 'code': 'signed', 'message': '备用代理复核到直连后的延迟到账，已确认新增 $25；未再次提交',
                            'quota': quota_of(profile), 'quota_after': quota_of(profile), 'used_after': quota_of(profile, 'used_quota'),
                            'observed_after': observed_at, 'verified_gain': gain, 'reward_amount': 25, 'logs': []}
                if gain != 0 or not same_day:
                    return {**continuation, 'code': 'uncertain', 'message': '切换代理后余额变化或日期无法确认，未再次提交签到',
                            'quota': quota_of(profile), 'quota_after': quota_of(profile), 'used_after': quota_of(profile, 'used_quota'),
                            'observed_after': observed_at, 'logs': []}
            return self.checkin_precheck(account, quota_of(profile), quota_of(profile, 'used_quota'), observed_at)
        options = ({'attempt_limit': job['_checkin_attempt_limit'], 'attempt_offset': job['_checkin_attempt_offset']}
                   if '_checkin_attempt_limit' in job else {})
        return await self.service.checkin(account['result'], route, settings,
                    before_submit=precheck,
                    on_submit=lambda: None if job.get('_checkin_submitted') else self.mark_submitted(job, account), **options)

    def mark_submitted(self, job, account):
        at = self.store.mark_submission(account['id'], account['result']['api_user'])
        job.update(_checkin_submitted=True, _submission_at=at)

    def checkin_precheck(self, account, quota, used, now):
        # Beijing dates group reports; they do not describe the site's eligibility cycle.
        self.store.observe_account(account['id'], account['result']['api_user'], quota, used, at=now)
        return None

    def resolve_checkin_receipt(self, account, result, settings):
        now = time.time()
        identity = account['result']['api_user']
        if result.get('receipt_code'):
            before_at = result.get('observed_before', now)
            after_at = result.get('observed_after', now)
            gain = balance_credit({'quota': result.get('quota_before'), 'used_quota': result.get('used_before')},
                                  {'quota': result.get('quota_after'), 'used_quota': result.get('used_after')})
            same_day = len({beijing_day(at) for at in (before_at, result.get('claimed_at', before_at), after_at, now)}) == 1
            if result['code'] == 'signed' and (not verified_reward(account.get('provider', 'anyrouter'), gain) or not same_day):
                result = {**result, 'code': 'uncertain', 'reward_amount': None,
                          'message': '未通过本轮签到前后余额核验，不能确认该平台奖励到账'}
            if result['code'] == 'already_signed':
                result = {**result, 'code': 'no_credit' if gain == 0 else 'uncertain', 'reward_amount': None,
                          'message': '网站提示已领取，但本轮没有核实到账；未计为成功'}
            self.store.observe_account(account['id'], identity, result.get('quota_before'), result.get('used_before'), at=before_at)
            self.store.observe_account(account['id'], identity, result.get('quota_after'), result.get('used_after'), at=after_at,
                                       confirm=result['code'] == 'signed' and same_day,
                                       evidence='balance_credit' if account.get('provider') == 'agentrouter' else 'balance_25')
        # Never turn this attempt's missing credit into success using an earlier reward.
        return result

    async def perform_agentrouter(self, job, account, password, settings):
        if not password:
            raise TaskError('password_required', 'AgentRouter 需要保存账号密码，以全新会话重新登录签到')
        state = self.store.daily_state(account)
        if (job['kind'] == 'checkin' and not state and account['checkin_status'] == 'uncertain'
                and account['last_checkin'] and beijing_day(account['last_checkin']) == beijing_day(time.time())
                and not job.get('_retry_unconfirmed')):
            raise TaskError('uncertain', 'AgentRouter 上次首次登录结果不明，请手动重新核验')
        if state and submitted_today(state, state.api_user, time.time()):
            if account['checkin_status'] == 'uncertain' and not job.get('_retry_unconfirmed'):
                raise TaskError('uncertain', 'AgentRouter 上次登录领取结果不明，请手动重新核验')
            self.store.clear_rejected_submission(account['id'], state.submitted_at)
        def submitted():
            if job.get('_checkin_submitted'):
                return
            if account['result']:
                self.mark_submitted(job, account)
            else:
                job['_checkin_submitted'] = True
        operation = 'checkin' if job['kind'] == 'checkin' else job.get('_route_operation', 'extract')
        self.log_job(job, 'AgentRouter 使用保存的账号密码在新会话登录，查询用户信息触发自动签到')
        credentials, proxy_id = await self.routed(job, account, operation, settings,
            lambda active_route: self.service.agent_login_checkin(account['login']['username'], password, active_route, settings, on_submit=submitted))
        result = credentials.pop('checkin_result')
        now = time.time()
        self.store.execute("UPDATE accounts SET result_enc=?,validity='valid',last_extracted=?,last_validated=?,quota=COALESCE(?,quota),updated=? WHERE id=?",
            (self.store.vault.seal(credentials, f'result:{account["id"]}'), now, now, credentials.get('quota'), now, account['id']))
        account.update(result=credentials)
        result = self.resolve_checkin_receipt(account, result, settings)
        if result['code'] in ('signed', 'no_credit') and job.get('_submission_at'):
            self.store.clear_rejected_submission(account['id'], job['_submission_at'])
        status = 'success' if job['kind'] == 'extract' else result['code']
        message = ('AgentRouter 凭证提取完成；' if job['kind'] == 'extract' else '') + result['message']
        self.complete_account(job, "UPDATE accounts SET message=?,last_checkin=?,checkin_status=?,quota=COALESCE(?,quota),proxy_id=?,updated=? WHERE id=?",
            (result['message'], now, result['code'], result.get('quota'), proxy_id, now, account['id']), status, message,
            balance=(result.get('quota_before'), result.get('quota_after')), usage=(result.get('used_before'), result.get('used_after')),
            reward_amount=result.get('reward_amount'), record_code=result['code'])
        # Credential extraction can itself trigger AgentRouter's reward. Preserve
        # its actual outcome in statistics instead of recording generic success.
        if job['kind'] == 'extract':
            if settings.auto_checkin:
                with self.store.transaction() as connection:
                    schedule = self.store.auto_schedule(connection, settings.auto_checkin_interval_minutes)
                    connection.execute('INSERT OR IGNORE INTO schedule_accounts VALUES (?,?)', (schedule, account['id']))
                    schedule_row = connection.execute('SELECT * FROM schedules WHERE id=?', (schedule,)).fetchone()
                    if schedule_row['mode'] == 'random_window':
                        plan_window(connection, schedule_row, time.time())
        for line in result.get('logs', []):
            self.log_job(job, line, kind='script')

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
        agent_result = None
        if account.get('provider') == 'agentrouter':
            credentials, proxy_id = await self.routed(job, account, 'refresh', settings,
                lambda active_route: self.service.agent_login_checkin(account['login']['username'], password, active_route, settings,
                    on_submit=lambda: job.update(_checkin_submitted=True)))
            agent_result = credentials.pop('checkin_result')
        else:
            credentials, proxy_id = await self.routed(job, account, 'refresh', settings, lambda active_route: self.service.extract(account['login']['username'], password, active_route, settings))
        if not credentials.get('session') or not str(credentials.get('api_user', '')).isdigit():
            raise TaskError('missing_session', '自动重新提取未获得完整凭证，未提交签到')
        now = time.time()
        self.store.execute("UPDATE accounts SET result_enc=?,validity='valid',message='自动重新提取成功',last_extracted=?,last_validated=?,quota=?,proxy_id=?,updated=? WHERE id=?",
                           (self.store.vault.seal(credentials, f'result:{account["id"]}'), now, now, credentials.get('quota'), proxy_id, now, account['id']))
        account.update(result=credentials, validity='valid', last_extracted=now)
        if agent_result:
            result = self.resolve_checkin_receipt(account, agent_result, settings)
            self.store.record_checkin(account['id'], job['id'], result['code'], result.get('quota_before'), result.get('quota_after'),
                                      used_before=result.get('used_before'), used_after=result.get('used_after'), reward_amount=result.get('reward_amount'))
            self.store.execute('UPDATE accounts SET last_checkin=?,checkin_status=? WHERE id=?', (now, result['code'], account['id']))
            self.log_job(job, result['message'], kind='script')
        self.log_job(job, f'自动重新提取成功，继续执行原{operation}任务', kind='extract', category='invalid')

    def complete_account(self, job, query, values, status, message, balance=None, usage=(None, None), reward_amount=None, record_code=None):
        with self.store.transaction() as connection:
            connection.execute(query, values)
            if balance is not None:
                self.store.record_checkin(job['account_id'], job['id'], record_code or status, *balance, connection=connection,
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
                if schedule['mode'] == 'random_window':
                    dispatch_window(self.store, connection, schedule, now)
                    continue
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
