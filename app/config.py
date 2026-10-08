import base64
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from argon2 import extract_parameters
from dotenv import load_dotenv
from app.security import DEFAULT_TRUSTED_PROXIES, proxy_sources

ROOT = Path(__file__).resolve().parent.parent
VERSION = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
APP_NAME = 'any签到助手'
TARGET_ORIGIN = 'https://anyrouter.top'


def secret_value(name, default_file):
    if os.environ.get(name):
        return os.environ[name]
    path = Path(os.environ.get(name + '_FILE', str(ROOT / 'secrets' / default_file)))
    try:
        return path.read_text(encoding='utf-8').strip()
    except OSError:
        raise RuntimeError('密钥或管理员密码文件缺失；请恢复原文件，不要重新初始化已有数据') from None


@dataclass(frozen=True)
class Config:
    key: bytes
    admin_hash: str
    data_dir: Path
    public_url: str = 'http://127.0.0.1:8000'
    cookie_secure: bool = False
    start_worker: bool = True
    admin_username: str = 'admin'
    update_dir: Path | None = None
    trusted_proxy_ips: tuple[str, ...] = DEFAULT_TRUSTED_PROXIES

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / '.env')
        try:
            key = base64.b64decode(secret_value('APP_SECRET_KEY', 'app.key'), altchars=b'-_', validate=True)
            admin_hash = secret_value('APP_ADMIN_PASSWORD_HASH', 'admin.hash')
            extract_parameters(admin_hash)
        except (ValueError, TypeError):
            raise RuntimeError('密钥或管理员密码哈希格式不正确，请恢复原文件') from None
        if len(key) != 32:
            raise RuntimeError('AES-256-GCM 密钥必须为 32 字节；不会自动生成替换密钥')
        public_url = os.environ.get('APP_PUBLIC_URL', 'http://127.0.0.1:8000').rstrip('/')
        parsed = urlsplit(public_url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise RuntimeError('APP_PUBLIC_URL 必须是合法的 HTTP(S) 来源地址')
        if parsed.path not in ('', '/') or parsed.query or parsed.fragment:
            raise RuntimeError('APP_PUBLIC_URL 不能包含路径、查询参数或片段')
        if parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1') and os.environ.get('APP_ALLOW_INSECURE_HTTP') != '1':
            raise RuntimeError('HTTP IP 部署需要显式设置 APP_ALLOW_INSECURE_HTTP=1；公网使用建议反代 HTTPS')
        username = os.environ.get('APP_ADMIN_USERNAME', 'admin')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}', username):
            raise RuntimeError('管理员账号必须为 1–64 位字母、数字或 _.@-，并以字母或数字开头')
        try:
            proxies = proxy_sources(os.environ.get('APP_TRUSTED_PROXY_IPS', ','.join(DEFAULT_TRUSTED_PROXIES)))
        except ValueError as error:
            raise RuntimeError(str(error)) from None
        return cls(key, admin_hash, Path(os.environ.get('APP_DATA_DIR', str(ROOT / 'data'))),
                   public_url, parsed.scheme == 'https', os.environ.get('APP_START_WORKER', '1') != '0', username,
                   Path(os.environ['APP_UPDATE_DIR']) if os.environ.get('APP_UPDATE_DIR') else None, proxies)
