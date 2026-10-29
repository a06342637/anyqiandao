from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from app.db import SCHEMA_VERSION
from app.checkin_state import DailyCheckinState
from app.schemas import NetworkRoute, ProxyInput
from app.providers import Provider

Timestamp = Annotated[float, Field(ge=0, le=253402300799, allow_inf_nan=False, strict=True)]
Balance = Annotated[float, Field(ge=-1e15, le=1e15, allow_inf_nan=False, strict=True)]
Username = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]


class BackupAccount(BaseModel):
    provider: Provider = 'anyrouter'
    position: int | None = Field(default=None, ge=0, le=9223372036854775807)
    provider_position: int | None = Field(default=None, ge=0, le=9223372036854775807)
    username: Username
    password: str | None = Field(default=None, min_length=1, max_length=4096)
    note: str = Field(default='', max_length=500)
    result: dict | None = None
    validity: Literal['unknown', 'valid', 'invalid', 'blocked', 'network_error'] = 'unknown'
    message: str = Field(default='', max_length=4000)
    quota: Balance | None = None
    checkin_status: str | None = Field(default=None, max_length=40)
    last_validated: Timestamp | None = None
    last_extracted: Timestamp | None = None
    last_checkin: Timestamp | None = None
    daily_checkin: DailyCheckinState | None = None
    created: Timestamp | None = None
    updated: Timestamp | None = None
    checkin_route: NetworkRoute | None = None

    @field_validator('result')
    @classmethod
    def valid_credentials(cls, value):
        if value is not None:
            if not isinstance(value.get('session'), str) or not 1 <= len(value['session']) <= 65536 or not str(value.get('api_user', '')).isdigit():
                raise ValueError('备份凭证不完整')
        return value

    @model_validator(mode='after')
    def provider_matches(self):
        if self.result and self.result.get('provider') not in (None, self.provider):
            raise ValueError('凭证平台与账号平台不一致')
        return self


class BackupAccountRef(BaseModel):
    username: Username
    provider: Provider = 'anyrouter'


class BackupAssignment(BackupAccountRef):
    day: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    due_at: Timestamp
    expires_at: Timestamp
    status: Literal['planned', 'queued', 'missed'] = 'planned'


class BackupSchedule(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    interval_minutes: int = Field(default=1440, ge=5, le=525600)
    enabled: bool = True
    next_run: Timestamp | None = None
    last_run: Timestamp | None = None
    created: Timestamp | None = None
    accounts: list[Username | BackupAccountRef] = Field(default_factory=list, max_length=100000)
    auto: bool = False
    network_route: NetworkRoute | None = None
    mode: Literal['interval', 'random_window'] = 'interval'
    window_start: str = Field(default='13:00', pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    window_end: str = Field(default='18:00', pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    assignments: list[BackupAssignment] = Field(default_factory=list, max_length=200000)

    @model_validator(mode='after')
    def valid_window(self):
        from datetime import date, datetime
        from app.checkin_state import BEIJING
        from app.random_schedule import bounds
        if self.mode == 'random_window' and self.window_start >= self.window_end:
            raise ValueError('随机签到时间范围无效')
        for item in self.assignments:
            start, end = bounds(self.model_dump(exclude={'assignments'}), date.fromisoformat(item.day))
            if item.due_at >= item.expires_at or datetime.fromtimestamp(item.due_at, BEIJING).date().isoformat() != item.day:
                raise ValueError('随机安排日期或时间无效')
            if item.status == 'planned' and (not start <= item.due_at < end or item.expires_at != end):
                raise ValueError('随机安排超出配置窗口')
        return self


class BackupProxy(BaseModel):
    id: str | None = Field(default=None, max_length=100)
    name: str = Field(default='', max_length=100)
    enabled: bool = True
    config: dict
    trusted_key: str | None = Field(default=None, max_length=16384)
    created: Timestamp | None = None

    @field_validator('config')
    @classmethod
    def valid_proxy(cls, value):
        return ProxyInput.model_validate(value).model_dump(exclude={'name', 'enabled'}, exclude_none=True)


class BackupCheckin(BaseModel):
    provider: Provider = 'anyrouter'
    username: Username
    code: str = Field(min_length=1, max_length=40)
    quota_before: Balance | None = None
    quota_after: Balance | None = None
    used_before: Balance | None = None
    used_after: Balance | None = None
    reward_amount: float | None = Field(default=None, ge=0, le=1e15, allow_inf_nan=False)
    created: Timestamp
    balance_source: Literal['live', 'legacy'] = 'legacy'

    @model_validator(mode='after')
    def provider_reward(self):
        if self.provider == 'anyrouter' and self.reward_amount is not None and self.reward_amount > 25:
            raise ValueError('AnyRouter 奖励字段不能超过固定奖励')
        return self


class BackupDocument(BaseModel):
    app: Literal['any-signin-assistant']
    format: Literal[1]
    schema_version: int = Field(default=0, ge=0, le=SCHEMA_VERSION)
    settings: dict | None = None
    accounts: list[BackupAccount] = Field(default_factory=list, max_length=100000)
    schedules: list[BackupSchedule] = Field(default_factory=list, max_length=100000)
    proxies: list[BackupProxy] = Field(default_factory=list, max_length=10000)
    checkins: list[BackupCheckin] = Field(default_factory=list, max_length=1000000)
