"""One-click backup and restore of everything the user typed or earned: accounts (with passwords and
credentials), notes and ordering, schedules, proxies, check-in statistics and runtime settings.

The archive is plain JSON so it can be restored onto a server with a different encryption key.
It therefore contains secrets; the UI warns about that and the file is never written to disk here.
"""
import json
import io
import time
import uuid
import zipfile
import zlib

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.backup_schema import BackupDocument
from app.checkin_state import DailyCheckinState, merge_daily_state, valid_observation, observe_balance, submitted_today, beijing_day
from app.config import VERSION
from app.db import SCHEMA_VERSION
from app.schemas import RuntimeSettings
from app.backup_encryption import MAGIC

FORMAT = 1
RESTORE_LIMIT = 64 * 1024 * 1024


def backup_document(store):
    with store.connection() as snapshot:
        snapshot.execute('BEGIN')
        metadata = dict(snapshot.execute('SELECT key,value FROM meta'))
        def read(query, values=()):
            return [dict(row) for row in snapshot.execute(query, values)]
        accounts = []
        for row in read('SELECT * FROM accounts ORDER BY position,created,rowid'):
            login = store.vault.open(row['login_enc'], f'login:{row["id"]}')
            result = store.vault.open(row['result_enc'], f'result:{row["id"]}') if row['result_enc'] else None
            daily = store.daily_state(row)
            accounts.append({'username': login['username'], 'password': login.get('password'), 'note': row['note'], 'result': result,
                             'validity': row['validity'], 'message': row['message'], 'quota': row['quota'], 'checkin_status': row['checkin_status'],
                             'last_validated': row['last_validated'], 'last_extracted': row['last_extracted'], 'last_checkin': row['last_checkin'],
                             'created': row['created'], 'updated': row['updated'], 'daily_checkin': daily.model_dump() if daily else None,
                             'checkin_route': json.loads(row['checkin_route'])})
        usernames = {row['id']: account['username'] for row, account in zip(read('SELECT id FROM accounts ORDER BY position,created,rowid'), accounts)}
        schedules = []
        auto_id = metadata.get('auto_schedule_id')
        for row in read('SELECT * FROM schedules ORDER BY created'):
            members = [usernames[item['account_id']] for item in read('SELECT account_id FROM schedule_accounts WHERE schedule_id=?', (row['id'],)) if item['account_id'] in usernames]
            schedules.append({'name': row['name'], 'interval_minutes': row['interval_minutes'], 'enabled': bool(row['enabled']), 'next_run': row['next_run'],
                              'last_run': row['last_run'], 'created': row['created'], 'accounts': members, 'auto': row['id'] == auto_id,
                              'network_route': json.loads(row['network_route'])})
        proxies = []
        for row in read('SELECT * FROM proxies ORDER BY created,rowid'):
            config = store.vault.open(row['config_enc'], f'proxy:{row["id"]}')
            proxies.append({'id': row['id'], 'name': row['name'], 'enabled': bool(row['enabled']), 'config': config, 'trusted_key': row['trusted_key'], 'created': row['created']})
        checkins = [{'username': usernames.get(row['account_id']), 'code': row['code'], 'quota_before': row['quota_before'], 'quota_after': row['quota_after'],
                     'used_before': row['used_before'], 'used_after': row['used_after'], 'reward_amount': row['reward_amount'], 'created': row['created'], 'balance_source': row['balance_source']}
                    for row in read('SELECT * FROM checkins ORDER BY created') if row['account_id'] in usernames]
        document = {'app': 'any-signin-assistant', 'format': FORMAT, 'version': VERSION, 'schema_version': SCHEMA_VERSION, 'created': time.time(),
                    'settings': RuntimeSettings.model_validate_json(metadata['settings']).model_dump(), 'accounts': accounts, 'schedules': schedules, 'proxies': proxies, 'checkins': checkins}
    return document


