from typing import Literal
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from app.passwords import ADMIN_PASSWORD_MIN, ADMIN_PASSWORD_MAX


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class LoginInput(StrictModel):
    username: str = Field(default='admin', min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class AdminPasswordInput(StrictModel):
    new_password: str = Field(min_length=ADMIN_PASSWORD_MIN, max_length=ADMIN_PASSWORD_MAX)


class AccountInput(StrictModel):
    username: str = Field(min_length=1, max_length=512)
    password: str = Field(min_length=1, max_length=4096)
    row_id: str = Field(min_length=1, max_length=100)

    @field_validator('username')
    @classmethod
    def clean_username(cls, value):
        if not value.strip():
            raise ValueError('账号不能为空')
        return value.strip()


class ImportInput(StrictModel):
    import_id: str = Field(min_length=1, max_length=100)
    accounts: list[AccountInput] = Field(min_length=1, max_length=100)


class CredentialPair(StrictModel):
    session: str = Field(min_length=1, max_length=65536)
    api_user: str = Field(min_length=1, max_length=20)

    @field_validator('session')
    @classmethod
    def clean_session(cls, value):
        value = value.strip()
        if value.startswith('session='):
            value = value[8:].split(';', 1)[0].strip()
        if not value or any(ord(character) < 33 or ord(character) > 126 or character == ';' for character in value):
            raise ValueError('请输入单独的 session 值，不包含其他 Cookie 或换行')
        return value

    @field_validator('api_user', mode='before')
    @classmethod
    def clean_api_user(cls, value):
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError('api_user 必须为正整数')
        value = str(value).strip()
        if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= 9223372036854775807:
            raise ValueError('api_user 必须为正整数')
        return str(int(value))


class CredentialInput(CredentialPair):
    username: str = Field(default='', max_length=512)


class CredentialImport(StrictModel):
    accounts: list[CredentialInput] = Field(min_length=1, max_length=100)


class InsightInput(StrictModel):
    view: Literal['dashboard', 'tokens']
    range: Literal['day', 'week', 'month'] = 'day'
    page: int = Field(default=1, ge=1, le=10000)
    limit: int = Field(default=10, ge=1, le=50)


class Selection(StrictModel):
    ids: list[str] = Field(default_factory=list, max_length=10000)
    all_matching: bool = False
    exclude_ids: list[str] = Field(default_factory=list, max_length=10000)
    validity: Literal['unknown', 'valid', 'invalid', 'blocked', 'network_error'] | None = None
    only_extracted: bool = False


class ActionInput(Selection):
    action: Literal['validate', 'extract', 'extract_invalid', 'checkin']
    password: str | None = Field(default=None, min_length=1, max_length=4096)
    repair_invalid: bool | None = None


class ProxyInput(StrictModel):
    name: str = Field(default='', max_length=100)
    scheme: Literal['auto', 'http', 'socks5', 'ssh'] = 'auto'
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    username: str = Field(default='', max_length=512)
    password: str | None = Field(default=None, max_length=4096)
    enabled: bool = True

    @field_validator('host')
    @classmethod
    def clean_host(cls, value):
        value = value.strip().strip('[]')
        if not value or any(character.isspace() or character in '/@?#\\' for character in value):
            raise ValueError('请只输入主机名或 IP 地址，不包含协议、路径和认证信息')
        return value


class AccountEdit(StrictModel):
    username: str | None = Field(default=None, min_length=1, max_length=512)
    password: str | None = Field(default=None, min_length=1, max_length=4096)
    note: str | None = Field(default=None, max_length=500)


class Reorder(StrictModel):
    ids: list[str] = Field(min_length=1, max_length=500)


class ProxyImport(StrictModel):
    lines: list[str] = Field(min_length=1, max_length=100)


class ProxyTrust(StrictModel):
    fingerprint: str = Field(min_length=10, max_length=200)


class NetworkRoute(StrictModel):
    mode: Literal['inherit', 'direct', 'proxy'] = 'direct'
    proxy_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode='after')
    def selected_proxy(self):
        if self.mode == 'proxy' and not self.proxy_id:
            raise ValueError('请选择一个代理节点')
        if self.mode != 'proxy':
            self.proxy_id = None
        return self


