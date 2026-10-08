from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator

from app.db import SCHEMA_VERSION
from app.checkin_state import DailyCheckinState
from app.schemas import NetworkRoute, ProxyInput

Timestamp = Annotated[float, Field(ge=0, le=253402300799, allow_inf_nan=False, strict=True)]
Balance = Annotated[float, Field(ge=-1e15, le=1e15, allow_inf_nan=False, strict=True)]
Username = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]


class BackupAccount(BaseModel):
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


class BackupSchedule(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    interval_minutes: int = Field(default=1440, ge=5, le=525600)
    enabled: bool = True
    next_run: Timestamp | None = None
    last_run: Timestamp | None = None
    created: Timestamp | None = None
    accounts: list[Username] = Field(default_factory=list, max_length=100000)
    auto: bool = False
    network_route: NetworkRoute | None = None


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
    username: Username
    code: str = Field(min_length=1, max_length=40)
    quota_before: Balance | None = None
    quota_after: Balance | None = None
    used_before: Balance | None = None
    used_after: Balance | None = None
    reward_amount: float | None = Field(default=None, ge=0, le=25, allow_inf_nan=False)
    created: Timestamp
    balance_source: Literal['live', 'legacy'] = 'legacy'


class BackupDocument(BaseModel):
    app: Literal['any-signin-assistant']
    format: Literal[1]
    schema_version: int = Field(default=0, ge=0, le=SCHEMA_VERSION)
    settings: dict | None = None
    accounts: list[BackupAccount] = Field(default_factory=list, max_length=100000)
    schedules: list[BackupSchedule] = Field(default_factory=list, max_length=100000)
    proxies: list[BackupProxy] = Field(default_factory=list, max_length=10000)
    checkins: list[BackupCheckin] = Field(default_factory=list, max_length=1000000)
