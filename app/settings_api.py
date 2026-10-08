import time
import uuid
import json

from fastapi import APIRouter, Depends, HTTPException, Query

from app.branding import public_branding
from app.config import VERSION
from app.proxy import parse_proxy
from app.schemas import AccountCheckinRoute, BrandingSettings, OperationRoutes, ProxyImport, ProxyInput, ProxyTrust, RuntimeSettings, ScheduleInput, Selection
from app.network_routes import validate_route


def settings_router(store, auth):
    router = APIRouter(prefix='/api/v1', dependencies=[Depends(auth.require)])

    @router.get('/settings')
    def settings():
        current = store.settings()
        previous = float(store.meta('last_log_cleanup', '0')) or None
        return {'settings': store.settings().model_dump(), 'version': VERSION, 'script_revision': '9f1394d0f5cc',
                'last_log_cleanup': previous, 'next_log_cleanup': (previous or time.time()) + current.log_cleanup_hours * 3600 if current.log_cleanup_enabled else None,
                'auto_schedule_id': store.meta('auto_schedule_id') or None}

    @router.put('/settings')
    def update_settings(payload: RuntimeSettings):
        changes = payload.model_dump(include=payload.model_fields_set, exclude={'max_concurrency', 'checkin_concurrency', 'auto_checkin_interval_minutes', 'site_name', 'site_icon_text', 'operation_routes', 'proxy_mode'})
        with store.transaction() as connection:
            current = connection.execute("SELECT value FROM meta WHERE key='settings'").fetchone()
            previous = RuntimeSettings.model_validate_json(current['value'])
            updated = previous.model_copy(update=changes)
            connection.execute("UPDATE meta SET value=? WHERE key='settings'", (updated.model_dump_json(),))
            if updated.auto_checkin and not previous.auto_checkin:
                schedule_id = store.auto_schedule(connection, previous.auto_checkin_interval_minutes)
                connection.execute('INSERT OR IGNORE INTO schedule_accounts SELECT ?,id FROM accounts WHERE result_enc IS NOT NULL', (schedule_id,))
        store.log('system', '运行参数已更新；新参数对后续任务生效')
        return {'ok': True}

    @router.put('/settings/branding')
    def update_branding(payload: BrandingSettings):
        with store.transaction() as connection:
            current = connection.execute("SELECT value FROM meta WHERE key='settings'").fetchone()
            settings = RuntimeSettings.model_validate_json(current['value'])
            updated = settings.model_copy(update=payload.model_dump())
            connection.execute("UPDATE meta SET value=? WHERE key='settings'", (updated.model_dump_json(),))
        store.log('system', '网站名称与图标已更新，浏览器标签和登录页面同步生效')
        return public_branding(store)

    @router.post('/logs/cleanup')
    def cleanup():
        return store.cleanup()

    def public_proxy(row):
        config = store.vault.open(row['config_enc'], f'proxy:{row["id"]}')
        return {key: row[key] for key in ('id', 'name', 'enabled', 'status', 'message', 'latency', 'tested_at', 'candidate_fingerprint')} | {
            'scheme': config['scheme'], 'host': config['host'], 'port': config['port'], 'username': config.get('username', ''),
            'has_password': bool(config.get('password')), 'trusted': bool(row['trusted_key'])}

    @router.get('/proxies')
    def proxies(page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=100)):
        rows = store.all('SELECT * FROM proxies ORDER BY created,rowid LIMIT ? OFFSET ?', (limit, (page - 1) * limit))
        return {'items': [public_proxy(row) for row in rows], 'total': store.one('SELECT COUNT(*) AS count FROM proxies')['count'], 'page': page, 'limit': limit}

    @router.get('/proxies/options')
    def proxy_options():
        # Route selectors need every node, but no addresses or credentials.
        rows = store.all('SELECT id,name,enabled,config_enc FROM proxies ORDER BY created,rowid')
        return {'items': [{'id': row['id'], 'name': row['name'], 'enabled': bool(row['enabled']),
                          'scheme': store.vault.open(row['config_enc'], f'proxy:{row["id"]}')['scheme']} for row in rows]}

    def insert_proxy(config, name=''):
        proxy_id = uuid.uuid4().hex
        store.execute('INSERT INTO proxies(id,name,config_enc,created) VALUES (?,?,?,?)',
                      (proxy_id, name or f'节点 {proxy_id[:6]}', store.vault.seal(config, f'proxy:{proxy_id}'), time.time()))
        job_id, _ = store.enqueue('proxy_test', proxy_id=proxy_id)
        return proxy_id, job_id

    @router.post('/proxies/import')
    def import_proxies(payload: ProxyImport):
        inserted, errors, job_ids = [], [], []
        for position, line in enumerate(payload.lines):
            try:
                config = parse_proxy(line)
                proxy_id, job_id = insert_proxy(config)
                inserted.append(proxy_id)
                job_ids.append(job_id)
            except ValueError as error:
                errors.append({'line': position + 1, 'message': str(error)})
        return {'inserted': len(inserted), 'errors': errors, 'job_ids': job_ids}

    @router.post('/proxies')
    def create_proxy(payload: ProxyInput):
        config = payload.model_dump(exclude={'name', 'enabled'})
        config['password'] = config['password'] or ''
        proxy_id, job_id = insert_proxy(config, payload.name)
        store.execute('UPDATE proxies SET enabled=? WHERE id=?', (int(payload.enabled), proxy_id))
        return {'id': proxy_id, 'job_ids': [job_id]}

    @router.put('/proxies/{proxy_id}')
    def update_proxy(proxy_id: str, payload: ProxyInput):
        row = store.one('SELECT * FROM proxies WHERE id=?', (proxy_id,))
        if not row:
            raise HTTPException(404, '代理不存在')
        previous = store.vault.open(row['config_enc'], f'proxy:{proxy_id}')
        config = payload.model_dump(exclude={'name', 'enabled'})
        identity_changed = any(config[key] != previous.get(key) for key in ('host', 'port', 'username'))
        host_changed = any(config[key] != previous.get(key) for key in ('host', 'port', 'scheme'))
        if config['password'] is None:
            if identity_changed and previous.get('password'):
                raise HTTPException(400, '更换地址或用户名时请重新输入密码，避免将旧凭证发往新地址')
            config['password'] = previous.get('password', '')
        store.execute("UPDATE proxies SET name=?,enabled=?,config_enc=?,status='unknown',message='',failed_until=0,latency=NULL,candidate_key=NULL,candidate_fingerprint=NULL,trusted_key=? WHERE id=?",
                      (payload.name or row['name'], int(payload.enabled), store.vault.seal(config, f'proxy:{proxy_id}'), None if host_changed else row['trusted_key'], proxy_id))
        return {'ok': True}

    @router.post('/proxies/test')
    def test_proxies(payload: Selection):
        selected = [row['id'] for row in store.all('SELECT id FROM proxies')] if payload.all_matching else payload.ids
        queued, job_ids = 0, []
        for proxy_id in dict.fromkeys(selected):
            if store.one('SELECT id FROM proxies WHERE id=?', (proxy_id,)):
                job_id, inserted = store.enqueue('proxy_test', proxy_id=proxy_id)
                queued += int(inserted)
                job_ids.append(job_id)
        return {'queued': queued, 'job_ids': job_ids}

    @router.post('/proxies/{proxy_id}/trust')
    def trust_proxy(proxy_id: str, payload: ProxyTrust):
        row = store.one('SELECT * FROM proxies WHERE id=?', (proxy_id,))
        if not row or not row['candidate_key'] or row['candidate_fingerprint'] != payload.fingerprint:
            raise HTTPException(409, '指纹已变化或尚未探测，请重新测试并核对')
        store.execute("UPDATE proxies SET trusted_key=candidate_key,candidate_key=NULL,candidate_fingerprint=NULL,status='unknown',message='已确认指纹，等待连接测试',failed_until=0 WHERE id=?", (proxy_id,))
        job_id, _ = store.enqueue('proxy_test', proxy_id=proxy_id)
        return {'ok': True, 'job_ids': [job_id]}

    @router.delete('/proxies/{proxy_id}')
    def delete_proxy(proxy_id: str):
        with store.transaction() as connection:
            if connection.execute("SELECT id FROM jobs WHERE proxy_id=? AND status='running'", (proxy_id,)).fetchone():
                raise HTTPException(409, '节点正在测试，请等待完成或取消任务')
            settings = RuntimeSettings.model_validate_json(connection.execute("SELECT value FROM meta WHERE key='settings'").fetchone()['value'])
            routes = list(settings.operation_routes.model_dump().values())
            for table, column in (('accounts', 'checkin_route'), ('schedules', 'network_route')):
                routes.extend(json.loads(row[0]) for row in connection.execute(f'SELECT {column} FROM {table}'))
            if any(route.get('mode') == 'proxy' and route.get('proxy_id') == proxy_id for route in routes):
                raise HTTPException(409, '此代理仍被操作默认线路、账号或签到计划引用；请先改为其他节点或直连，再删除，避免备份无法恢复')
            connection.execute('DELETE FROM proxies WHERE id=?', (proxy_id,))
        return {'ok': True}

    @router.put('/settings/routes')
    def operation_routes(payload: OperationRoutes):
        with store.transaction() as connection:
            for name in type(payload).model_fields:
                validate_route(store, getattr(payload, name), connection)
            previous = RuntimeSettings.model_validate_json(connection.execute("SELECT value FROM meta WHERE key='settings'").fetchone()['value'])
            updated = previous.model_copy(update={'operation_routes': payload, 'proxy_mode': 'direct'})
            connection.execute("UPDATE meta SET value=? WHERE key='settings'", (updated.model_dump_json(),))
        store.log('system', '操作线路已保存；仅显式指定的操作使用所选代理，当前执行任务保持原线路')
        return payload.model_dump()

    @router.put('/accounts/routes/checkin')
    def account_checkin_route(payload: AccountCheckinRoute):
        with store.transaction() as connection:
            validate_route(store, payload.network_route, connection)
            where, values = store.selection_where(payload)
            if not store.selection_count(payload, connection=connection):
                raise HTTPException(400, '请先选择账号')
            changed = connection.execute('UPDATE accounts SET checkin_route=? WHERE ' + where, [payload.network_route.model_dump_json(), *values]).rowcount
        store.log('system', f'已设置 {changed} 个账号的签到线路；对后续领取的任务生效')
        return {'updated': changed}

    @router.get('/schedules')
    def schedules(page: int = Query(1, ge=1), limit: int = Query(10, ge=1, le=100)):
        auto_id = store.meta('auto_schedule_id') or None
        rows = store.all('SELECT schedules.*,(SELECT COUNT(*) FROM schedule_accounts WHERE schedule_id=schedules.id) AS account_count FROM schedules '
                         'ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END,created DESC LIMIT ? OFFSET ?', (auto_id or '', limit, (page - 1) * limit))
        for row in rows:
            row['network_route'] = json.loads(row['network_route'])
        return {'items': rows, 'total': store.one('SELECT COUNT(*) AS count FROM schedules')['count'], 'page': page, 'limit': limit, 'auto_schedule_id': auto_id}

    @router.get('/schedules/{schedule_id}')
    def schedule_detail(schedule_id: str):
        schedule = store.one('SELECT * FROM schedules WHERE id=?', (schedule_id,))
        if not schedule:
            raise HTTPException(404, '签到计划不存在')
        schedule['account_count'] = store.one('SELECT COUNT(*) AS count FROM schedule_accounts WHERE schedule_id=?', (schedule_id,))['count']
        schedule['network_route'] = json.loads(schedule['network_route'])
        return schedule

    def save_schedule(payload, schedule_id=None):
        now = time.time()
        with store.transaction() as connection:
            validate_route(store, payload.network_route, connection)
            keep_accounts = bool(schedule_id and payload.keep_accounts)
            if not keep_accounts:
                count = store.selection_count(payload, connection=connection)
                if not count:
                    raise HTTPException(400, '至少选择一个已提取凭证的账号')
                where, values = store.selection_where(payload)
                missing = connection.execute('SELECT id FROM accounts WHERE (' + where + ') AND result_enc IS NULL LIMIT 1', values).fetchone()
                if missing:
                    raise HTTPException(409, '请先完成所选账号的凭证提取，再创建签到计划')
            if schedule_id:
                previous = connection.execute('SELECT * FROM schedules WHERE id=?', (schedule_id,)).fetchone()
                if not previous:
                    raise HTTPException(404, '签到计划不存在')
                changed = previous['interval_minutes'] != payload.interval_minutes or (payload.enabled and not previous['enabled'])
                next_run = now + payload.interval_minutes * 60 if changed else previous['next_run']
                connection.execute('UPDATE schedules SET name=?,interval_minutes=?,enabled=?,next_run=? WHERE id=?',
                                   (payload.name, payload.interval_minutes, int(payload.enabled), next_run, schedule_id))
                if not keep_accounts:
                    connection.execute('DELETE FROM schedule_accounts WHERE schedule_id=?', (schedule_id,))
                connection.execute("UPDATE jobs SET status='cancelled',payload_enc=NULL,message='签到计划已修改',finished=? WHERE source=? AND status='pending'", (now, f'schedule:{schedule_id}'))
            else:
                schedule_id = uuid.uuid4().hex
                connection.execute('INSERT INTO schedules(id,name,interval_minutes,enabled,next_run,created) VALUES (?,?,?,?,?,?)',
                                   (schedule_id, payload.name, payload.interval_minutes, int(payload.enabled), now + payload.interval_minutes * 60, now))
            if not keep_accounts:
                connection.execute('INSERT INTO schedule_accounts(schedule_id,account_id) SELECT ?,id FROM accounts WHERE ' + where,
                                   [schedule_id, *values])
            connection.execute('UPDATE schedules SET network_route=? WHERE id=?', (payload.network_route.model_dump_json(), schedule_id))
        return {'id': schedule_id}

    @router.post('/schedules')
    def create_schedule(payload: ScheduleInput):
        return save_schedule(payload)

    @router.put('/schedules/{schedule_id}')
    def update_schedule(schedule_id: str, payload: ScheduleInput):
        return save_schedule(payload, schedule_id)

    @router.post('/schedules/{schedule_id}/{action}')
    def schedule_action(schedule_id: str, action: str):
        schedule = store.one('SELECT * FROM schedules WHERE id=?', (schedule_id,))
        if not schedule:
            raise HTTPException(404, '签到计划不存在')
        if action == 'pause':
            store.execute('UPDATE schedules SET enabled=0 WHERE id=?', (schedule_id,))
            store.execute("UPDATE jobs SET status='cancelled',message='签到计划已暂停',finished=? WHERE source=? AND status='pending'", (time.time(), f'schedule:{schedule_id}'))
            return {'ok': True}
        if action == 'resume':
            if not store.one('SELECT account_id FROM schedule_accounts WHERE schedule_id=? LIMIT 1', (schedule_id,)):
                raise HTTPException(409, '计划中没有账号，请先重新选择账号')
            store.execute('UPDATE schedules SET enabled=1,next_run=? WHERE id=?', (time.time() + schedule['interval_minutes'] * 60, schedule_id))
            return {'ok': True}
        if action == 'run':
            queued, job_ids = 0, []
            with store.transaction() as connection:
                for account in connection.execute("SELECT account_id FROM schedule_accounts JOIN accounts ON accounts.id=account_id WHERE schedule_id=?", (schedule_id,)):
                    job_id, inserted = store.enqueue('checkin', account['account_id'], source=f'schedule:{schedule_id}', connection=connection)
                    queued += int(inserted)
                    job_ids.append(job_id)
            return {'queued': queued, 'job_ids': job_ids}
        raise HTTPException(404, '计划操作不存在')

    @router.delete('/schedules/{schedule_id}')
    def delete_schedule(schedule_id: str):
        with store.transaction() as connection:
            connection.execute('DELETE FROM schedules WHERE id=?', (schedule_id,))
            connection.execute("UPDATE jobs SET status='cancelled',message='签到计划已删除',finished=? WHERE source=? AND status='pending'", (time.time(), f'schedule:{schedule_id}'))
        return {'ok': True}

    return router
