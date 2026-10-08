"""Explicit route selection; adding a proxy never opts any operation into it."""
from fastapi import HTTPException

from app.schemas import NetworkRoute

OPERATION_LABELS = {'extract': '登录 / 提取 Cookie', 'refresh': '更新凭证', 'validate': '检测凭证',
                    'checkin': '签到', 'tokens': 'API 令牌', 'dashboard': '数据看板'}


def validate_route(store, route, connection=None):
    if route.mode != 'proxy':
        return
    row = (connection.execute('SELECT enabled FROM proxies WHERE id=?', (route.proxy_id,)).fetchone()
           if connection is not None else store.one('SELECT enabled FROM proxies WHERE id=?', (route.proxy_id,)))
    if not row or not row['enabled']:
        raise HTTPException(400, '所选代理不存在或未启用，请重新选择')


def route_for(store, settings, job, account, operation):
    if operation == 'checkin':
        if job['source'].startswith('schedule:'):
            row = store.one('SELECT network_route FROM schedules WHERE id=?', (job['source'].split(':', 1)[1],))
            if row:
                route = NetworkRoute.model_validate_json(row['network_route'])
                if route.mode != 'inherit':
                    return route
        route = NetworkRoute.model_validate_json(account.get('checkin_route') or '{"mode":"inherit"}')
        if route.mode != 'inherit':
            return route
    return getattr(settings.operation_routes, 'validation' if operation == 'validate' else operation)
