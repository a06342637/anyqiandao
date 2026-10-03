import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db import balance_view
from app.schemas import HistorySelection, JobSelection


def selection_where(payload):
    predicates, values = [], []
    if not payload.all_matching:
        identifiers = list(dict.fromkeys(payload.ids))
        if not identifiers:
            raise HTTPException(400, '请先选择要清理的记录')
        predicates.append('id IN (' + ','.join('?' for identifier in identifiers) + ')')
        values.extend(identifiers)
    if payload.exclude_ids:
        predicates.append('id NOT IN (' + ','.join('?' for identifier in payload.exclude_ids) + ')')
        values.extend(payload.exclude_ids)
    return predicates, values


def history_router(store, auth, engine):
    router = APIRouter(prefix='/api/v1', dependencies=[Depends(auth.require)])

    def delete_history(table, payload):
        if table == 'checkins' and payload.category:
            raise HTTPException(400, '签到明细不支持日志分类筛选')
        predicates, values = selection_where(payload)
        for field in ('account_id', 'job_id', 'category'):
            value = getattr(payload, field)
            if value:
                predicates.append(f'{field}=?')
                values.append(value)
        if payload.before is not None:
            if payload.before > time.time():
                raise HTTPException(400, '清理截止时间不能晚于当前时间')
            predicates.append('created<?')
            values.append(payload.before)
        where = ' AND '.join(predicates) or '1'
        protected = "(job_id IS NULL OR job_id NOT IN (SELECT id FROM jobs WHERE status IN ('pending','running')))"
        with store.transaction() as connection:
            total = connection.execute(f'SELECT COUNT(*) FROM {table} WHERE {where}', values).fetchone()[0]
            removed = connection.execute(f'DELETE FROM {table} WHERE ({where}) AND {protected}', values).rowcount
        label = '运行/账号日志' if table == 'logs' else '账号签到明细'
        store.log('system', f'手动清理 {removed} 条{label}，保留 {total - removed} 条等待/运行任务相关记录；账号与凭证不受影响')
        return {'removed': removed, 'protected': total - removed}

    @router.post('/logs/delete')
    def delete_logs(payload: HistorySelection):
        return delete_history('logs', payload)

    @router.get('/checkins')
    def checkins(account_id: str | None = None, page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100)):
        where, values = ('WHERE account_id=?', [account_id]) if account_id else ('', [])
        rows = store.all(f'SELECT * FROM checkins {where} ORDER BY created DESC,id DESC LIMIT ? OFFSET ?', [*values, limit, (page - 1) * limit])
        names = {}
        for row in rows:
            identifier = row['account_id']
            if identifier not in names:
                account = store.account(identifier) if identifier else None
                names[identifier] = account['login']['username'] if account else None
        return {'items': [{'id': str(row['id']), 'account_id': row['account_id'], 'job_id': row['job_id'],
                           'username': names[row['account_id']], **balance_view(row)} for row in rows],
                'total': store.one(f'SELECT COUNT(*) AS count FROM checkins {where}', values)['count'], 'page': page, 'limit': limit}

    @router.post('/checkins/delete')
    def delete_checkins(payload: HistorySelection):
        return delete_history('checkins', payload)

    @router.post('/jobs/delete')
    async def delete_jobs(payload: JobSelection):
        predicates, values = selection_where(payload)
        predicates.append('hidden=0')
        if payload.lane:
            predicates.append("kind='checkin'" if payload.lane == 'checkin' else "kind!='checkin'")
        if payload.state:
            predicates.append("status IN ('pending','running')" if payload.state == 'active' else "status NOT IN ('pending','running')")
        rows = store.all('SELECT id,status FROM jobs WHERE ' + ' AND '.join(predicates), values)
        active = [row for row in rows if row['status'] in ('pending', 'running')]
        for offset in range(0, len(active), 20):
            await asyncio.gather(*(engine.cancel_and_wait(row['id']) for row in active[offset:offset + 20]))
        removed = 0
        with store.transaction() as connection:
            for row in rows:
                removed += connection.execute("UPDATE jobs SET hidden=1 WHERE id=? AND hidden=0 AND status NOT IN ('pending','running')", (row['id'],)).rowcount
        store.log('system', f'批量移除 {removed} 条队列记录；账号、执行日志和签到统计保留，未安全结束的任务不会删除')
        return {'removed': removed, 'protected': len(rows) - removed}

    return router
