import asyncio
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import HTTPException, Request
from app.security import canonical_origin, request_origin
from app.passwords import ADMIN_HASH_CONTEXT, effective_admin_hash

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
        if origin and (not canonical_origin(origin) or canonical_origin(origin) != request_origin(request, self.config)):
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
        password_state = self.store.meta(ADMIN_HASH_CONTEXT)
        try:
            async with self.hash_slots:
                await asyncio.to_thread(self.hasher.verify, effective_admin_hash(self.store, self.config, password_state), password)
        except VerificationError:
            raise HTTPException(401, '管理员账号或密码不正确') from None
        if not hmac.compare_digest(username.encode(), self.config.admin_username.encode()):
            raise HTTPException(401, '管理员账号或密码不正确')
        token = secrets.token_urlsafe(32)
        with self.store.transaction() as connection:
            current = connection.execute('SELECT value FROM meta WHERE key=?', (ADMIN_HASH_CONTEXT,)).fetchone()
            if (current['value'] if current else '') != password_state:
                raise HTTPException(401, '管理员密码已更改，请使用新密码重新登录')
            connection.execute('INSERT INTO sessions VALUES (?,?)', (hashlib.sha256(token.encode()).hexdigest(), time.time() + 86400))
        attempts.clear()
        return token

    async def reset_password(self, password, token):
        async with self.hash_slots:
            hashed = await asyncio.to_thread(self.hasher.hash, password)
        sealed = self.store.vault.seal(hashed, ADMIN_HASH_CONTEXT)
        with self.store.transaction() as connection:
            session = connection.execute('SELECT expires FROM sessions WHERE token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            if not session or session['expires'] <= time.time():
                raise HTTPException(401, '登录已失效，请重新登录后设置密码')
            connection.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (ADMIN_HASH_CONTEXT, sealed))
            connection.execute('DELETE FROM sessions')
            connection.execute('INSERT INTO logs(kind,level,message,created,category) VALUES (?,?,?,?,?)',
                               ('system', 'info', '管理员密码已重置，所有旧登录会话已注销', time.time(), 'system'))
        self.attempts.clear()

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
