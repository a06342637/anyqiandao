import asyncio
import hashlib
import json
import secrets
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.config import VERSION
from app.db import SCHEMA_VERSION
from app.schemas import AccountEdit, ActionInput, ImportInput, QueueSettings, Reorder, RuntimeSettings, Selection
from app.statistics import checkin_statistics


def account_router(store, auth, engine):
    router = APIRouter(prefix='/api/v1', dependencies=[Depends(auth.require)])

    @router.get('/meta')
    def meta():
        return {'name': store.settings().site_name, 'version': VERSION, 'schema_version': SCHEMA_VERSION,
                'script_revision': '9f1394d0f5cc', 'concurrency': store.settings().max_concurrency}

    @router.get('/dashboard')
    def dashboard():
        accounts = store.one("SELECT COUNT(*) AS total,SUM(validity='valid') AS valid,SUM(validity='invalid') AS invalid,SUM(result_enc IS NOT NULL) AS extracted FROM accounts")
        jobs = store.one("SELECT SUM(status='pending') AS pending,SUM(status='running') AS running FROM jobs")
        next_run = store.one('SELECT MIN(next_run) AS next_run FROM schedules WHERE enabled=1')
        settings = store.settings()
        return {'accounts': {key: value or 0 for key, value in accounts.items()},
                'jobs': {key: value or 0 for key, value in jobs.items()}, 'next_run': next_run['next_run'],
                'paused': store.meta('queue_paused') == '1', 'pause_reason': store.meta('pause_reason'), 'resource_wait': engine.resource_wait, 'global_concurrency': 1,
                'timezone': settings.timezone, 'proxy_mode': settings.proxy_mode,
                'enabled_proxies': store.one('SELECT COUNT(*) AS count FROM proxies WHERE enabled=1')['count'],
                'storage_error': engine.storage_error, 'max_concurrency': settings.max_concurrency, 'checkin_concurrency': settings.checkin_concurrency}

    @router.get('/events')
    async def events(request: Request, token=Depends(auth.require)):
        async def generate():
            digest = hashlib.sha256(token.encode()).hexdigest()
            while not await request.is_disconnected():
                session = store.one('SELECT expires FROM sessions WHERE token_hash=?', (digest,))
                if not session or session['expires'] <= time.time():
                    yield 'event: expired\ndata: {}\n\n'
                    return
                yield 'event: progress\ndata: ' + json.dumps(dashboard(), ensure_ascii=True) + '\n\n'
                await asyncio.sleep(3)
        return StreamingResponse(generate(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})

    @router.get('/accounts')
    def accounts(page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=100),
                 validity: str | None = None, only_extracted: bool = False):
        try:
            selection = Selection(all_matching=True, validity=validity or None, only_extracted=only_extracted)
        except ValueError:
            raise HTTPException(400, '账号状态筛选无效') from None
        where, values = store.selection_where(selection)
        count = store.selection_count(selection)
        rows = store.all('SELECT * FROM accounts WHERE ' + where + ' ORDER BY position,created,rowid LIMIT ? OFFSET ?',
                         [*values, limit, (page - 1) * limit])
        return {'items': [store.public_account(row) for row in rows], 'total': count, 'page': page, 'limit': limit}

    @router.post('/accounts/import')
    def import_accounts(payload: ImportInput):
        if not store.enough_space():
            raise HTTPException(507, '数据盘可用空间不足 64MB，请先释放空间，未提交本批数据')
        queued, skipped, account_ids, job_ids, duplicates = 0, 0, [], [], []
        with store.transaction() as connection:
            for item in payload.accounts:
                dedupe = f'import:{payload.import_id}:{item.row_id}'
                existing_job = connection.execute('SELECT id,account_id FROM jobs WHERE dedupe_key=?', (dedupe,)).fetchone()
                if existing_job:
                    skipped += 1
                    account_ids.append(existing_job['account_id'])
                    job_ids.append(existing_job['id'])
                    continue
                fingerprint = store.vault.fingerprint(item.username)
                existing = connection.execute('SELECT id FROM accounts WHERE login_hash=?', (fingerprint,)).fetchone()
                if existing:
                    skipped += 1
                    duplicates.append(item.username)
                    account_ids.append(existing['id'])
                    continue
                account_id = uuid.uuid4().hex
                login_enc = store.vault.seal({'username': item.username, 'password': item.password}, f'login:{account_id}')
                now = time.time()
                connection.execute('INSERT INTO accounts(id,login_hash,login_enc,created,updated,position) VALUES (?,?,?,?,?,(SELECT COALESCE(MAX(position),0)+1 FROM accounts))',
                                   (account_id, fingerprint, login_enc, now, now))
                job_id, inserted = store.enqueue('extract', account_id, source='import', payload={'password': item.password},
                                            dedupe_key=dedupe, connection=connection)
                queued += int(inserted)
                skipped += int(not inserted)
                account_ids.append(account_id)
                job_ids.append(job_id)
        return {'queued': queued, 'skipped': skipped, 'account_ids': account_ids, 'job_ids': job_ids, 'duplicates': duplicates, 'duplicate_count': len(duplicates)}

    @router.post('/accounts/actions')
    def actions(payload: ActionInput):
        queued, skipped, filtered, missing, missing_count, job_ids = 0, 0, 0, [], 0, []
        repair_invalid = payload.repair_invalid if payload.repair_invalid is not None else store.settings().auto_reextract
        if payload.repair_invalid is not None and payload.action != 'validate':
            raise HTTPException(400, '自动重提选项只能用于凭证检测')
        with store.transaction() as connection:
            count = store.selection_count(payload, connection=connection)
            if not count:
                raise HTTPException(400, '请先选择账号')
            if payload.password is not None and (count != 1 or payload.action not in ('extract', 'extract_invalid')):
                raise HTTPException(400, '新密码只能用于一个指定账号的重新提取；多账号请使用各自保存的密码，或在账号列表逐个编辑')
            for row in store.iter_selected(connection, payload):
                account_id = row['id']
                if payload.action == 'extract_invalid' and row['validity'] != 'invalid':
                    filtered += 1
                    continue
                if payload.action in ('extract', 'extract_invalid'):
                    login = store.vault.open(row['login_enc'], f'login:{account_id}')
                    password = payload.password or login.get('password')
                    if not password:
                        missing_count += 1
                        if len(missing) < 100:
                            missing.append(account_id)
                        continue
                    if payload.password and payload.password != login.get('password'):
                        connection.execute('UPDATE accounts SET login_enc=?,updated=? WHERE id=?',
                                           (store.vault.seal(login | {'password': payload.password}, f'login:{account_id}'), time.time(), account_id))
                    job_id, inserted = store.enqueue('extract', account_id,
                        payload={'password': password, 'only_if_invalid': payload.action == 'extract_invalid', 'route_operation': 'refresh' if row['result_enc'] else 'extract'}, connection=connection)
                else:
                    if payload.action == 'validate' and not row['result_enc'] and not repair_invalid:
                        skipped += 1
                        continue
                    options = {'repair_invalid': repair_invalid} if payload.action == 'validate' else None
                    job_id, inserted = store.enqueue(payload.action, account_id, payload=options, connection=connection)
                queued += int(inserted)
                skipped += int(not inserted)
                job_ids.append(job_id)
        return {'queued': queued, 'skipped': skipped, 'filtered_count': filtered,
                'needs_password': missing, 'needs_password_count': missing_count, 'job_ids': job_ids}

    @router.put('/accounts/{account_id}')
    def edit_account(account_id: str, payload: AccountEdit):
        with store.transaction() as connection:
            row = connection.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
            if not row:
                raise HTTPException(404, '账号不存在')
            login = store.vault.open(row['login_enc'], f'login:{account_id}')
            was_password_account = bool(login.get('password'))
            changed = {}
            if payload.username is not None and not payload.username.strip():
                raise HTTPException(400, '账号不能为空')
            if payload.username is not None and payload.username.strip() and payload.username.strip() != login['username']:
                username = payload.username.strip()
                fingerprint = store.vault.fingerprint(username)
                if connection.execute('SELECT 1 FROM accounts WHERE login_hash=? AND id!=?', (fingerprint, account_id)).fetchone():
                    raise HTTPException(409, '已存在相同账号')
                login['username'] = username
                connection.execute('UPDATE accounts SET login_hash=? WHERE id=?', (fingerprint, account_id))
                changed['username'] = True
            if payload.password is not None and payload.password != login.get('password'):
                login['password'] = payload.password
                changed['password'] = True
            if changed:
                if connection.execute("SELECT 1 FROM jobs WHERE account_id=? AND status IN ('pending','running') LIMIT 1", (account_id,)).fetchone():
                    raise HTTPException(409, '该账号仍有等待或执行中的任务，请先完成或取消，再修改账号密码')
                connection.execute('UPDATE accounts SET login_enc=?,updated=? WHERE id=?', (store.vault.seal(login, f'login:{account_id}'), time.time(), account_id))
                if changed.get('username') and (was_password_account or payload.password):
                    connection.execute("UPDATE accounts SET result_enc=NULL,checkin_state_enc=NULL,validity='unknown',message='账号已修改，请重新提取凭证',quota=NULL,last_validated=NULL,last_extracted=NULL,last_checkin=NULL,checkin_status=NULL WHERE id=?", (account_id,))
                    connection.execute('DELETE FROM console_results WHERE account_id=?', (account_id,))
            if payload.note is not None and payload.note != row['note']:
                connection.execute('UPDATE accounts SET note=?,updated=? WHERE id=?', (payload.note.strip()[:500], time.time(), account_id))
                changed['note'] = True
        if changed.get('username') or changed.get('password'):
            message = '账号已修改，旧凭证已作废，历史统计保留；请重新提取' if changed.get('username') and (was_password_account or payload.password) else '手动账号显示名称已更新，原凭证与历史保留' if changed.get('username') else '密码已更新并加密保存'
            store.log('system', message, account_id=account_id)
        return {'ok': True, 'changed': sorted(changed)}

    @router.post('/accounts/reorder')
    def reorder_accounts(payload: Reorder):
        # The given ids take the positions they occupied as a block, in the new order; everything else keeps its place.
        with store.transaction() as connection:
            wanted = set(payload.ids)
            ordered = [row['id'] for row in connection.execute('SELECT id FROM accounts ORDER BY position,created,rowid')]
            if len(wanted) != len(payload.ids) or not wanted.issubset(ordered):
                raise HTTPException(400, '排序包含重复或不存在的账号，请刷新后重试')
            slots = [index for index, account_id in enumerate(ordered) if account_id in wanted]
            for slot, account_id in zip(slots, payload.ids):
                ordered[slot] = account_id
            connection.executemany('UPDATE accounts SET position=? WHERE id=?', enumerate(ordered, start=1))
        return {'ok': True}

    @router.get('/accounts/{account_id}/credentials')
    def credentials(account_id: str):
        account = store.account(account_id)
        if not account or not account['result']:
            raise HTTPException(404, '该账号尚无可复制的凭证')
        if account['validity'] == 'invalid':
            raise HTTPException(409, '该凭证已确认失效，请重新提取')
        return {'session': account['result']['session'], 'api_user': str(account['result']['api_user'])}

    @router.get('/accounts/{account_id}/login')
    def login_details(account_id: str):
        account = store.account(account_id)
        if not account:
            raise HTTPException(404, '账号不存在')
        return {'username': account['login']['username'], 'password': account['login'].get('password')}

    @router.post('/accounts/export-logins')
    def export_logins(payload: Selection):
        # One "账号----密码" per line, the same shape the bulk import accepts, so the copy can be pasted straight back in.
        count = store.selection_count(payload)
        if not count:
            raise HTTPException(400, '请先选择账号')
        if count > 5000:
            raise HTTPException(413, '一次最多复制 5000 个账号的账密，请缩小选择范围')

        def generate():
            with store.connection() as connection:
                for row in store.iter_selected(connection, payload, columns='id,login_enc'):
                    login = store.vault.open(row['login_enc'], f'login:{row["id"]}')
                    yield (login['username'] + '----' + login['password'] if login.get('password') else login['username']) + '\n'
        return StreamingResponse(generate(), media_type='text/plain; charset=utf-8', headers={'Cache-Control': 'no-store', 'X-Export-Count': str(count)})

    def export_stream(selection):
        async def generate():
            with store.connection() as connection:
                connection.execute('BEGIN')
                iterator = store.iter_selected(connection, selection, columns='id,result_enc', exportable=True)
                separator = ''
                yield '['
                for row in iterator:
                    result = store.vault.open(row['result_enc'], f'result:{row["id"]}')
                    item = {'cookies': {'session': result['session']}, 'api_user': str(result['api_user'])}
                    yield separator + json.dumps(item, ensure_ascii=False, separators=(',', ':'))
                    separator = ','
                yield ']'
        return StreamingResponse(generate(), media_type='application/json',
            headers={'Content-Disposition': 'attachment; filename="ANYROUTER_ACCOUNTS.json"', 'Cache-Control': 'no-store'})

    @router.post('/accounts/export')
    def export_accounts(payload: Selection):
        where, values = store.selection_where(payload, exportable=True)
        summary = store.one('SELECT COUNT(*) AS count,COALESCE(SUM(LENGTH(result_enc)+100),0) AS size FROM accounts WHERE ' + where, values)
        if not summary['count']:
            raise HTTPException(400, '选择中没有可导出的成功结果；未提取和已失效的账号不会导出')
        if summary['size'] > 1048576:
            raise HTTPException(413, '选择集较大，请使用下载配置；不会截断复制内容')
        response = export_stream(payload)
        response.headers['X-Export-Count'] = str(summary['count'])
        return response

    @router.post('/accounts/export-link')
    def export_link(payload: Selection, token=Depends(auth.require)):
        count = store.selection_count(payload, exportable=True)
        if not count:
            raise HTTPException(400, '选择中没有可导出的成功结果')
        ticket = secrets.token_urlsafe(32)
        digest = hashlib.sha256(ticket.encode()).hexdigest()
        with store.transaction() as connection:
            connection.execute('DELETE FROM exports WHERE expires<?', (time.time(),))
            connection.execute('INSERT INTO exports VALUES (?,?,?,?)',
                (digest, hashlib.sha256(token.encode()).hexdigest(), store.vault.seal(payload.model_dump(), f'export:{digest}'), time.time() + 300))
        return {'url': '/api/v1/accounts/download/' + ticket, 'count': count}

    @router.get('/accounts/download/{ticket}')
    def download(ticket: str, token=Depends(auth.require)):
        digest = hashlib.sha256(ticket.encode()).hexdigest()
        with store.transaction() as connection:
            row = connection.execute('SELECT * FROM exports WHERE token_hash=? AND session_hash=? AND expires>?',
                                     (digest, hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
            if not row:
                raise HTTPException(410, '下载链接已过期或已使用，请重新点击下载')
            selection = Selection.model_validate(store.vault.open(row['selection_enc'], f'export:{digest}'))
            connection.execute('DELETE FROM exports WHERE token_hash=?', (digest,))
        return export_stream(selection)

    @router.post('/accounts/delete')
    def delete_accounts(payload: Selection):
        where, values = store.selection_where(payload)
        with store.transaction() as connection:
            active = connection.execute("SELECT id FROM jobs WHERE status IN ('pending','running') AND account_id IN (SELECT id FROM accounts WHERE " + where + ') LIMIT 1', values).fetchone()
            if active:
                raise HTTPException(409, '选择中有等待或运行任务，请先取消任务再删除')
            removed = connection.execute('DELETE FROM accounts WHERE ' + where, values).rowcount
            connection.execute('UPDATE schedules SET enabled=0 WHERE id NOT IN (SELECT schedule_id FROM schedule_accounts)')
        return {'removed': removed}

    @router.delete('/accounts/{account_id}')
    def delete_account(account_id: str):
        return delete_accounts(Selection(ids=[account_id]))

    @router.get('/jobs')
    def jobs(page: int = Query(1, ge=1), limit: int = Query(10, ge=1, le=100), lane: str = '', state: str = ''):
        if lane not in ('', 'queue', 'checkin') or state not in ('', 'active', 'finished'):
            raise HTTPException(400, '队列筛选无效')
        predicates = ['hidden=0']
        if lane:
            predicates.append("kind='checkin'" if lane == 'checkin' else "kind!='checkin'")
        if state:
            predicates.append("status IN ('pending','running')" if state == 'active' else "status NOT IN ('pending','running')")
        where = ' AND '.join(predicates)
        rows = store.all("SELECT id,account_id,proxy_id,kind,status,source,message,created,started,finished FROM jobs WHERE " + where + " ORDER BY CASE WHEN status='running' THEN 0 WHEN status='pending' THEN 1 ELSE 2 END,CASE WHEN status='pending' THEN created ELSE -created END,rowid LIMIT ? OFFSET ?",
                         (limit, (page - 1) * limit))
        for row in rows:
            account = store.one('SELECT login_enc FROM accounts WHERE id=?', (row['account_id'],)) if row['account_id'] else None
            row['username'] = store.vault.open(account['login_enc'], f'login:{row["account_id"]}')['username'] if account else None
        counts = store.one("SELECT SUM(status='pending' AND kind!='checkin') AS queue_pending,SUM(status='running' AND kind!='checkin') AS queue_running,SUM(status='pending' AND kind='checkin') AS checkin_pending,SUM(status='running' AND kind='checkin') AS checkin_running FROM jobs")
        settings = store.settings()
        return {'items': rows, 'total': store.one('SELECT COUNT(*) AS count FROM jobs WHERE ' + where)['count'], 'page': page, 'limit': limit,
                'paused': store.meta('queue_paused') == '1', 'pause_reason': store.meta('pause_reason'), 'resource_wait': engine.resource_wait, 'global_concurrency': 1, 'max_concurrency': settings.max_concurrency,
                'checkin_concurrency': settings.checkin_concurrency, 'counts': {key: value or 0 for key, value in counts.items()}}

    @router.put('/queue/settings')
    def queue_settings(payload: QueueSettings):
        with store.transaction() as connection:
            current = connection.execute("SELECT value FROM meta WHERE key='settings'").fetchone()
            settings = RuntimeSettings.model_validate_json(current['value']).model_copy(update=payload.model_dump())
            connection.execute("UPDATE meta SET value=? WHERE key='settings'", (settings.model_dump_json(),))
        store.log('system', f'并发已保存：普通队列 {payload.max_concurrency}，签到队列 {payload.checkin_concurrency}；新任务立即按新上限领取，已有任务不强行中断')
        return payload.model_dump()

    @router.post('/queue/{action}')
    def queue_action(action: str):
        if action == 'pause':
            store.set_meta('queue_paused', '1')
            store.set_meta('pause_reason', '已手动暂停，当前任务结束后不再启动新任务')
        elif action == 'resume':
            if not store.enough_space() or engine.storage_error:
                raise HTTPException(507, '请先解决存储异常并重启本项目，尚未恢复队列')
            store.set_meta('queue_paused', '0')
            store.set_meta('pause_reason', '')
            store.execute('UPDATE proxies SET failed_until=0 WHERE enabled=1')
        elif action == 'cancel-pending':
            store.execute("UPDATE jobs SET status='cancelled',message='批量取消等待任务',payload_enc=NULL,finished=? WHERE status='pending'", (time.time(),))
        else:
            raise HTTPException(404, '操作不存在')
        return {'ok': True}

    @router.post('/jobs/{job_id}/cancel')
    async def cancel_job(job_id: str):
        job = await engine.cancel_and_wait(job_id)
        if job is None:
            raise HTTPException(404, '任务不存在')
        if job['status'] in ('pending', 'running'):
            raise HTTPException(409, '任务正在安全退出，请稍后刷新；尚未删除任务或中断清理')
        return {'ok': True, 'status': job['status'], 'message': job['message']}

    @router.delete('/jobs/{job_id}')
    async def delete_job(job_id: str):
        result = await cancel_job(job_id)
        with store.transaction() as connection:
            job = connection.execute('SELECT kind,account_id FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not job:
                raise HTTPException(404, '任务不存在')
            connection.execute('UPDATE jobs SET hidden=1 WHERE id=?', (job_id,))
        store.log(job['kind'], '任务已从执行队列删除；执行日志与签到统计保留', job_id=job_id, account_id=job['account_id'])
        return {**result, 'removed': True}

    @router.get('/jobs/status')
    def job_status(ids: str = Query(..., min_length=1, max_length=20000)):
        # Polled by the page after every action so the result can be shown where the button was pressed.
        wanted = [value for value in dict.fromkeys(ids.split(',')) if value][:500]
        rows = store.all('SELECT id,account_id,proxy_id,kind,status,message,finished FROM jobs WHERE id IN (' + ','.join('?' for _ in wanted) + ')', wanted) if wanted else []
        for row in rows:
            account = store.one('SELECT login_enc,validity,quota,checkin_status FROM accounts WHERE id=?', (row['account_id'],)) if row['account_id'] else None
            row['username'] = store.vault.open(account['login_enc'], f'login:{row["account_id"]}')['username'] if account else None
            row['validity'] = account['validity'] if account else None
            row['quota'] = account['quota'] if account else None
            row['balance'] = store.checkin_balance(job_id=row['id']) if row['kind'] == 'checkin' else None
            proxy = store.one('SELECT name FROM proxies WHERE id=?', (row['proxy_id'],)) if row['proxy_id'] and not row['account_id'] else None
            row['proxy_name'] = proxy['name'] if proxy else None
        return {'items': rows}

    @router.get('/stats')
    def stats(range: str = Query('day')):
        if range not in ('day', 'week', 'month'):
            raise HTTPException(400, '统计范围只支持 day / week / month')
        return checkin_statistics(store, range)

    @router.get('/logs')
    def logs(page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100), kind: str | None = None, category: str | None = None,
             job_id: str | None = None, account_id: str | None = None):
        predicates, values = [], []
        if category:
            predicates.append('category=?')
            values.append(category)
        elif kind == 'checkin':
            predicates.append("kind IN ('checkin','script')")
        elif kind:
            predicates.append('kind=?')
            values.append(kind)
        if job_id:
            predicates.append('job_id=?')
            values.append(job_id)
        if account_id:
            predicates.append('account_id=?')
            values.append(account_id)
        where = ' WHERE ' + ' AND '.join(predicates) if predicates else ''
        count = store.one('SELECT COUNT(*) AS count FROM logs' + where, values)['count']
        rows = store.all('SELECT * FROM logs' + where + ' ORDER BY id DESC LIMIT ? OFFSET ?', [*values, limit, (page - 1) * limit])
        names = {}
        for row in rows:
            if row['account_id'] and row['account_id'] not in names:
                account = store.one('SELECT login_enc FROM accounts WHERE id=?', (row['account_id'],))
                names[row['account_id']] = store.vault.open(account['login_enc'], f'login:{row["account_id"]}')['username'] if account else None
            row['username'] = names.get(row['account_id'])
        scope = [(field, value) for field, value in (('job_id', job_id), ('account_id', account_id)) if value]
        scope_where = ' WHERE ' + ' AND '.join(f'{field}=?' for field, _ in scope) if scope else ''
        categories = store.all('SELECT category,COUNT(*) AS count FROM logs' + scope_where + ' GROUP BY category',
                               [value for _, value in scope])
        return {'items': rows, 'total': count, 'page': page, 'limit': limit, 'categories': {row['category']: row['count'] for row in categories}}

    return router
