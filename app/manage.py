import argparse
import base64
import getpass
import os
import re
import secrets
import sqlite3
import sys
import time
import warnings
from pathlib import Path
from urllib.parse import urlsplit

from argon2 import PasswordHasher

from app.config import ROOT, VERSION, Config
from app.crypto import Vault
from app.db import Store
from app.passwords import ADMIN_HASH_CONTEXT, ADMIN_PASSWORD_MIN, ADMIN_PASSWORD_MAX


class PasswordInputError(RuntimeError):
    pass


def validate_admin_password(password):
    if not ADMIN_PASSWORD_MIN <= len(password) <= ADMIN_PASSWORD_MAX:
        raise PasswordInputError('管理员密码需要 5–1024 个字符，未修改密码')


def read_reset_password(args):
    if getattr(args, 'password_stdin', False):
        password = sys.stdin.read(ADMIN_PASSWORD_MAX + 1)
        validate_admin_password(password)
        return password
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            password = getpass.getpass('输入新的管理员密码（5–1024 字符，不回显）：')
            validate_admin_password(password)
            confirmation = getpass.getpass('再次输入新密码：')
    except (EOFError, getpass.GetPassWarning):
        raise PasswordInputError('未能安全读取密码，请在交互式终端运行重置脚本；密码未修改') from None
    if password != confirmation:
        raise PasswordInputError('两次输入不一致，密码未修改')
    return password


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
    if supplied_password:
        validate_admin_password(supplied_password)
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
    password = read_reset_password(args)
    path = Path(os.environ.get('APP_ADMIN_PASSWORD_HASH_FILE', str(ROOT / 'secrets' / 'admin.hash')))
    if path.is_symlink() or not path.is_file():
        raise RuntimeError('密码哈希文件缺失或为符号链接')
    metadata = path.stat()
    store = Store(config.data_dir, Vault(config.key))
    encoded = PasswordHasher().hash(password) + '\n'
    temporary = path.with_name('.admin-reset-' + secrets.token_hex(8))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        if os.name != 'nt':
            os.chown(temporary, metadata.st_uid, metadata.st_gid)
        # Validate/decrypt the database before changing the hash. A failed file
        # replacement rolls back session revocation and leaves the old file intact.
        with store.transaction() as connection:
            connection.execute('DELETE FROM sessions')
            connection.execute('DELETE FROM meta WHERE key=?', (ADMIN_HASH_CONTEXT,))
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    print('密码哈希已更新、旧会话已注销。请重新创建 app 容器加载新密码；主加密密钥未改变。')


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
    reset = commands.add_parser('reset-password')
    reset.add_argument('--password-stdin', action='store_true', help='通过标准输入接收密码，避免将密码写进命令行参数')
    reset.set_defaults(handler=reset_password)
    args = parser.parse_args()
    try:
        args.handler(args)
    except PasswordInputError as error:
        raise SystemExit(str(error)) from None
    except (EOFError, KeyboardInterrupt):
        raise SystemExit('输入已中止，请重新运行命令；不会显示密码。') from None
    except (RuntimeError, OSError, sqlite3.Error):
        raise SystemExit('操作未完成：请检查参数、原始配置、暂停状态、文件权限和可用空间；不会自动替换旧密钥。') from None


if __name__ == '__main__':
    main()
