import argparse
import base64
import getpass
import os
import re
import secrets
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from argon2 import PasswordHasher

from app.config import ROOT, VERSION, Config
from app.crypto import Vault
from app.db import Store


def private_file(path, content, *, owner=None):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as output:
        output.write(content)
    if owner is not None and os.name != 'nt':
        os.chown(path, owner, owner)


def initialize(args):
    root = args.root.resolve()
    parsed = urlsplit(args.public_url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        raise RuntimeError('请提供不带路径的完整站点地址，例如 https://signin.example.com')
    if parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1') and not args.allow_http:
        raise RuntimeError('HTTP IP 部署必须显式传入 --allow-http；公网建议使用反代 HTTPS')
    if not 1 <= args.port <= 65535 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}', args.username):
        raise RuntimeError('端口或管理员用户名不合法')
    supplied_password = sys.stdin.read(4097) if args.password_stdin else ''
    if supplied_password and not 12 <= len(supplied_password) <= 1024:
        raise RuntimeError('自定义管理密码必须为 12–1024 个字符，或留空使用随机强密码')
    for path in (root / 'secrets' / 'app.key', root / 'secrets' / 'admin.hash', root / 'data' / 'assistant.sqlite3', root / '.env', root / '部署信息.txt'):
        if path.exists():
            raise RuntimeError('检测到已有配置或数据库，拒绝重新初始化；请恢复匹配密钥或使用升级流程')
    root.mkdir(parents=True, exist_ok=True)
    key_dir = root / 'secrets'
    data_dir = root / 'data'
    for directory in (key_dir, data_dir):
        directory.mkdir(mode=0o700, exist_ok=True)
        if args.owner is not None and os.name != 'nt':
            os.chown(directory, args.owner, args.owner)
    password = supplied_password or secrets.token_urlsafe(24)
    private_file(key_dir / 'app.key', base64.urlsafe_b64encode(secrets.token_bytes(32)).decode() + '\n', owner=args.owner)
    private_file(key_dir / 'admin.hash', PasswordHasher().hash(password) + '\n', owner=args.owner)
    public_url = args.public_url.rstrip('/')
    private_file(root / '.env', f'APP_PUBLIC_URL={public_url}\nAPP_DOMAIN={parsed.hostname}\nAPP_PORT={args.port}\nAPP_IMAGE=any-signin-assistant:{VERSION}\n'
                 f'APP_BIND_HOST={args.bind_host}\nAPP_ADMIN_USERNAME={args.username}\nAPP_ALLOW_INSECURE_HTTP={int(args.allow_http)}\n'
                 f'APP_ENABLE_HTTPS_PROXY={int(args.https_proxy)}\n')
    private_file(root / '部署信息.txt',
        f'any签到助手 v{VERSION}\n\n访问地址：{public_url}\n管理员账号：{args.username}\n管理员密码：{password}\n'
        f'后端监听：{args.bind_host}:{args.port}\n'
        'HTTP 不加密。IP:端口部署请自行配置防火墙与 HTTPS 反代；需要外部反代时请同步修改 APP_PUBLIC_URL。\n\n'
        '请妥善保管本文件，不要上传 GitHub、网盘公共目录或发给其他人。\n'
        '密钥独立保存在 secrets/app.key；必须连同数据库、admin.hash 和 .env 一起离线备份。\n'
        '服务器登录密码不保存在本项目中。使用方法见 使用说明.txt。\n')
    print('初始化完成。管理员密码仅写入 部署信息.txt，未打印到日志。请妥善备份 secrets 目录。')


def backup(args):
    config = Config.from_env()
    store = Store(config.data_dir, Vault(config.key))
    if store.meta('queue_paused') != '1' or store.one("SELECT id FROM jobs WHERE status='running' LIMIT 1"):
        raise RuntimeError('请先暂停队列并等待当前任务结束，再进行备份')
    destination = args.output.resolve()
    if destination.exists():
        raise RuntimeError('备份目标已存在，拒绝覆盖')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with store.connection() as source:
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('备份数据库完整性检查失败')
        finally:
            target.close()
    os.chmod(destination, 0o600)
    print(f'一致性备份完成：{destination}。还必须独立备份原始 secrets 与 .env。')


def pause(args):
    config = Config.from_env()
    store = Store(config.data_dir, Vault(config.key))
    store.set_meta('queue_paused', '1')
    store.set_meta('pause_reason', '维护操作已暂停队列')
    print('已暂停新任务。当前任务不会被强行中止；请等待完成后备份。')


def reset_password(args):
    config = Config.from_env()
    password = getpass.getpass('输入新的管理员密码（至少 16 字符，不回显）：')
    if len(password) < 16 or password != getpass.getpass('再次输入新密码：'):
        raise RuntimeError('密码太短或两次输入不一致，未修改')
    path = Path(os.environ.get('APP_ADMIN_PASSWORD_HASH_FILE', str(ROOT / 'secrets' / 'admin.hash')))
    with path.open('w', encoding='utf-8') as output:
        output.write(PasswordHasher().hash(password) + '\n')
    Store(config.data_dir, Vault(config.key)).execute('DELETE FROM sessions')
    print('密码哈希已更新、旧会话已注销。请重启本项目使新密码生效，密钥未改变。')


def main():
    parser = argparse.ArgumentParser(prog='python -m app.manage')
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init')
    init.add_argument('--root', type=Path, default=ROOT)
    init.add_argument('--public-url', default='http://127.0.0.1:8000')
    init.add_argument('--port', type=int, default=18780)
    init.add_argument('--owner', type=int)
    init.add_argument('--username', default='admin')
    init.add_argument('--password-stdin', action='store_true')
    init.add_argument('--allow-http', action='store_true')
    init.add_argument('--https-proxy', action='store_true')
    init.add_argument('--bind-host', choices=['127.0.0.1', '0.0.0.0'], default='127.0.0.1')
    init.set_defaults(handler=initialize)
    backup_parser = commands.add_parser('backup')
    backup_parser.add_argument('--output', type=Path, required=True)
    backup_parser.set_defaults(handler=backup)
    commands.add_parser('pause').set_defaults(handler=pause)
    commands.add_parser('reset-password').set_defaults(handler=reset_password)
    args = parser.parse_args()
    try:
        args.handler(args)
    except (RuntimeError, OSError, sqlite3.Error):
        raise SystemExit('操作未完成：请检查参数、原始配置、暂停状态、文件权限和可用空间；不会自动替换旧密钥。') from None


if __name__ == '__main__':
    main()
