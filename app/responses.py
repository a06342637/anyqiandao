import json

from app.errors import TaskError

EXPIRED = ('未登录', '请先登录', '登录已过期', '登录已失效', '登录状态无效', '登录状态已失效', '登录状态已过期', '登录信息已过期', 'cookie已失效', 'cookie失效', 'cookie已过期', 'cookie过期', 'cookie无效', 'session expired', 'session has expired', 'session is invalid', 'invalid session', 'cookie expired', 'cookie has expired', 'cookie is invalid', 'invalid cookie', 'not logged', 'unauthenticated')
ALREADY = ('已经签到', '已签到', '重复签到', 'already checked', 'already signed')
SIGNED = ('签到成功', 'check-in successful', 'check in successful', 'checked in successfully', 'sign-in successful', 'signed in successfully')
CAPTCHA = ('captcha', 'challenges.cloudflare.com', '人机验证', '安全验证', '验证码', '滑动验证')


def is_waf_challenge(text):
    # Alibaba Cloud WAF serves a tiny HTML page whose script derives the acw_sc__v2 cookie and reloads; a browser passes it without any human action.
    head = (text or '').lstrip()[:400].lower()
    return head.startswith('<html') and 'arg1' in text and ('acw_sc__' in text or 'reload' in text)


def parse_response(status, text):
    if status == 401:
        raise TaskError('invalid', '登录凭证已失效，请重新提取')
    if status in (403, 429):
        raise TaskError('needs_manual', '网站拒绝访问或触发风控，需要人工处理')
    if status >= 500:
        raise TaskError('upstream_error', f'网站暂时异常（HTTP {status}），未判定凭证失效')
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        if is_waf_challenge(text):
            raise TaskError('upstream_error', '网站返回了 WAF 验证页面，本次未判定凭证失效；可更换代理后重试', retry_proxy=True) from None
        if any(marker in text.lower() for marker in CAPTCHA):
            raise TaskError('needs_manual', '网站返回了验证码页面，需要人工处理') from None
        raise TaskError('upstream_error', '网站响应格式异常，未判定凭证失效') from None
    if not isinstance(payload, dict):
        raise TaskError('upstream_error', '网站响应格式异常')
    message = str(payload.get('message', payload.get('msg', ''))).lower()
    compact = ''.join(message.split())
    if any(marker in message or marker.replace(' ', '') in compact for marker in EXPIRED):
        raise TaskError('invalid', '登录凭证已失效，请重新提取')
    if status != 200:
        raise TaskError('upstream_error', f'网站返回 HTTP {status}，请稍后重试')
    return payload


def profile_result(status, text, expected_id):
    payload = parse_response(status, text)
    profile = payload.get('data')
    if payload.get('success') is not True or not isinstance(profile, dict):
        raise TaskError('upstream_error', '网站未确认登录有效，保留原凭证等待复查')
    user_id = profile.get('id')
    if isinstance(user_id, bool) or not str(user_id).isdigit() or int(user_id) <= 0:
        raise TaskError('upstream_error', '网站未返回可验证的用户 ID')
    if expected_id and str(user_id) != str(expected_id):
        raise TaskError('invalid', '用户 ID 与当前凭证不匹配，请重新提取')
    return profile


def checkin_result(status, text):
    payload = parse_response(status, text)
    message = str(payload.get('message', payload.get('msg', ''))).lower()
    if any(marker in message for marker in ALREADY):
        return 'already_signed', '网站提示已签到，无需重复领取'
    explicit = payload.get('success')
    success = explicit is True
    if explicit is None:
        success = (type(payload.get('ret')) is int and payload['ret'] == 1) or (type(payload.get('code')) is int and payload['code'] == 0)
    if success:
        if any(marker in message for marker in SIGNED):
            return 'signed', '网站确认签到成功'
        return 'accepted', '网站已接受签到请求，尚需核实新增额度'
    raise TaskError('checkin_failed', '网站未确认签到成功，请查看网站状态后重试')
