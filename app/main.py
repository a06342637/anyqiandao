from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api import account_router
from app.backup_api import RESTORE_LIMIT, backup_router
from app.branding import favicon_svg, public_branding
from app.auth import Auth, COOKIE_NAME
from app.config import APP_NAME, ROOT, VERSION, Config
from app.crypto import Vault
from app.db import SCHEMA_VERSION, Store
from app.engine import Engine
from app.schemas import LoginInput, AdminPasswordInput
from app.settings_api import settings_router
from app.history_api import history_router
from app.credential_api import credential_router
from app.console_api import console_router
from app.update_api import update_router
from app.remote_backup import RemoteBackup
from app.remote_backup_api import remote_backup_router
from app.security import secure_transport, local_health_probe, uses_https
from app.login_page import login_page, login_script


class BodyLimit:
    def __init__(self, app, limit=1048576):
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] not in ('POST', 'PUT', 'PATCH', 'DELETE'):
            return await self.app(scope, receive, send)
        limit = RESTORE_LIMIT if scope.get('path') == '/api/v1/backup/restore' else self.limit
        chunks, total = [], 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            total += len(chunk)
            if total > limit:
                response = JSONResponse({'detail': '单次请求超过 1MB，请使用分块导入' if limit == self.limit else '备份文件超过 64MB'}, status_code=413)
                return await response(scope, receive, send)
            chunks.append(chunk)
            if not message.get('more_body', False):
                break
        delivered = False
        async def buffered_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': b''.join(chunks), 'more_body': False}
            return await receive()
        await self.app(scope, buffered_receive, send)


def create_app(config=None, service=None):
    config = config or Config.from_env()
    store = Store(config.data_dir, Vault(config.key))
    auth = Auth(config, store)
    engine = Engine(store, service)
    remote_backup = RemoteBackup(store, config)

    @asynccontextmanager
    async def lifespan(application):
        if config.start_worker:
            await engine.start()
        try:
            if config.start_worker:
                await remote_backup.start()
            yield
        finally:
            await remote_backup.stop()
            if config.start_worker:
                await engine.stop()

    app = FastAPI(title=APP_NAME, version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.engine, app.state.auth = store, engine, auth
    app.state.remote_backup = remote_backup
    app.add_middleware(BodyLimit)

    @app.middleware('http')
    async def security_headers(request: Request, call_next):
        path = request.url.path
        private = path.startswith(('/api/', '/assets/')) and path != '/api/v1/auth/login'
        if private and not auth.session(request):
            response = JSONResponse({'detail': '请先登录管理页面'}, status_code=401)
        elif (private or path == '/api/v1/auth/login') and not secure_transport(request, config):
            response = JSONResponse({'detail': '请使用 HTTPS 或本机 SSH 隧道；禁止通过公网 HTTP 传输账号和密钥'}, status_code=426)
        elif private and request.method not in ('GET', 'HEAD', 'OPTIONS') and not path.startswith('/api/v1/auth/') and store.meta('maintenance') == '1':
            response = JSONResponse({'detail': '正在更新版本并保护数据，请等待健康检查结束后重试'}, status_code=503, headers={'Retry-After': '5'})
        else:
            response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Vary'] = 'Cookie'
        response.headers['X-Robots-Tag'] = 'noindex, nofollow'
        response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        if uses_https(request, config):
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return JSONResponse({'detail': '输入格式不正确，请检查字段或数据长度',
                             'errors': [{'field': '.'.join(map(str, item['loc'])), 'message': item['msg']} for item in error.errors()]}, status_code=422)

    @app.get('/healthz')
    def health(request: Request):
        if engine.storage_error:
            return JSONResponse({'status': 'storage_error'}, status_code=503)
        if local_health_probe(request) or auth.session(request):
            return {'status': 'ok', 'version': VERSION, 'schema_version': SCHEMA_VERSION}
        return {'status': 'ok'}

    @app.get('/login')
    def login_view(request: Request):
        return RedirectResponse('/') if auth.session(request) and secure_transport(request, config) else login_page(secure_transport(request, config))

    @app.get('/login.js')
    def login_js():
        return login_script()

    @app.post('/api/v1/auth/login')
    async def login(request: Request, payload: LoginInput):
        token = await auth.login(request, payload.password, payload.username)
        response = JSONResponse({'csrf_token': auth.csrf(token)})
        response.set_cookie(COOKIE_NAME, token, httponly=True, secure=config.cookie_secure or uses_https(request, config), samesite='strict', max_age=86400, path='/')
        return response

    @app.get('/api/v1/auth/me')
    def me(token=Depends(auth.require)):
        return {'csrf_token': auth.csrf(token), 'name': store.settings().site_name, 'version': VERSION, 'username': config.admin_username}

    @app.get('/api/v1/branding')
    def branding(token=Depends(auth.require)):
        return public_branding(store)

    @app.get('/favicon.svg')
    def favicon(request: Request):
        text = store.settings().site_icon_text if auth.session(request) and secure_transport(request, config) else '·'
        return Response(favicon_svg(text), media_type='image/svg+xml')

    @app.post('/api/v1/auth/logout')
    def logout(request: Request, token=Depends(auth.require)):
        auth.logout(token)
        response = JSONResponse({'ok': True})
        response.delete_cookie(COOKIE_NAME, path='/', secure=config.cookie_secure or uses_https(request, config), httponly=True, samesite='strict')
        response.headers['Clear-Site-Data'] = '"cache", "storage"'
        return response

    @app.put('/api/v1/settings/admin-password')
    async def reset_admin_password(request: Request, payload: AdminPasswordInput, token=Depends(auth.require)):
        await auth.reset_password(payload.new_password, token)
        response = JSONResponse({'ok': True, 'message': '管理员密码已更新，请使用新密码重新登录'})
        response.delete_cookie(COOKIE_NAME, path='/', secure=config.cookie_secure or uses_https(request, config), httponly=True, samesite='strict')
        response.headers['Clear-Site-Data'] = '"cache", "storage"'
        return response

    app.include_router(account_router(store, auth, engine))
    app.include_router(settings_router(store, auth))
    app.include_router(history_router(store, auth, engine))
    app.include_router(credential_router(store, auth))
    app.include_router(console_router(store, auth))
    app.include_router(backup_router(store, auth))
    app.include_router(remote_backup_router(remote_backup, auth))
    app.include_router(update_router(store, auth, config))
    assets = ROOT / 'frontend' / 'dist' / 'assets'
    if assets.exists():
        app.mount('/assets', StaticFiles(directory=assets), name='assets')

    @app.get('/{path:path}')
    def frontend(path: str, request: Request):
        if path.startswith('api/'):
            return JSONResponse({'detail': '接口不存在'}, status_code=404)
        if not auth.session(request):
            return login_page(secure_transport(request, config))
        if not secure_transport(request, config):
            return login_page(False)
        index = ROOT / 'frontend' / 'dist' / 'index.html'
        if not index.exists():
            return JSONResponse({'detail': '后端已启动，请先构建 frontend 前端资源'}, status_code=503)
        return FileResponse(index)

    return app
