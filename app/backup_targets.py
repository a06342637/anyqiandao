"""Remote transports. Only this instance's completed ZIPs participate in retention."""
import asyncio
import posixpath
import re
import uuid
from contextlib import asynccontextmanager

import asyncssh
import oss2


class BackupError(RuntimeError):
    pass


def safe_error(error):
    # Never propagate provider responses: they can include credentials, URLs or request headers.
    if isinstance(error, BackupError):
        return str(error)
    if isinstance(error, asyncssh.HostKeyNotVerifiable):
        return 'SSH 主机指纹已变化，已拒绝连接；请核实服务器后重新配置信任'
    if isinstance(error, asyncssh.PermissionDenied):
        return 'SSH 身份验证失败，请检查用户名、密码或私钥'
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return '连接或传输超时，请检查网络后重试'
    if isinstance(error, oss2.exceptions.OssError):
        return f'OSS 请求失败（HTTP {error.status}），请检查地域、Endpoint、凭证及 Bucket 权限'
    if isinstance(error, asyncssh.SFTPError):
        return 'SFTP 操作失败，请检查目录、读写权限和服务器剩余空间'
    if isinstance(error, (OSError, asyncssh.Error)):
        return '远程连接或文件操作失败，请检查地址、网络、权限和剩余空间'
    if isinstance(error, (ValueError, asyncssh.KeyImportError)):
        return '配置格式不正确，请检查私钥和私钥口令'
    return '备份操作失败，请检查配置、网络及存储空间后重试'


async def blocking(function, *args, **kwargs):
    """Drain SDK/file work on cancellation before callers remove temporary files."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            pass
        raise


def owned_backup(name, instance):
    return bool(re.fullmatch(r'any-signin-' + re.escape(instance) + r'-\d{8}T\d{12}Z-[a-f0-9]{8}-(app|full)\.(zip|asb)', name))


class OSSTarget:
    def __init__(self, settings):
        self.settings = settings
        endpoint = settings.endpoint or f'oss-{settings.region}{"-internal" if settings.internal else ""}.aliyuncs.com'
        self.bucket = oss2.Bucket(oss2.AuthV4(settings.access_key_id, settings.access_key_secret),
                                 'https://' + endpoint, settings.bucket, region=settings.region, connect_timeout=25)

    async def probe(self):
        key = self.settings.prefix + '.any-signin-probe-' + uuid.uuid4().hex
        await blocking(self.bucket.put_object, key, b'connection test')
        try:
            await blocking(self.bucket.list_objects, prefix=self.settings.prefix, max_keys=1)
        finally:
            await blocking(self.bucket.delete_object, key)

    async def upload(self, path, name):
        await blocking(self.bucket.put_object_from_file, self.settings.prefix + name, str(path))

    async def prune(self, instance):
        def clean():
            prefix = self.settings.prefix
            objects = [item.key for item in oss2.ObjectIterator(self.bucket, prefix=prefix + 'any-signin-' + instance + '-')
                       if owned_backup(item.key[len(prefix):], instance)]
            expired = sorted(objects, reverse=True)[self.settings.keep:]
            for key in expired:
                self.bucket.delete_object(key)
            return len(expired)
        return await blocking(clean)

    async def browse(self, path):
        from app.remote_backup_schema import OSSSettings
        path = OSSSettings(prefix=path).prefix
        result = await blocking(self.bucket.list_objects, prefix=path, delimiter='/', max_keys=1000)
        return {'path': path, 'parent': posixpath.dirname(path.rstrip('/')),
                'directories': sorted(result.prefix_list), 'truncated': result.is_truncated}


class PinnedClient(asyncssh.SSHClient):
    def __init__(self, trusted):
        self.trusted = trusted
        self.key = None

    def validate_host_public_key(self, host, addr, port, key):
        self.key = key
        return not self.trusted or key.export_public_key().decode().strip() == self.trusted


class SFTPTarget:
    def __init__(self, client, settings):
        self.client, self.settings = client, settings

    async def probe(self):
        await self.client.makedirs(self.settings.directory, attrs=asyncssh.SFTPAttrs(permissions=0o700), exist_ok=True)
        path = posixpath.join(self.settings.directory, '.any-signin-probe-' + uuid.uuid4().hex)
        async with self.client.open(path, 'wb') as file:
            await file.write(b'connection test')
        try:
            await self.client.listdir(self.settings.directory)
        finally:
            await self.client.remove(path)

    async def upload(self, path, name):
        await self.client.makedirs(self.settings.directory, attrs=asyncssh.SFTPAttrs(permissions=0o700), exist_ok=True)
        final = posixpath.join(self.settings.directory, name)
        temporary = final + '.part'
        try:
            await self.client.put(str(path), temporary)
            await self.client.chmod(temporary, 0o600)
            await self.client.rename(temporary, final)
        finally:
            try:
                await self.client.remove(temporary)
            except asyncssh.SFTPNoSuchFile:
                pass

    async def prune(self, instance):
        names = []
        async for item in self.client.scandir(self.settings.directory):
            if item.attrs.type == asyncssh.FILEXFER_TYPE_REGULAR and owned_backup(item.filename, instance):
                names.append(item.filename)
        expired = sorted(names, reverse=True)[self.settings.keep:]
        for name in expired:
            await self.client.remove(posixpath.join(self.settings.directory, name))
        return len(expired)

    async def browse(self, path):
        from app.remote_backup_schema import SFTPSettings
        path = SFTPSettings(directory=path or self.settings.directory).directory
        directories, truncated = [], False
        async for item in self.client.scandir(path):
            if item.filename not in ('.', '..') and item.attrs.type == asyncssh.FILEXFER_TYPE_DIRECTORY:
                directories.append(posixpath.join(path, item.filename))
                if len(directories) >= 1000:
                    truncated = True
                    break
        return {'path': path, 'parent': posixpath.dirname(path), 'directories': sorted(directories), 'truncated': truncated}


@asynccontextmanager
async def open_target(kind, settings, trusted='', remember=None):
    if kind == 'oss':
        yield OSSTarget(settings)
        return
    verifier = PinnedClient(trusted)
    keys = [asyncssh.import_private_key(settings.private_key, passphrase=settings.passphrase or None)] if settings.auth == 'private_key' else []
    async with asyncssh.connect(settings.host, port=settings.port, username=settings.username,
                                password=settings.password if settings.auth == 'password' else None,
                                client_keys=keys, agent_path=None, config=None, known_hosts=(),
                                client_factory=lambda: verifier, connect_timeout=25, login_timeout=25,
                                keepalive_interval=15, keepalive_count_max=3) as connection:
        # Pin only after authentication succeeds, never from a failed login.
        if not trusted and remember and verifier.key:
            remember(verifier.key.export_public_key().decode().strip())
        async with connection.start_sftp_client() as client:
            yield SFTPTarget(client, settings)
