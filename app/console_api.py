import hashlib
import time

from fastapi import APIRouter, Depends, HTTPException

from app.schemas import InsightInput


def console_router(store, auth):
    router = APIRouter(prefix='/api/v1', dependencies=[Depends(auth.require)])

    @router.post('/accounts/{account_id}/insights')
    def load_insights(account_id: str, payload: InsightInput):
        if not store.enough_space():
            raise HTTPException(507, '存储空间不足，未提交查询')
        with store.transaction() as connection:
            account = connection.execute('SELECT result_enc,validity,login_enc FROM accounts WHERE id=?', (account_id,)).fetchone()
            if not account:
                raise HTTPException(404, '账号不存在')
            if not account['result_enc'] or account['validity'] == 'invalid':
                login = store.vault.open(account['login_enc'], f'login:{account_id}')
                raise HTTPException(409, '凭证缺失或已失效，请先' + ('重新提取凭证' if login.get('password') else '手动更新 session 与 api_user'))
            previous = connection.execute("SELECT id,payload_enc FROM jobs WHERE kind='insights' AND account_id=? AND status IN ('pending','running')", (account_id,)).fetchone()
            if previous and store.vault.open(previous['payload_enc'], f'job:{previous["id"]}') != payload.model_dump():
                raise HTTPException(409, '该账号已有数据查询排队，请等待完成后再切换范围或页面')
            job_id, added = store.enqueue('insights', account_id, source='console', payload=payload.model_dump(), connection=connection)
        return {'job_id': job_id, 'queued': added}

    @router.get('/accounts/{account_id}/insights/{job_id}')
    def insights(account_id: str, job_id: str):
        job = store.one("SELECT status,message FROM jobs WHERE id=? AND account_id=? AND kind='insights'", (job_id, account_id))
        if not job:
            raise HTTPException(404, '查询任务不存在')
        result = None
        if job['status'] == 'success':
            row = store.one('SELECT * FROM console_results WHERE job_id=? AND account_id=? AND expires>?', (job_id, account_id, time.time()))
            if not row:
                raise HTTPException(410, '查询结果已过期，请重新加载')
            account = store.one('SELECT result_enc FROM accounts WHERE id=?', (account_id,))
            if not account or hashlib.sha256((account['result_enc'] or '').encode()).hexdigest() != row['credential_hash']:
                raise HTTPException(409, '账号凭证已更新，请重新查询，旧结果不会展示')
            result = store.vault.open(row['result_enc'], f'console:{job_id}')
        return {**job, 'data': result, 'paused': store.meta('queue_paused') == '1'}

    return router