def backup_router(store, auth):
    router = APIRouter(prefix='/api/v1', dependencies=[Depends(auth.require)])

    @router.post('/backup')
    def backup():
        document = backup_document(store)
        accounts, schedules, proxies, checkins = (document[key] for key in ('accounts', 'schedules', 'proxies', 'checkins'))
        body = json.dumps(document, ensure_ascii=False, separators=(',', ':')).encode()
        if len(body) > RESTORE_LIMIT:
            raise HTTPException(413, '备份数据超过 64MB，请使用服务器备份脚本')
        store.log('backup', f'已生成备份：{len(accounts)} 个账号、{len(schedules)} 个计划、{len(proxies)} 个代理、{len(checkins)} 条签到记录')
        stamp = time.strftime('%Y%m%d-%H%M%S')
        return StreamingResponse(iter([body]), media_type='application/json',
                                 headers={'Content-Disposition': f'attachment; filename="any-signin-backup-{stamp}.json"', 'Cache-Control': 'no-store',
                                          'X-Backup-Accounts': str(len(accounts))})

    @router.post('/backup/restore')
    async def restore(request: Request):
        raw = await request.body()
        if len(raw) > RESTORE_LIMIT:
            raise HTTPException(413, '备份文件超过 64MB')
        if raw.startswith(MAGIC):
            raise HTTPException(400, '这是旧版 ASB 加密备份，请先使用 scripts/decrypt_backup.py 转为 JSON 或 ZIP，再导入')
        try:
            if raw.startswith(b'PK'):
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    entry = archive.getinfo('backup.json')
                    if entry.file_size > RESTORE_LIMIT:
                        raise HTTPException(413, '备份数据解压后超过 64MB')
                    with archive.open(entry) as file:
                        raw = file.read(RESTORE_LIMIT + 1)
                    if len(raw) > RESTORE_LIMIT:
                        raise HTTPException(413, '备份数据解压后超过 64MB')
            document = json.loads(raw)
        except (ValueError, KeyError, zipfile.BadZipFile, RuntimeError, NotImplementedError, zlib.error, EOFError):
            raise HTTPException(400, '备份文件不是有效的 JSON 或不包含 backup.json 的 ZIP') from None
        if not isinstance(document, dict) or document.get('app') != 'any-signin-assistant' or document.get('format') != FORMAT:
            raise HTTPException(400, '不是本程序生成的备份文件，或格式版本不兼容')
        try:
            document = BackupDocument.model_validate(document).model_dump()
            restored_settings = {key: value for key, value in (document['settings'] or {}).items() if key in RuntimeSettings.model_fields}
            RuntimeSettings.model_validate(store.settings().model_dump() | restored_settings)
        except ValueError:
            raise HTTPException(400, '备份内容校验失败：请检查格式版本、账号、凭证、时间或余额字段；未修改任何数据') from None
        accounts = document['accounts']
        summary = {'accounts_added': 0, 'accounts_updated': 0, 'schedules': 0, 'proxies': 0, 'checkins': 0}
        now = time.time()
        with store.transaction() as connection:
            if connection.execute("SELECT 1 FROM jobs WHERE status='running' LIMIT 1").fetchone():
                raise HTTPException(409, '有任务正在执行，请先暂停队列并等待完成后再恢复')
            ids_by_name = {}
            schedule_routes, account_routes, proxy_ids = [], [], {}
            for item in accounts:
                username = str(item.get('username', '')).strip()
                if not username:
                    continue
                fingerprint = store.vault.fingerprint(username)
                existing = connection.execute('SELECT * FROM accounts WHERE login_hash=?', (fingerprint,)).fetchone()
                account_id = existing['id'] if existing else uuid.uuid4().hex
                login = {'username': username}
                if item.get('password'):
                    login['password'] = str(item['password'])
                result = item.get('result') if isinstance(item.get('result'), dict) and item['result'].get('session') else None
                incoming_state = DailyCheckinState.model_validate(item['daily_checkin']) if item.get('daily_checkin') else None
                if not result or not valid_observation(incoming_state, result.get('api_user'), now):
                    incoming_state = None
                if existing:
                    current_login = store.vault.open(existing['login_enc'], f'login:{account_id}')
                    current_state = store.daily_state(existing)
                    incoming_time = max([item.get(field) or 0 for field in ('updated', 'last_validated', 'last_extracted', 'last_checkin', 'created')]
                                        + [incoming_state.observed_at if incoming_state else 0])
                    current_time = max([existing[field] or 0 for field in ('updated', 'last_validated', 'last_extracted', 'last_checkin', 'created')]
                                       + [current_state.observed_at if current_state and valid_observation(current_state, current_state.api_user, now) else 0])
                    newer = incoming_time >= current_time
                    if login.get('password') and (newer or not current_login.get('password')):
                        current_login['password'] = login['password']
                    take_result = result and newer and (not existing['result_enc'] or (item.get('last_extracted') or 0) >= (existing['last_extracted'] or 0))
                    result_enc = store.vault.seal(result, f'result:{account_id}') if take_result else existing['result_enc']
                    take_balance = item.get('quota') is not None and (existing['quota'] is None or newer)
                    take_checkin = (item.get('last_checkin') or 0) > (existing['last_checkin'] or 0)
                    connection.execute('UPDATE accounts SET login_enc=?,result_enc=?,note=?,validity=?,quota=?,last_extracted=?,last_validated=?,last_checkin=?,checkin_status=?,message=?,updated=? WHERE id=?',
                                       (store.vault.seal(current_login, f'login:{account_id}'), result_enc, item['note'] if newer else existing['note'],
                                        item['validity'] if take_result else existing['validity'], item['quota'] if take_balance else existing['quota'],
                                        item['last_extracted'] if take_result else existing['last_extracted'], item['last_validated'] if newer else existing['last_validated'],
                                        item['last_checkin'] if take_checkin else existing['last_checkin'], item['checkin_status'] if take_checkin else existing['checkin_status'],
                                        item['message'] if newer else existing['message'], max(incoming_time, current_time), account_id))
                    summary['accounts_updated'] += 1
                else:
                    connection.execute('INSERT INTO accounts(id,login_hash,login_enc,result_enc,validity,message,last_validated,last_extracted,last_checkin,checkin_status,quota,created,updated,note,position) '
                                       'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,(SELECT COALESCE(MAX(position),0)+1 FROM accounts))',
                                       (account_id, fingerprint, store.vault.seal(login, f'login:{account_id}'), store.vault.seal(result, f'result:{account_id}') if result else None,
                                        item.get('validity') or 'unknown', str(item.get('message') or '')[:500], item.get('last_validated'), item.get('last_extracted'), item.get('last_checkin'),
                                        item.get('checkin_status'), item.get('quota'), item.get('created') or now, now, str(item.get('note') or '')[:500]))
                    summary['accounts_added'] += 1
                ids_by_name[username] = account_id
                if item.get('checkin_route') is not None:
                    account_routes.append((account_id, item['checkin_route']))
                current = connection.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
                current_state = store.daily_state(current)
                credentials = store.vault.open(current['result_enc'], f'result:{account_id}') if current['result_enc'] else {}
                merged = merge_daily_state(current_state, incoming_state, credentials.get('api_user'), now)
                legacy_at = item.get('last_checkin') or 0
                if (document['schema_version'] < 10 and item.get('checkin_status') == 'uncertain'
                        and result and str(result.get('api_user')) == str(credentials.get('api_user'))
                        and 0 < legacy_at <= now and beijing_day(legacy_at) == beijing_day(now)
                        and not submitted_today(merged, credentials['api_user'], now)):
                    merged = observe_balance(merged, credentials['api_user'], None, None, now)
                    merged.submitted_at = legacy_at
                connection.execute('UPDATE accounts SET checkin_state_enc=? WHERE id=?',
                                   (store.vault.seal(merged.model_dump(), f'checkin-state:{account_id}') if merged else None, account_id))
            for item in document.get('schedules') or []:
                if not isinstance(item, dict) or not item.get('name'):
                    continue
                interval = max(5, min(int(item.get('interval_minutes') or 1440), 525600))
                schedule = connection.execute('SELECT id FROM schedules WHERE name=?', (item['name'],)).fetchone()
                if item.get('auto'):
                    schedule_id = store.auto_schedule(connection, interval)
                    connection.execute('UPDATE schedules SET interval_minutes=?,enabled=? WHERE id=?', (interval, int(bool(item.get('enabled', True))), schedule_id))
                elif schedule:
                    schedule_id = schedule['id']
                    connection.execute('UPDATE schedules SET interval_minutes=?,enabled=? WHERE id=?', (interval, int(bool(item.get('enabled', True))), schedule_id))
                else:
                    schedule_id = uuid.uuid4().hex
                    connection.execute('INSERT INTO schedules(id,name,interval_minutes,enabled,next_run,last_run,created) VALUES (?,?,?,?,?,?,?)',
                                       (schedule_id, str(item['name'])[:100], interval, int(bool(item.get('enabled', True))), now + interval * 60, item.get('last_run'), item.get('created') or now))
                for username in item.get('accounts') or []:
                    if username in ids_by_name:
                        connection.execute('INSERT OR IGNORE INTO schedule_accounts VALUES (?,?)', (schedule_id, ids_by_name[username]))
                summary['schedules'] += 1
                if item.get('network_route') is not None:
                    schedule_routes.append((schedule_id, item['network_route']))
            for item in document.get('proxies') or []:
                config = item.get('config') if isinstance(item, dict) else None
                if not isinstance(config, dict) or not config.get('host') or not config.get('port'):
                    continue
                duplicate = False
                for row in connection.execute('SELECT id,config_enc FROM proxies').fetchall():
                    current = store.vault.open(row['config_enc'], f'proxy:{row["id"]}')
                    if (current.get('scheme') == config.get('scheme') and current.get('host') == config['host']
                            and current.get('port') == config['port'] and current.get('username', '') == config.get('username', '')):
                        duplicate = True
                        break
                if duplicate:
                    if item.get('id'):
                        proxy_ids[item['id']] = row['id']
                    continue
                proxy_id = uuid.uuid4().hex
                connection.execute('INSERT INTO proxies(id,name,config_enc,enabled,trusted_key,created) VALUES (?,?,?,?,?,?)',
                                   (proxy_id, str(item.get('name') or config['host'])[:100], store.vault.seal(config, f'proxy:{proxy_id}'), int(bool(item.get('enabled', True))), item.get('trusted_key'), item.get('created') or now))
                summary['proxies'] += 1
                if item.get('id'):
                    proxy_ids[item['id']] = proxy_id
            def mapped_route(route):
                route = dict(route)
                if route.get('mode') == 'proxy':
                    route['proxy_id'] = proxy_ids.get(route.get('proxy_id'))
                    if not route['proxy_id']:
                        raise HTTPException(400, '备份中的线路缺少对应代理节点，未恢复数据；请使用包含代理的完整应用备份')
                return route
            for account_id, route in account_routes:
                connection.execute('UPDATE accounts SET checkin_route=? WHERE id=?', (json.dumps(mapped_route(route)), account_id))
            for schedule_id, route in schedule_routes:
                connection.execute('UPDATE schedules SET network_route=? WHERE id=?', (json.dumps(mapped_route(route)), schedule_id))
            if 'operation_routes' in restored_settings:
                restored_settings['operation_routes'] = {name: mapped_route(route) for name, route in restored_settings['operation_routes'].items()}
            existing_checkins = {(row['account_id'], round(row['created'], 3)) for row in connection.execute('SELECT account_id,created FROM checkins').fetchall()}
            for item in document.get('checkins') or []:
                if not isinstance(item, dict) or item.get('username') not in ids_by_name or not item.get('created'):
                    continue
                key = (ids_by_name[item['username']], round(float(item['created']), 3))
                if key in existing_checkins:
                    continue
                existing_checkins.add(key)
                connection.execute('INSERT INTO checkins(account_id,job_id,code,quota_before,quota_after,used_before,used_after,created,balance_source,reward_amount) VALUES (?,?,?,?,?,?,?,?,?,?)',
                                   (key[0], None, str(item.get('code') or 'signed')[:40], item.get('quota_before'), item.get('quota_after'),
                                    item.get('used_before'), item.get('used_after'), float(item['created']), 'live' if item.get('balance_source') == 'live' else 'legacy', item.get('reward_amount')))
                summary['checkins'] += 1
            if restored_settings:
                current = connection.execute("SELECT value FROM meta WHERE key='settings'").fetchone()['value']
                merged = RuntimeSettings.model_validate(RuntimeSettings.model_validate_json(current).model_dump() | restored_settings)
                connection.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('settings', merged.model_dump_json()))
        store.log('backup', f'已从备份恢复：新增 {summary["accounts_added"]} 个账号，更新 {summary["accounts_updated"]} 个，{summary["schedules"]} 个计划，{summary["proxies"]} 个代理，{summary["checkins"]} 条签到记录')
        return summary

    return router
