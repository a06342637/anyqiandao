import math
import time
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from app.errors import TaskError
from app.responses import parse_response, profile_result


def number_of(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return value if math.isfinite(value) else None
        except OverflowError:
            return None
    return None


def money_of(value):
    number = number_of(value)
    return round(number / 500000, 6) if number is not None else None


def text_of(value, limit=512):
    return ''.join(character for character in value if character.isprintable())[:limit] if isinstance(value, str) else ''


def response_data(status, text):
    payload = parse_response(status, text)
    if payload.get('success') is not True:
        raise TaskError('upstream_error', '网站未提供这项数据；可能不支持或暂时不可用，请稍后重试')
    return payload.get('data')


def profile_view(profile):
    return {'username': text_of(profile.get('username')), 'display_name': text_of(profile.get('display_name')),
            'quota': money_of(profile.get('quota')), 'used_quota': money_of(profile.get('used_quota')),
            'request_count': number_of(profile.get('request_count')), 'group': text_of(profile.get('group')),
            'status': number_of(profile.get('status'))}


def token_view(token, expected_id):
    if not isinstance(token, dict):
        raise TaskError('upstream_error', '网站令牌列表格式异常，未展示不完整数据')
    if token.get('user_id') is not None and str(token['user_id']) != str(expected_id):
        raise TaskError('invalid', '令牌所属用户与当前凭证不匹配，已停止读取')
    raw_key = token.get('key')
    key = raw_key if isinstance(raw_key, str) and 8 <= len(raw_key) <= 512 and all(character.isascii() and (character.isalnum() or character in '-_') for character in raw_key) else None
    limits = token.get('model_limits', '')
    if isinstance(limits, str):
        models = [text_of(model.strip(), 200) for model in limits.split(',') if model.strip()]
    elif isinstance(limits, list):
        models = [text_of(model, 200) for model in limits if isinstance(model, str)]
    else:
        models = []
    allowed_ips = token.get('allow_ips', token.get('allowed_ips', ''))
    return {'id': str(token.get('id', '')), 'name': text_of(token.get('name')) or '未命名令牌',
            'key': key if not key or key.startswith('sk-') else 'sk-' + key,
            'status': number_of(token.get('status')), 'used_quota': money_of(token.get('used_quota')),
            'remain_quota': money_of(token.get('remain_quota')), 'unlimited_quota': token.get('unlimited_quota') is True,
            'created_time': number_of(token.get('created_time')), 'expired_time': number_of(token.get('expired_time')),
            'accessed_time': number_of(token.get('accessed_time')), 'group': text_of(token.get('group')),
            'model_limits_enabled': token.get('model_limits_enabled') is True, 'model_limits': models,
            'allowed_ips': [text_of(value.strip(), 200) for value in allowed_ips.replace(',', '\n').splitlines() if value.strip()] if isinstance(allowed_ips, str) else []}


def usage_view(rows, since, until, bucket, timezone, expected_id):
    if not isinstance(rows, list):
        raise TaskError('upstream_error', '网站使用统计格式异常，未将缺失数据当作零使用')
    requests = tokens = quota = 0
    series, models = {}, {}
    zone = ZoneInfo(timezone)
    for row in rows:
        if not isinstance(row, dict) or any(number_of(row.get(field)) is None for field in ('count', 'token_used', 'quota', 'created_at')):
            raise TaskError('upstream_error', '网站使用统计字段不完整，未将缺失数据当作零使用')
        if row.get('user_id') is not None and str(row['user_id']) != str(expected_id):
            raise TaskError('invalid', '使用统计所属用户与当前凭证不匹配，已停止读取')
        if row['count'] < 0 or row['token_used'] < 0 or not 0 <= row['created_at'] <= 253402300799:
            raise TaskError('upstream_error', '网站使用统计数值异常')
        requests += row['count']
        tokens += row['token_used']
        quota += row['quota']
        label = datetime.fromtimestamp(row['created_at'], zone).strftime('%m-%d %H:00' if bucket == 'hour' else '%Y-%m-%d')
        model = text_of(row.get('model_name'), 200) or '未知模型'
        for target in (series.setdefault(label, {'label': label, 'requests': 0, 'tokens': 0, 'quota': 0}),
                       models.setdefault(model, {'model': model, 'requests': 0, 'tokens': 0, 'quota': 0})):
            target['requests'] += row['count']
            target['tokens'] += row['token_used']
            target['quota'] += row['quota']
    minutes = max(1, (until - since) / 60)
    convert = lambda row: {key: value for key, value in row.items() if key != 'quota'} | {'cost': money_of(row['quota'])}
    return {'requests': requests, 'tokens': tokens, 'cost': money_of(quota), 'rpm': requests / minutes, 'tpm': tokens / minutes,
            'series': [convert(row) for _, row in sorted(series.items())],
            'models': [convert(row) for row in sorted(models.values(), key=lambda value: (-value['quota'], value['model']))]}


async def read_console(session, credentials, settings, options):
    headers = {'New-Api-User': str(credentials['api_user']), 'Accept': 'application/json', 'Cache-Control': 'no-store'}
    status, text = await session.api('/api/user/self', headers=headers)
    profile = profile_result(status, text, credentials['api_user'])
    result = {'view': options['view'], 'profile': profile_view(profile), 'fetched_at': time.time()}
    if options['view'] == 'tokens':
        page, limit = options['page'], options['limit']
        status, text = await session.api('/api/token/?' + urlencode({'p': page - 1, 'size': limit}), headers=headers)
        data = response_data(status, text)
        total = number_of(data.get('total')) if isinstance(data, dict) else None
        rows = data.get('items') if isinstance(data, dict) else data
        if not isinstance(rows, list) or len(rows) > limit:
            raise TaskError('upstream_error', '网站令牌列表分页格式异常')
        return {**result, 'items': [token_view(row, credentials['api_user']) for row in rows], 'page': page, 'limit': limit,
                'total': total, 'has_more': page * limit < total if total is not None else len(rows) == limit}
    until = int(time.time())
    since = until - {'day': 1, 'week': 7, 'month': 30}[options['range']] * 86400
    bucket = 'hour' if options['range'] == 'day' else 'day'
    result.update(range=options['range'], since=since, until=until, timezone=settings.timezone, usage=None, usage_error='')
    try:
        query = urlencode({'start_timestamp': since, 'end_timestamp': until, 'default_time': bucket})
        status, text = await session.api('/api/data/self/?' + query, headers=headers)
        result['usage'] = usage_view(response_data(status, text), since, until, bucket, settings.timezone, credentials['api_user'])
    except TaskError as error:
        if error.code == 'invalid':
            raise
        result['usage_error'] = error.message
    return result
