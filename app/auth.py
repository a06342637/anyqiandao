import asyncio
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import HTTPException, Request

COOKIE_NAME = 'any_assistant_session'


class Auth:
    def __init__(self, config, store):
        self.config = config
        self.store = store
        self.hasher = PasswordHasher()
        self.attempts = defaultdict(deque)
        self.hash_slots = asyncio.Semaphore(2)

    def origin(self, request):
        if request.headers.get('sec-fetch-site') == 'cross-site':
            raise HTTPException(403, '请求来源不匹配')
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != self.config.public_url:
            raise HTTPException(403, '请求来源不匹配')

    def csrf(self, token):
        return hmac.new(self.config.key, f'csrf:{token}'.encode(), hashlib.sha256).hexdigest()

    async def login(self, request: Request, password, username='admin'):
        self.origin(request)
        address = request.client.host if request.client else 'unknown'
        now = time.monotonic()
        if len(self.attempts) > 10000:
            self.attempts = defaultdict(deque, {key: attempts for key, attempts in self.attempts.items() if attempts and attempts[-1] > now - 900})
        attempts = self.attempts[address]
        while attempts and attempts[0] < now - 900:
            attempts.popleft()
        if len(attempts) >= 10:
            raise HTTPException(429, '尝试次数过多，请 15 分钟后再试')
        attempts.append(now)
        try:
            async with self.hash_slots:
                await asyncio.to_thread(self.hasher.verify, self.config.admin_hash, password)
        except VerificationError:
            raise HTTPException(401, '管理员账号或密码不正确') from None
        if not hmac.compare_digest(username.encode(), self.config.admin_username.encode()):
            raise HTTPException(401, '管理员账号或密码不正确')
        attempts.clear()
        token = secrets.token_urlsafe(32)
        self.store.execute('INSERT INTO sessions VALUES (?,?)', (hashlib.sha256(token.encode()).hexdigest(), time.time() + 86400))
        return token

    def session(self, request: Request):
        token = request.cookies.get(COOKIE_NAME, '')
        if not token or len(token) > 128:
            return None
        session = self.store.one('SELECT expires FROM sessions WHERE token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),))
        if not session or session['expires'] <= time.time():
            return None
        return token

    def require(self, request: Request):
        token = self.session(request)
        if not token:
            raise HTTPException(401, '请先登录管理页面')
        self.origin(request)
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if not hmac.compare_digest(request.headers.get('x-csrf-token', ''), self.csrf(token)):
                raise HTTPException(403, '安全校验失败，请刷新页面后重试')
        return token

    def logout(self, token):
        self.store.execute('DELETE FROM sessions WHERE token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),))
