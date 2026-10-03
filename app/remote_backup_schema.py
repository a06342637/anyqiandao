import re
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator

from app.schemas import StrictModel


class OSSSettings(StrictModel):
    enabled: bool = False
    region: str = Field(default='cn-hangzhou', max_length=64)
    bucket: str = Field(default='', max_length=63)
    access_key_id: str = Field(default='', max_length=256)
    access_key_secret: str = Field(default='', max_length=4096)
    prefix: str = Field(default='any-signin/', max_length=512)
    internal: bool = False
    endpoint: str = Field(default='', max_length=253)
    keep: int = Field(default=10, ge=1, le=1000)

    @field_validator('region')
    @classmethod
    def valid_region(cls, value):
        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)+', value):
            raise ValueError('地域格式不正确，例如 cn-hangzhou')
        return value

    @field_validator('bucket')
    @classmethod
    def valid_bucket(cls, value):
        value = value.strip()
        if value and not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', value):
            raise ValueError('Bucket 名称格式不正确')
        return value

    @field_validator('endpoint')
    @classmethod
    def valid_endpoint(cls, value):
        value = value.strip()
        if not value:
            return value
        parsed = urlsplit(value if '://' in value else 'https://' + value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.path not in ('', '/') or parsed.query or parsed.fragment
                or not re.fullmatch(r'[a-z0-9.-]+', parsed.hostname)):
            raise ValueError('Endpoint 必须是 HTTPS 域名，不能包含路径或凭证')
        return parsed.hostname

    @field_validator('prefix')
    @classmethod
    def valid_prefix(cls, value):
        value = value.strip().strip('/')
        if '\\' in value or any(part in ('.', '..') for part in value.split('/')) or any(ord(c) < 32 for c in value):
            raise ValueError('保存目录不能包含相对路径或控制字符')
        return value + '/' if value else ''


class SFTPSettings(StrictModel):
    enabled: bool = False
    host: str = Field(default='', max_length=253)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(default='root', max_length=128)
    auth: Literal['password', 'private_key'] = 'password'
    password: str = Field(default='', max_length=4096)
    private_key: str = Field(default='', max_length=32768)
    passphrase: str = Field(default='', max_length=4096)
    directory: str = Field(default='/root/any-signin-backups', min_length=1, max_length=1024)
    keep: int = Field(default=10, ge=1, le=1000)

    @field_validator('host', 'username')
    @classmethod
    def clean_field(cls, value):
        value = value.strip()
        if any(c.isspace() or ord(c) < 32 for c in value) or any(c in value for c in '/\\@'):
            raise ValueError('主机和用户名不能包含空格、路径或 @')
        return value

    @field_validator('directory')
    @classmethod
    def valid_directory(cls, value):
        value = value.strip().rstrip('/') or '/'
        if not value.startswith('/') or '\\' in value or any(p in ('.', '..') for p in value.split('/')) or any(ord(c) < 32 for c in value):
            raise ValueError('请填写服务器绝对目录，不能包含 ..')
        return value


class RemoteBackupSettings(StrictModel):
    enabled: bool = False
    every: int = Field(default=1, ge=1, le=365)
    unit: Literal['days', 'hours'] = 'days'
    time: str = Field(default='04:30', pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    timezone: str = Field(default='Asia/Shanghai', max_length=64)
    mode: Literal['app', 'full'] = 'app'
    encryption_password: str = Field(default='', max_length=1024)
    oss: OSSSettings = Field(default_factory=OSSSettings)
    sftp: SFTPSettings = Field(default_factory=SFTPSettings)

    @field_validator('timezone')
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('时区不正确') from None
        return value


class BrowseInput(StrictModel):
    path: str = Field(default='', max_length=1024)
