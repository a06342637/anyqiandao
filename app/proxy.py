import asyncio
import base64
import hmac
import re
import secrets
from urllib.parse import unquote, urlsplit

import asyncssh
from python_socks import ProxyType
from python_socks.async_.asyncio import Proxy as SocksProxy

from app.errors import HostKeyRequired, TaskError

ALLOWED_HOSTS = {'anyrouter.top', 'challenges.cloudflare.com', 'fonts.googleapis.com', 'fonts.gstatic.com', 'cdn.jsdelivr.net'}


def allowed_destination(host):
    normalized = host.rstrip('.').lower()
    return normalized in ALLOWED_HOSTS or normalized.endswith('.anyrouter.top')


def parse_proxy(line):
    text = line.strip()
    if not text:
        raise ValueError('代理不能为空')
    try:
        if '://' in text:
            parsed = urlsplit(text)
            scheme = {'s5': 'socks5', 'socks': 'socks5'}.get(parsed.scheme.lower(), parsed.scheme.lower())
            if scheme not in ('http', 'socks5', 'ssh'):
                raise ValueError('不支持的协议')
            if parsed.path not in ('', '/') or parsed.query or parsed.fragment:
                raise ValueError('URL 含路径或未编码特殊字符')
            default_port = {'http': 8080, 'socks5': 1080, 'ssh': 22}[scheme]
            result = {'scheme': scheme, 'host': parsed.hostname, 'port': parsed.port or default_port,
                      'username': unquote(parsed.username or ''), 'password': unquote(parsed.password or '')}
        else:
            if re.search(r'\s', text):
                fields = text.split(None, 3)
            elif text.startswith('['):
                matched = re.fullmatch(r'\[([^]]+)\]:(\d+)(?::([^:]*):(.*))?', text)
                if not matched:
                    raise ValueError('IPv6 格式错误')
                fields = [value or '' for value in matched.groups()]
            else:
                fields = text.split(':', 3)
            if len(fields) not in (2, 4):
                raise ValueError('必须是两字段或四字段')
            result = {'scheme': 'auto', 'host': fields[0], 'port': int(fields[1]),
                      'username': fields[2] if len(fields) == 4 else '', 'password': fields[3] if len(fields) == 4 else ''}
        if not result['host'] or not 1 <= result['port'] <= 65535:
            raise ValueError('地址或端口错误')
        if any(character.isspace() for character in result['host']) or any(character in result['host'] for character in '/@?#'):
            raise ValueError('地址错误')
        return result
    except (ValueError, TypeError):
        raise ValueError('代理格式无效，请使用协议 URL、IP:端口:用户名:密码，或空格分隔的四字段；URL 特殊字符需编码') from None


async def detect_protocol(config, timeout):
    host, port = config['host'], config['port']
    async def probe(payload, banner=False):
        writer = None
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
            if payload:
                writer.write(payload)
                await writer.drain()
            return await asyncio.wait_for(reader.read(512), min(timeout, 1.5 if banner else 4))
        except (OSError, asyncio.TimeoutError):
            return b''
        finally:
            if writer:
                writer.close()
                await writer.wait_closed()
    if (await probe(b'', banner=True)).startswith(b'SSH-'):
        return 'ssh'
    response = await probe(b'\x05\x02\x00\x02')
    if len(response) >= 2 and response[0] == 5 and response[1] in (0, 2, 255):
        return 'socks5'
    response = await probe(b'CONNECT anyrouter.top:443 HTTP/1.1\r\nHost: anyrouter.top:443\r\n\r\n')
    if response.startswith(b'HTTP/'):
        return 'http'
    raise TaskError('proxy_error', '无法识别协议或节点不可达，请指定协议后重试', retry_proxy=True)


