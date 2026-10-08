"""Persistent backup schedule, snapshot creation and isolated remote retention."""
import asyncio
import base64
import json
import math
import shutil
import sqlite3
import tempfile
import time
import uuid
import zipfile
from contextlib import closing
from copy import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncssh

from app.backup_api import backup_document
from app.backup_targets import BackupError, blocking, open_target, safe_error
from app.config import ROOT, VERSION
from app.remote_backup_schema import RemoteBackupSettings
from app.update_common import source_files
from app.passwords import effective_admin_hash

CONFIG_KEY = 'remote_backup_config'
STATE_KEY = 'remote_backup_state'


def next_run(settings, now, anchor=None):
    zone = ZoneInfo(settings.timezone)
    local = datetime.fromtimestamp(anchor or now, zone)
    hour, minute = map(int, settings.time.split(':'))
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if settings.unit == 'hours':
        start = anchor or candidate.timestamp()
        step = settings.every * 3600
        return start + max(0, math.floor((now - start) / step) + 1) * step
    days = max(0, (datetime.fromtimestamp(now, zone).date() - candidate.date()).days // settings.every)
    candidate += timedelta(days=days * settings.every)
    while candidate.timestamp() <= now:
        candidate += timedelta(days=settings.every)
    return candidate.timestamp()


def create_archive(store, config, mode, directory, name):
    path = directory / name
    if shutil.disk_usage(directory).free < max(64 * 1024 * 1024, store.path.stat().st_size * 3):
        raise BackupError('本地剩余空间不足，未生成或上传备份')
    database = directory / 'snapshot.sqlite3'
    with store.connection() as source, closing(sqlite3.connect(database)) as snapshot:
        source.backup(snapshot)
    snapshot_store = copy(store)
    snapshot_store.path = database
    document = backup_document(snapshot_store)
    if mode == 'full':
        with closing(sqlite3.connect(database)) as snapshot:
            snapshot.execute('PRAGMA secure_delete=ON')
            for table in ('sessions', 'exports', 'console_results', 'jobs', 'logs'):
                snapshot.execute(f'DELETE FROM {table}')
            snapshot.execute("DELETE FROM meta WHERE key LIKE 'remote_backup_%' OR key IN ('maintenance', 'pause_reason')")
            snapshot.execute("INSERT OR REPLACE INTO meta VALUES ('queue_paused', '1')")
            snapshot.commit()
            snapshot.execute('VACUUM')
            if snapshot.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise BackupError('数据库一致性校验失败，未上传备份')
    body = json.dumps(document, ensure_ascii=False, separators=(',', ':')).encode()
    if len(body) > 64 * 1024 * 1024:
        raise BackupError('应用数据超过恢复接口的 64MB 限制，请使用服务器备份脚本')
    instructions = ('any签到助手远程备份\n\n应用数据恢复：在“设置 → 备份与恢复”选择此 ZIP，或解压后导入 backup.json。\n'
                    '含账号密码和 Cookie，请保存在私有存储中。远程存储凭证、运行日志和会话不包含在应用数据中。\n')
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('backup.json', body)
        if mode == 'full':
            archive.write(database, 'data/assistant.sqlite3')
            archive.writestr('secrets/app.key', base64.urlsafe_b64encode(config.key).decode() + '\n')
            archive.writestr('secrets/admin.hash', effective_admin_hash(snapshot_store, config) + '\n')
            # Only known deployment fields: no arbitrary environment variables or remote backup secrets.
            archive.writestr('.env', f'APP_PUBLIC_URL={config.public_url}\nAPP_ADMIN_USERNAME={config.admin_username}\n'
                             f'APP_IMAGE=any-signin-assistant:{VERSION}\nAPP_ALLOW_INSECURE_HTTP={int(not config.cookie_secure)}\n'
                             f'APP_TRUSTED_PROXY_IPS={",".join(config.trusted_proxy_ips) or "none"}\n')
            for file, relative in source_files(ROOT):
                archive.write(file, relative)
            instructions += ('\n完整恢复：先停止应用，将 ZIP 解压到独立目录。保留 data/、secrets/ 和 .env，'
                             '核对 .env 中访问地址、端口等部署参数，运行 bash scripts/deploy.sh 重建。'
                             '队列默认暂停，确认数据后再继续。完整包包含原加密密钥和管理员密码哈希；'
                             '不包含运行日志、临时会话、远程备份配置及宿主机自定义文件，恢复后重新配置远程备份。\n')
        archive.writestr('RESTORE.txt', instructions)
    return path


class RemoteBackup:
    def __init__(self, store, config):
        self.store, self.config = store, config
        self.task = None
        self.scheduler = None
        self.lock = asyncio.Lock()
        self.stopping = False
        self.instance = store.meta('remote_backup_instance')
        if not self.instance:
            self.instance = uuid.uuid4().hex[:12]
            store.set_meta('remote_backup_instance', self.instance)

    def settings(self):
        value = self.store.meta(CONFIG_KEY)
        return RemoteBackupSettings.model_validate(self.store.vault.open(value, CONFIG_KEY)) if value else RemoteBackupSettings()

    def state(self):
        return json.loads(self.store.meta(STATE_KEY, '{}'))

    def write_state(self, state):
        self.store.set_meta(STATE_KEY, json.dumps(state, ensure_ascii=False))

    def public(self):
        settings = self.settings().model_dump()
        for kind, fields in (('oss', ('access_key_secret',)), ('sftp', ('password', 'private_key', 'passphrase'))):
            for field in fields:
                settings[kind]['has_' + field] = bool(settings[kind].pop(field))
        trusted = self.store.meta('remote_backup_host_key')
        return {'settings': settings, 'state': self.state(), 'busy': self.lock.locked() or bool(self.task and not self.task.done()),
                'fingerprint': asyncssh.import_public_key(trusted).get_fingerprint() if trusted else ''}

    def log(self, message, level='info'):
        self.store.log('backup', message, level=level, category='backup')

    def require_idle(self):
        if self.lock.locked() or (self.task and not self.task.done()):
            raise BackupError('备份或连接操作正在执行，请稍后重试')

    def validate_target(self, kind, settings):
        if kind == 'oss' and not all((settings.bucket, settings.access_key_id, settings.access_key_secret)):
            raise BackupError('请完整填写 OSS Bucket、AccessKey ID 和 AccessKey Secret')
        if kind == 'sftp' and not all((settings.host, settings.username, settings.password if settings.auth == 'password' else settings.private_key)):
            raise BackupError('请完整填写 SSH 主机、用户名及登录密码或私钥')

    def save(self, incoming):
        self.require_idle()
        previous = self.settings()
        settings = incoming.model_copy(deep=True)
        changed_ssh = any(getattr(settings.sftp, key) != getattr(previous.sftp, key) for key in ('host', 'port', 'username'))
        if changed_ssh and (previous.sftp.password or previous.sftp.private_key) and not (incoming.sftp.password or incoming.sftp.private_key):
            raise BackupError('SSH 目标已更改，请重新输入密码或私钥，避免把旧凭证发送给新服务器')
        for kind, fields in (('oss', ('access_key_secret',)), ('sftp', ('password', 'private_key', 'passphrase'))):
            for field in fields:
                if not getattr(getattr(settings, kind), field):
                    setattr(getattr(settings, kind), field, getattr(getattr(previous, kind), field))
        for kind in ('oss', 'sftp'):
            target = getattr(settings, kind)
            if target.enabled:
                self.validate_target(kind, target)
        if settings.enabled and not (settings.oss.enabled or settings.sftp.enabled):
            raise BackupError('启用自动备份前，请至少启用一个备份目标')
        state = self.state()
        schedule_fields = ('enabled', 'every', 'unit', 'time', 'timezone')
        if any(getattr(settings, field) != getattr(previous, field) for field in schedule_fields) or not state.get('next_run'):
            state['next_run'] = next_run(settings, time.time()) if settings.enabled else None
        if (settings.sftp.host, settings.sftp.port) != (previous.sftp.host, previous.sftp.port):
            self.store.set_meta('remote_backup_host_key', '')
        for kind in ('oss', 'sftp'):
            if getattr(settings, kind) != getattr(previous, kind):
                state.get('targets', {}).pop(kind, None)
        self.store.set_meta(CONFIG_KEY, self.store.vault.seal(settings.model_dump(), CONFIG_KEY))
        self.write_state(state)
        self.log('远程自动备份设置已保存；' + ('定时备份已启用' if settings.enabled else '定时备份已关闭'))
        return self.public()

    def target(self, kind, settings):
        self.validate_target(kind, settings)
        return open_target(kind, settings, self.store.meta('remote_backup_host_key'),
                           lambda key: self.store.set_meta('remote_backup_host_key', key))

    async def inspect(self, kind, path=None):
        self.require_idle()
        async with self.lock:
            try:
                async with asyncio.timeout(120):
                    async with self.target(kind, getattr(self.settings(), kind)) as target:
                        result = await target.browse(path) if path is not None else await target.probe()
                if path is not None:
                    return result
                self.log(f'{"阿里云 OSS" if kind == "oss" else "SSH / SFTP"} 连接测试成功：已验证创建、列举和删除权限')
                return {'ok': True, 'message': '连接成功，已验证读写和删除权限', 'fingerprint': self.public()['fingerprint']}
            except Exception as error:
                message = safe_error(error)
                self.log(f'{"OSS" if kind == "oss" else "SFTP"} {"目录浏览" if path is not None else "连接测试"}失败：{message}', 'error')
                raise BackupError(message) from None

    def launch(self, source='manual'):
        self.require_idle()
        if self.stopping or self.store.meta('maintenance') == '1':
            raise BackupError('应用正在停止或更新，请稍后再备份')
        settings = self.settings()
        if not (settings.oss.enabled or settings.sftp.enabled):
            raise BackupError('请先保存并启用至少一个备份目标')
        state = self.state()
        state.update(running=True, started_at=time.time(), source=source, result='正在生成备份包')
        self.write_state(state)
        self.task = asyncio.create_task(self.run(settings, source), name='remote-backup')

    async def run(self, settings, source):
        async with self.lock:
            state = self.state()
            started = state['started_at']
            targets = state.setdefault('targets', {})
            results = []
            temporary = None
            try:
                self.log(f'{"定时" if source == "scheduled" else "手动"}远程备份开始：{"应用数据包" if settings.mode == "app" else "完整备份"}')
                temporary = tempfile.TemporaryDirectory(prefix='.backup-', dir=self.store.path.parent)
                name = f'any-signin-{self.instance}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}-{uuid.uuid4().hex[:8]}-{settings.mode}.zip'
                path = await blocking(create_archive, self.store, self.config, settings.mode, Path(temporary.name), name)
                size = path.stat().st_size
                for kind in ('oss', 'sftp'):
                    target_settings = getattr(settings, kind)
                    if not target_settings.enabled:
                        continue
                    begin = time.time()
                    label = '阿里云 OSS' if kind == 'oss' else 'SSH / SFTP'
                    result = {'at': begin, 'name': name, 'size': size, 'status': 'success'}
                    try:
                        async with asyncio.timeout(1800):
                            async with self.target(kind, target_settings) as target:
                                await target.upload(path, name)
                                result['message'] = f'已上传 {name}（{size / 1024:.1f} KB）'
                                try:
                                    removed = await target.prune(self.instance)
                                    result['message'] += f'，清理旧备份 {removed} 份'
                                except Exception as error:
                                    result.update(status='warning', message=result['message'] + '；旧备份清理失败：' + safe_error(error))
                    except Exception as error:
                        result.update(status='error', message=safe_error(error))
                    result['duration'] = round(time.time() - begin, 1)
                    targets[kind] = result
                    results.append(result['status'])
                    self.log(f'{label}：{result["message"]}，耗时 {result["duration"]} 秒', 'info' if result['status'] == 'success' else result['status'])
                    self.write_state(state)
                state['status'] = 'success' if all(item == 'success' for item in results) else 'error' if all(item == 'error' for item in results) else 'warning'
                state['result'] = {'success': '全部备份成功', 'error': '全部备份失败', 'warning': '备份完成，部分操作需要检查'}[state['status']]
            except asyncio.CancelledError:
                state.update(status='error', result='备份被服务停止中断，下次启动后检查备份计划')
                self.log(state['result'], 'warning')
                raise
            except Exception as error:
                state.update(status='error', result=safe_error(error))
                self.log('备份失败：' + state['result'], 'error')
            finally:
                if temporary:
                    await blocking(temporary.cleanup)
                state.update(running=False, last_run=started, finished_at=time.time())
                # Manual backups do not postpone the configured schedule.
                if source == 'scheduled':
                    state['next_run'] = next_run(settings, time.time(), state.get('next_run'))
                self.write_state(state)

    async def start(self):
        self.stopping = False
        state = self.state()
        if state.get('running'):
            state.update(running=False, status='error', result='上次备份因服务退出中断')
            self.log(state['result'], 'warning')
        settings = self.settings()
        if settings.enabled and not state.get('next_run'):
            state['next_run'] = next_run(settings, time.time())
        self.write_state(state)
        # Only leftovers from this service's private temporary directories.
        for path in self.store.path.parent.glob('.backup-*'):
            if path.is_dir() and not path.is_symlink():
                await blocking(shutil.rmtree, path)
        self.scheduler = asyncio.create_task(self.clock(), name='backup-scheduler')

    async def clock(self):
        while not self.stopping:
            try:
                settings, state = self.settings(), self.state()
                if settings.enabled and state.get('next_run') and state['next_run'] <= time.time() and not self.store.meta('maintenance') == '1':
                    if not self.lock.locked() and not (self.task and not self.task.done()):
                        self.launch('scheduled')
            except Exception as error:
                self.log('备份计划检查失败：' + safe_error(error), 'error')
            await asyncio.sleep(10)

    async def stop(self):
        self.stopping = True
        tasks = [task for task in (self.scheduler, self.task) if task and not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
