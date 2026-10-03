import argparse
import ipaddress
import re
import socket
import subprocess
import sys
import urllib.request
from urllib.parse import urlsplit


def public_url(value):
    if not value:
        return None
    if any(character.isspace() for character in value):
        raise ValueError('访问地址不能包含空白或换行')
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}', parsed.hostname)):
        raise ValueError('请提供不带路径的 HTTP / HTTPS 地址')
    port = parsed.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError('访问地址中的端口不合法')
    if parsed.scheme == 'https' and port not in (None, 443):
        raise ValueError('内置 HTTPS 代理使用 443 端口；自定义反代请先按 IP 部署')
    return parsed


def select_port(value, url=''):
    parsed = public_url(url)
    if value and (not value.isascii() or not value.isdecimal()):
        raise ValueError('端口应为 1–65535 的整数，或回车随机选择')
    requested = int(value) if value else 0
    if value and not 1 <= requested <= 65535:
        raise ValueError('端口应为 1–65535 的整数')
    if parsed and parsed.scheme == 'http':
        exposed = parsed.port or 80
        if requested and requested != exposed:
            raise ValueError('应用端口必须与 HTTP 访问地址中的端口一致')
        requested = exposed
    if parsed and parsed.scheme == 'https' and requested in (80, 443):
        raise ValueError('HTTPS 代理需要 80/443，应用请选择其他端口')
    with socket.socket() as listener:
        listener.bind(('0.0.0.0', requested))
        return listener.getsockname()[1]


def username(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}', value):
        raise ValueError('用户名需要 1–64 位字母、数字或 _.@-，以字母或数字开头')
    return value


def server_address(value):
    if not value:
        try:
            with urllib.request.urlopen('https://api.ipify.org', timeout=5) as response:
                value = str(ipaddress.IPv4Address(response.read(100).decode().strip()))
        except (OSError, ValueError):
            addresses = subprocess.check_output(['hostname', '-I'], text=True, timeout=5).split()
            value = next((address for address in addresses if '.' in address), '')
            print('公网 IP 探测失败，使用本机 IPv4；如地址不正确，请设置 APP_INSTALL_HOST。', file=sys.stderr)
    return str(ipaddress.IPv4Address(value))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('port', 'username', 'host', 'url'))
    parser.add_argument('value')
    parser.add_argument('url', nargs='?', default='')
    args = parser.parse_args()
    try:
        if args.action == 'port':
            print(select_port(args.value, args.url))
        elif args.action == 'username':
            print(username(args.value))
        elif args.action == 'host':
            print(server_address(args.value))
        else:
            public_url(args.value)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(f'配置无效或端口已被占用：{error}') from None


if __name__ == '__main__':
    main()
