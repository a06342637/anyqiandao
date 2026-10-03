import json
import os
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from app.config import VERSION
from app.schemas import StrictModel
from app.update_common import DEFAULT_REPOSITORY, TERMINAL, UpdateError, latest_release, read_json, repository_name, version_tuple


class UpdateSource(StrictModel):
    repository: str = Field(default='', max_length=240)


class UpdateInstall(StrictModel):
    version: str = Field(min_length=1, max_length=40)
    force: bool = False
    acknowledged: bool = False


def update_router(store, auth, config):
    router = APIRouter(prefix='/api/v1/updates', dependencies=[Depends(auth.require)])
    directory = config.update_dir or Path('/run/assistant-update')
    lock = threading.Lock()

    def repository():
        return store.meta('update_repository').strip() or os.environ.get('APP_UPDATE_REPOSITORY', '').strip() or DEFAULT_REPOSITORY

    def operation():
        state = read_json(directory / 'status' / 'state.json', {})
        state = state if isinstance(state, dict) else {}
        pending = read_json(directory / 'requests' / 'request.json', {})
        if isinstance(pending, dict) and pending.get('id') and state.get('id') != pending['id']:
            return {'id': pending['id'], 'stage': 'queued', 'message': '等待更新执行器接收请求', 'progress': 0, 'version': pending.get('version'), 'started_at': pending.get('requested_at')}
        return {key: state[key] for key in ('id', 'stage', 'message', 'progress', 'version', 'previous_version', 'started_at', 'finished_at', 'updated_at', 'history', 'backup_path') if key in state}

    def available():
        heartbeat = read_json(directory / 'status' / 'heartbeat.json', {})
        return isinstance(heartbeat, dict) and heartbeat.get('protocol') == 1 and time.time() - heartbeat.get('at', 0) < 35

    def require_idle():
        current = operation()
        if current.get('id') and current.get('stage') not in TERMINAL:
            raise HTTPException(409, '已有更新正在执行，请勿重复提交')

    def checked():
        try:
            cache = json.loads(store.meta('update_check', '{}'))
            if cache.get('release'):
                cache['update_available'] = version_tuple(cache['release']['version']) > version_tuple(VERSION)
            return cache
        except (ValueError, TypeError, KeyError, UpdateError):
            return {}

    @router.get('')
    def status():
        cache = checked()
        if cache.get('repository') != repository():
            cache = {}
        return {'current_version': VERSION, 'repository': repository(), 'updater_available': available(),
                'check': cache, 'operation': operation()}

    @router.put('/source')
    def source(payload: UpdateSource):
        with lock:
            require_idle()
            try:
                value = repository_name(payload.repository) or DEFAULT_REPOSITORY
            except UpdateError as error:
                raise HTTPException(400, str(error)) from None
            store.set_meta('update_repository', value)
            store.set_meta('update_check', '{}')
            store.log('system', f'已设置版本发布仓库：{value or "未配置"}')
        return {'repository': value}

    @router.post('/check')
    def check():
        with lock:
            require_idle()
            cache = checked()
            if cache.get('repository') == repository() and time.time() - cache.get('checked_at', 0) < 60:
                if cache.get('error'):
                    raise HTTPException(429, cache['error'] + '；请在上次检测一分钟后重试')
                return cache
            try:
                release = latest_release(repository())
                result = {'repository': repository(), 'checked_at': time.time(), 'release': release,
                          'update_available': version_tuple(release['version']) > version_tuple(VERSION), 'error': ''}
            except UpdateError as error:
                store.set_meta('update_check', json.dumps({'repository': repository(), 'checked_at': time.time(), 'error': str(error)}, ensure_ascii=False))
                raise HTTPException(400, str(error)) from None
            store.set_meta('update_check', json.dumps(result, ensure_ascii=False))
            store.log('system', f'版本检测完成：当前 {VERSION}，远程 {release["version"]}')
            return result

    @router.post('/install', status_code=202)
    def install(payload: UpdateInstall):
        with lock:
            require_idle()
            if not available():
                raise HTTPException(503, '服务器更新执行器未就绪，请运行 scripts/install-updater.sh 或检查其 systemd 服务')
            if not payload.acknowledged:
                raise HTTPException(400, '请确认更新将下载并运行所配置仓库的代码')
            cache = checked()
            release = cache.get('release', {})
            if cache.get('repository') != repository() or time.time() - cache.get('checked_at', 0) > 3600 or cache.get('error') or release.get('version') != payload.version:
                raise HTTPException(409, '版本检测已过期或更新源已变化，请重新检测')
            try:
                target, current = version_tuple(payload.version), version_tuple(VERSION)
            except UpdateError as error:
                raise HTTPException(400, str(error)) from None
            if target < current or target == current and not payload.force:
                raise HTTPException(409, '已经是当前版本；重装同版本需勾选强制更新，不允许在线降级')
            identifier = uuid.uuid4().hex
            request = {'id': identifier, 'repository': repository(), 'version': payload.version, 'force': payload.force, 'requested_at': time.time()}
            path = directory / 'requests' / 'request.json'
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                    json.dump(request, output, ensure_ascii=False)
                    output.flush()
                    os.fsync(output.fileno())
            except FileExistsError:
                raise HTTPException(409, '更新请求已提交，请等待处理') from None
            except OSError:
                raise HTTPException(503, '无法写入更新请求，请检查部署时的更新目录权限') from None
            store.log('system', f'管理员请求更新到 v{payload.version}' + ('（已确认强制覆盖源码）' if payload.force else ''))
            return {'id': identifier, 'stage': 'queued', 'version': payload.version}

    return router