class ProxyBridge:
    def __init__(self, config, timeout=10, trusted_key=None):
        self.config = config
        self.timeout = timeout
        self.trusted_key = trusted_key
        self.secret = secrets.token_urlsafe(24)
        self.server = None
        self.ssh = None
        self.handlers = set()
        self.writers = set()

    async def __aenter__(self):
        try:
            if self.config['scheme'] == 'ssh':
                key = await asyncio.wait_for(asyncssh.get_server_host_key(self.config['host'], self.config['port'], config=None), self.timeout)
                if key is None:
                    raise TaskError('proxy_error', '无法读取 SSH 主机指纹', retry_proxy=True)
                exported = key.export_public_key().decode().strip()
                if not self.trusted_key or asyncssh.import_public_key(self.trusted_key).get_fingerprint() != key.get_fingerprint():
                    raise HostKeyRequired(exported, key.get_fingerprint(), bool(self.trusted_key))
                if not self.config.get('username') or not self.config.get('password'):
                    raise TaskError('proxy_error', 'SSH 代理需要用户名和密码', retry_proxy=True)
                self.ssh = await asyncssh.connect(self.config['host'], self.config['port'],
                    username=self.config['username'], password=self.config['password'],
                    known_hosts=([asyncssh.import_public_key(self.trusted_key)], [], []),
                    client_keys=[], agent_path=None, config=None, preferred_auth=['password'],
                    connect_timeout=self.timeout, login_timeout=self.timeout)
            self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0, limit=32768)
            self.port = self.server.sockets[0].getsockname()[1]
            return self
        except (HostKeyRequired, TaskError):
            await self.close()
            raise
        except (OSError, asyncssh.Error, asyncio.TimeoutError):
            await self.close()
            raise TaskError('proxy_error', '代理连接或 SSH 认证失败', retry_proxy=True) from None

    @property
    def url(self):
        return f'http://bridge:{self.secret}@127.0.0.1:{self.port}'

    @property
    def browser_proxy(self):
        return {'server': f'http://127.0.0.1:{self.port}', 'username': 'bridge', 'password': self.secret}

    async def connect(self, host, port):
        if self.ssh:
            return await asyncio.wait_for(self.ssh.open_connection(host, port), self.timeout)
        proxy = SocksProxy(proxy_type=ProxyType.SOCKS5 if self.config['scheme'] == 'socks5' else ProxyType.HTTP,
                           host=self.config['host'], port=self.config['port'], username=self.config.get('username') or None,
                           password=self.config.get('password') or None, rdns=True)
        socket = await proxy.connect(dest_host=host, dest_port=port, timeout=self.timeout)
        try:
            return await asyncio.open_connection(sock=socket)
        except BaseException:
            socket.close()
            raise

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        self.writers.add(writer)
        remote_writer = None
        established = False
        try:
            header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), self.timeout)
            lines = header.decode('latin-1').split('\r\n')
            method, authority, protocol = lines[0].split(' ', 2)
            headers = dict(line.split(':', 1) for line in lines[1:] if ':' in line)
            supplied = next((value.strip() for name, value in headers.items() if name.lower() == 'proxy-authorization'), '')
            expected = 'Basic ' + base64.b64encode(f'bridge:{self.secret}'.encode()).decode()
            if not hmac.compare_digest(supplied, expected):
                writer.write(b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="local"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                await writer.drain()
                return
            target = urlsplit('//' + authority)
            if method != 'CONNECT' or target.port != 443 or not target.hostname or not allowed_destination(target.hostname):
                writer.write(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
                await writer.drain()
                return
            remote_reader, remote_writer = await self.connect(target.hostname, target.port)
            self.writers.add(remote_writer)
            writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            await writer.drain()
            established = True
            async def relay(source, destination):
                while chunk := await source.read(65536):
                    destination.write(chunk)
                    await destination.drain()
            relays = [asyncio.create_task(relay(reader, remote_writer)), asyncio.create_task(relay(remote_reader, writer))]
            try:
                await asyncio.wait(relays, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for relay_task in relays:
                    relay_task.cancel()
                await asyncio.gather(*relays, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            if not established and not writer.is_closing():
                writer.write(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n')
                try:
                    await writer.drain()
                except OSError:
                    pass
        finally:
            for output in (writer, remote_writer):
                if output:
                    self.writers.discard(output)
                    output.close()
            self.handlers.discard(task)

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for writer in list(self.writers):
            writer.close()
        tasks = list(self.handlers)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.ssh:
            self.ssh.close()
            await self.ssh.wait_closed()

    async def __aexit__(self, *args):
        await self.close()
