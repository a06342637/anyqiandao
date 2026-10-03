import time
import uuid

from fastapi import APIRouter, Depends, HTTPException

from app.schemas import CredentialImport, CredentialPair


def credential_router(store, auth):
    router = APIRouter(prefix='/api/v1', dependencies=[Depends(auth.require)])

    def enqueue_checkin(connection, account_id):
        settings = store.settings()
        if not settings.auto_checkin:
            return None
        schedule_id = store.auto_schedule(connection, settings.auto_checkin_interval_minutes)
        connection.execute('INSERT OR IGNORE INTO schedule_accounts VALUES (?,?)', (schedule_id, account_id))
        job_id, _ = store.enqueue('checkin', account_id, source='import:session', connection=connection)
        return job_id

    @router.post('/accounts/import-credentials')
    def import_credentials(payload: CredentialImport):
        if not store.enough_space():
            raise HTTPException(507, '数据盘可用空间不足 64MB，未导入凭证')
        added, skipped, identifiers, jobs = 0, 0, [], []
        with store.transaction() as connection:
            known = {}
            for row in connection.execute('SELECT id,result_enc FROM accounts WHERE result_enc IS NOT NULL'):
                result = store.vault.open(row['result_enc'], f'result:{row["id"]}')
                known[str(result.get('api_user', ''))] = row['id']
            for item in payload.accounts:
                username = item.username.strip() or f'手动账号 {item.api_user}'
                fingerprint = store.vault.fingerprint(username)
                duplicate = connection.execute('SELECT id FROM accounts WHERE login_hash=?', (fingerprint,)).fetchone()
                existing = known.get(item.api_user) or (duplicate['id'] if duplicate else None)
                if existing:
                    skipped += 1
                    identifiers.append(existing)
                    continue
                identifier = uuid.uuid4().hex
                now = time.time()
                login = store.vault.seal({'username': username, 'password': None}, f'login:{identifier}')
                result = store.vault.seal({'session': item.session, 'api_user': item.api_user}, f'result:{identifier}')
                connection.execute("INSERT INTO accounts(id,login_hash,login_enc,result_enc,validity,message,last_extracted,created,updated,position) VALUES (?,?,?,?,'unknown',?,?,?,?,(SELECT COALESCE(MAX(position),0)+1 FROM accounts))",
                                   (identifier, fingerprint, login, result, 'Session 手动导入；仅签到，失效后等待手动更新凭证', now, now, now))
                known[item.api_user] = identifier
                identifiers.append(identifier)
                added += 1
                job_id = enqueue_checkin(connection, identifier)
                if job_id:
                    jobs.append(job_id)
        store.log('system', f'手动导入 {added} 个 Session 账号，跳过 {skipped} 个重复账号；不保存或猜测密码，不执行自动登录')
        return {'added': added, 'skipped': skipped, 'account_ids': identifiers, 'job_ids': jobs, 'queued': len(jobs)}

    @router.put('/accounts/{account_id}/credentials')
    def update_credentials(account_id: str, payload: CredentialPair):
        with store.transaction() as connection:
            row = connection.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
            if not row:
                raise HTTPException(404, '账号不存在')
            if connection.execute("SELECT 1 FROM jobs WHERE account_id=? AND status IN ('pending','running') LIMIT 1", (account_id,)).fetchone():
                raise HTTPException(409, '该账号仍有等待或执行中的任务，请先完成或取消，再更新凭证')
            old = store.vault.open(row['result_enc'], f'result:{account_id}') if row['result_enc'] else None
            if old and str(old.get('api_user')) != payload.api_user:
                raise HTTPException(400, 'api_user 与当前账号不一致；不同账号请使用新导入，避免混用签到统计')
            result = store.vault.seal(payload.model_dump(), f'result:{account_id}')
            now = time.time()
            connection.execute("UPDATE accounts SET result_enc=?,validity='unknown',message='凭证已手动更新，等待验证；未执行自动登录',last_extracted=?,last_validated=NULL,quota=NULL,updated=? WHERE id=?",
                               (result, now, now, account_id))
            connection.execute('DELETE FROM console_results WHERE account_id=?', (account_id,))
            job_id = enqueue_checkin(connection, account_id)
        store.log('system', 'Session 与 api_user 已加密更新；保留账号历史，未执行登录', account_id=account_id)
        return {'ok': True, 'job_ids': [job_id] if job_id else []}

    return router