class OperationRoutes(StrictModel):
    model_config = ConfigDict(extra='forbid', populate_by_name=True, serialize_by_alias=True)
    extract: NetworkRoute = Field(default_factory=NetworkRoute)
    refresh: NetworkRoute = Field(default_factory=NetworkRoute)
    validation: NetworkRoute = Field(default_factory=NetworkRoute, alias='validate')
    checkin: NetworkRoute = Field(default_factory=NetworkRoute)
    tokens: NetworkRoute = Field(default_factory=NetworkRoute)
    dashboard: NetworkRoute = Field(default_factory=NetworkRoute)

    @model_validator(mode='after')
    def no_inheritance(self):
        if any(getattr(self, name).mode == 'inherit' for name in type(self).model_fields):
            raise ValueError('操作默认线路必须选择直连或指定代理')
        return self


class AccountCheckinRoute(Selection):
    network_route: NetworkRoute = Field(default_factory=lambda: NetworkRoute(mode='inherit'))


class ScheduleInput(Selection):
    name: str = Field(min_length=1, max_length=100)
    interval_minutes: int = Field(default=1440, ge=5, le=525600)
    enabled: bool = True
    keep_accounts: bool = False
    network_route: NetworkRoute = Field(default_factory=lambda: NetworkRoute(mode='inherit'))


class BrandingSettings(StrictModel):
    site_name: str = Field(default='any签到助手', min_length=1, max_length=40)
    site_icon_text: str = Field(default='签', min_length=1, max_length=2)

    @field_validator('site_name', 'site_icon_text')
    @classmethod
    def clean_branding(cls, value):
        value = value.strip()
        if not value or any(unicodedata.category(character).startswith('C') for character in value):
            raise ValueError('网站名称和图标文字不能为空，也不能包含控制字符')
        return value


class QueueSettings(StrictModel):
    max_concurrency: int = Field(default=1, ge=1, le=5)
    checkin_concurrency: int = Field(default=1, ge=1, le=5)

    @model_validator(mode='after')
    def global_serial(self):
        # Read old settings safely, but never permit the old two-lane overlap.
        self.max_concurrency = self.checkin_concurrency = 1
        return self


class HistorySelection(StrictModel):
    ids: list[str] = Field(default_factory=list, max_length=10000)
    all_matching: bool = False
    exclude_ids: list[str] = Field(default_factory=list, max_length=10000)
    account_id: str | None = Field(default=None, max_length=100)
    job_id: str | None = Field(default=None, max_length=100)
    category: str | None = Field(default=None, max_length=40)
    before: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class JobSelection(StrictModel):
    ids: list[str] = Field(default_factory=list, max_length=10000)
    all_matching: bool = False
    exclude_ids: list[str] = Field(default_factory=list, max_length=10000)
    lane: Literal['', 'queue', 'checkin'] = ''
    state: Literal['', 'active', 'finished'] = ''


class RuntimeSettings(BrandingSettings, QueueSettings):
    proxy_mode: Literal['pool', 'direct'] = 'direct'
    operation_routes: OperationRoutes = Field(default_factory=OperationRoutes)
    connect_timeout: int = Field(default=10, ge=3, le=120)
    login_timeout: int = Field(default=60, ge=15, le=300)
    account_gap: int = Field(default=2, ge=0, le=60)
    log_retention_days: int = Field(default=7, ge=1, le=3650)
    queue_retention_days: int = Field(default=7, ge=1, le=3650)
    log_cleanup_hours: int = Field(default=24, ge=1, le=168)
    log_cleanup_enabled: bool = True
    timezone: str = 'Asia/Seoul'
    auto_checkin: bool = True
    auto_checkin_interval_minutes: int = Field(default=1440, ge=5, le=525600)
    auto_reextract: bool = True
    stats_retention_days: int = Field(default=90, ge=1, le=3650)

    @field_validator('timezone')
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError('时区名称无效') from error
        return value
