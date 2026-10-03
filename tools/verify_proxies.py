"""End-to-end check of the three proxy chains using local synthetic servers.

Starts an authenticated HTTP CONNECT proxy, an authenticated SOCKS5 server and an
asyncssh server that permits TCP forwarding, all on 127.0.0.1, then drives the real
ProxyBridge through each of them to https://anyrouter.top/login. Also checks that
protocol detection, SSH host-key pinning, wrong credentials, destination allow-list
and bridge authentication behave as designed. No account credentials are involved.
"""
import asyncio
import base64
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncssh
import httpx

from app.config import TARGET_ORIGIN
from app.errors import HostKeyRequired, TaskError
from app.proxy import ProxyBridge, detect_protocol

checks = []
HTTP_AUTH = ('httpuser', 'http-pass')
SOCKS_AUTH = ('socksuser', 'socks-pass')
SSH_AUTH = ('sshuser', 'ssh-pass')


def check(condition, label):
    assert condition, label
    checks.append(label)
    print('  ok:', label, flush=True)


async def relay(reader, writer):
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    except (OSError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()


async def pipe(client_reader, client_writer, remote_reader, remote_writer):
    await asyncio.gather(relay(client_reader, remote_writer), relay(remote_reader, client_writer), return_exceptions=True)


async def http_proxy(reader, writer):
    try:
        header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 10)
        lines = header.decode('latin-1').split('\r\n')
        method, target, _ = lines[0].split(' ', 2)
        headers = {name.strip().lower(): value.strip() for name, value in (line.split(':', 1) for line in lines[1:] if ':' in line)}
        expected = 'Basic ' + base64.b64encode(f'{HTTP_AUTH[0]}:{HTTP_AUTH[1]}'.encode()).decode()
        if headers.get('proxy-authorization') != expected:
            writer.write(b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="test"\r\nContent-Length: 0\r\n\r\n')
            await writer.drain()
            writer.close()
            return
        if method != 'CONNECT':
            writer.write(b'HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n')
            await writer.drain()
            writer.close()
            return
        host, port = target.rsplit(':', 1)
        remote_reader, remote_writer = await asyncio.open_connection(host, int(port))
        writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
        await writer.drain()
        await pipe(reader, writer, remote_reader, remote_writer)
    except Exception:
        writer.close()


async def socks5_proxy(reader, writer):
    try:
        version, count = await reader.readexactly(2)
        methods = await reader.readexactly(count)
        if version != 5 or 2 not in methods:
            writer.write(b'\x05\xff')
            await writer.drain()
            writer.close()
            return
        writer.write(b'\x05\x02')
        await writer.drain()
        await reader.readexactly(1)
        username = await reader.readexactly((await reader.readexactly(1))[0])
        password = await reader.readexactly((await reader.readexactly(1))[0])
        if (username.decode(), password.decode()) != SOCKS_AUTH:
            writer.write(b'\x01\x01')
            await writer.drain()
            writer.close()
            return
        writer.write(b'\x01\x00')
        await writer.drain()
        _, command, _, address_type = await reader.readexactly(4)
        if address_type == 1:
            host = socket.inet_ntoa(await reader.readexactly(4))
        elif address_type == 3:
            host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
        else:
            host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
        port = int.from_bytes(await reader.readexactly(2), 'big')
        if command != 1:
            writer.write(b'\x05\x07\x00\x01' + bytes(6))
            await writer.drain()
            writer.close()
            return
        remote_reader, remote_writer = await asyncio.open_connection(host, port)
        writer.write(b'\x05\x00\x00\x01' + bytes(6))
        await writer.drain()
        await pipe(reader, writer, remote_reader, remote_writer)
    except Exception:
        writer.close()


class ForwardOnlyServer(asyncssh.SSHServer):
    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        return (username, password) == SSH_AUTH

    def connection_requested(self, dest_host, dest_port, orig_host, orig_port):
        return True


async def fetch_through(route, url=TARGET_ORIGIN + '/login', proxy_url=None):
    async with httpx.AsyncClient(proxy=proxy_url or route.url, timeout=25, trust_env=False) as client:
        async with client.stream('GET', url) as response:
            return response.status_code


async def expect_task_error(coroutine, code, retry):
    try:
        await coroutine
    except TaskError as error:
        return error.code == code and error.retry_proxy == retry and not isinstance(error, HostKeyRequired)
    return False


async def main():
    http_server = await asyncio.start_server(http_proxy, '127.0.0.1', 0)
    socks_server = await asyncio.start_server(socks5_proxy, '127.0.0.1', 0)
    host_key = asyncssh.generate_private_key('ssh-ed25519')
    ssh_server = await asyncssh.listen('127.0.0.1', 0, server_host_keys=[host_key], server_factory=ForwardOnlyServer)
    http_port = http_server.sockets[0].getsockname()[1]
    socks_port = socks_server.sockets[0].getsockname()[1]
    ssh_port = ssh_server.sockets[0].getsockname()[1]
    print(f'local servers: http={http_port} socks5={socks_port} ssh={ssh_port}', flush=True)
    try:
        http_config = {'scheme': 'http', 'host': '127.0.0.1', 'port': http_port, 'username': HTTP_AUTH[0], 'password': HTTP_AUTH[1]}
        socks_config = {'scheme': 'socks5', 'host': '127.0.0.1', 'port': socks_port, 'username': SOCKS_AUTH[0], 'password': SOCKS_AUTH[1]}
        ssh_config = {'scheme': 'ssh', 'host': '127.0.0.1', 'port': ssh_port, 'username': SSH_AUTH[0], 'password': SSH_AUTH[1]}

        print('protocol detection', flush=True)
        check(await detect_protocol({'host': '127.0.0.1', 'port': http_port}, 5) == 'http', 'auto-detects HTTP proxy without sending credentials')
        check(await detect_protocol({'host': '127.0.0.1', 'port': socks_port}, 5) == 'socks5', 'auto-detects SOCKS5 proxy without sending credentials')
        check(await detect_protocol({'host': '127.0.0.1', 'port': ssh_port}, 5) == 'ssh', 'auto-detects SSH banner')
        idle = await asyncio.start_server(lambda r, w: w.close(), '127.0.0.1', 0)
        idle_port = idle.sockets[0].getsockname()[1]
        check(await expect_task_error(detect_protocol({'host': '127.0.0.1', 'port': idle_port}, 3), 'proxy_error', True), 'unknown protocol is reported, not guessed')
        idle.close()

        print('HTTP chain', flush=True)
        async with ProxyBridge(http_config, 10) as route:
            status = await fetch_through(route)
            check(status in range(100, 600), f'HTTP proxy with Basic auth reaches anyrouter.top (HTTP {status})')
            check(route.url.startswith('http://bridge:') and '127.0.0.1' in route.url, 'bridge listens on loopback only')
            wrong = route.url.replace(route.secret, 'wrong-secret')
            try:
                await fetch_through(route, proxy_url=wrong)
                rejected = False
            except httpx.ProxyError:
                rejected = True
            check(rejected, 'bridge rejects requests without the per-run bridge secret')
            try:
                await fetch_through(route, url='https://example.com/')
                blocked = False
            except httpx.ProxyError:
                blocked = True
            check(blocked, 'bridge refuses destinations outside the allow-list')
        bad_http = dict(http_config, password='nope')
        async with ProxyBridge(bad_http, 10) as route:
            try:
                await fetch_through(route)
                failed = False
            except httpx.HTTPError:
                failed = True
            check(failed, 'wrong HTTP proxy password fails instead of silently falling back to direct')

        print('SOCKS5 chain', flush=True)
        async with ProxyBridge(socks_config, 10) as route:
            status = await fetch_through(route)
            check(status in range(100, 600), f'SOCKS5 proxy with username/password reaches anyrouter.top (HTTP {status})')
        async with ProxyBridge(dict(socks_config, password='nope'), 10) as route:
            try:
                await fetch_through(route)
                failed = False
            except httpx.HTTPError:
                failed = True
            check(failed, 'wrong SOCKS5 password fails instead of falling back to direct')

        print('SSH chain', flush=True)
        try:
            async with ProxyBridge(ssh_config, 10, None):
                pass
            check(False, 'unreachable')
        except HostKeyRequired as error:
            fingerprint = error.fingerprint
            trusted = error.public_key
            check(fingerprint.startswith('SHA256:') and trusted.startswith('ssh-ed25519 '), f'first SSH use stops for fingerprint confirmation ({fingerprint[:20]}…)')
            check(fingerprint == host_key.convert_to_public().get_fingerprint(), 'reported fingerprint matches the real host key')
        async with ProxyBridge(ssh_config, 10, trusted) as route:
            status = await fetch_through(route)
            check(status in range(100, 600), f'SSH TCP forwarding with pinned host key reaches anyrouter.top (HTTP {status})')
            check(route.ssh is not None, 'SSH session established without executing remote commands')
        other_key = asyncssh.generate_private_key('ssh-ed25519').convert_to_public().export_public_key().decode().strip()
        try:
            async with ProxyBridge(ssh_config, 10, other_key):
                pass
            check(False, 'unreachable')
        except HostKeyRequired as error:
            check('变化' in error.message, 'changed host key is refused before any password is sent')
        check(await expect_task_error(ProxyBridge(dict(ssh_config, password='nope'), 10, trusted).__aenter__(), 'proxy_error', True), 'wrong SSH password is a retryable proxy error')
    finally:
        http_server.close()
        socks_server.close()
        ssh_server.close()
        await asyncio.gather(http_server.wait_closed(), socks_server.wait_closed(), return_exceptions=True)
    print(f'PASS: {len(checks)} proxy-chain checks (HTTP, SOCKS5, SSH forwarding, host-key pinning, allow-list).')


asyncio.run(main())
