import math
from datetime import datetime
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

BEIJING = ZoneInfo('Asia/Shanghai')
DAILY_REWARD = Decimal('25')
REWARD_TOLERANCE = Decimal('0.0001')


class DailyCheckinState(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)

    api_user: str = Field(pattern=r'^[0-9]{1,19}$')
    day: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    observed_at: float = Field(ge=0, le=253402300799)
    quota: float | None = None
    used_quota: float | None = None
    confirmed_at: float | None = Field(default=None, ge=0, le=253402300799)
    last_signed_at: float | None = Field(default=None, ge=0, le=253402300799)
    evidence: Literal['', 'balance_25', 'receipt', 'history'] = ''


def beijing_day(timestamp):
    return datetime.fromtimestamp(timestamp, BEIJING).strftime('%Y-%m-%d')


def finite_amount(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def fixed_reward(value):
    return finite_amount(value) and abs(Decimal(str(value)) - DAILY_REWARD) <= REWARD_TOLERANCE


def balance_credit(before, after):
    if not finite_amount(before.get('quota')) or not finite_amount(after.get('quota')):
        return None
    gain = Decimal(str(after['quota'])) - Decimal(str(before['quota']))
    used_before, used_after = before.get('used_quota'), after.get('used_quota')
    if finite_amount(used_before) and finite_amount(used_after):
        if used_after < used_before:
            return None
        gain += Decimal(str(used_after)) - Decimal(str(used_before))
    elif used_before is not None or used_after is not None:
        return None
    return float(gain)


def valid_observation(state, api_user, now):
    return bool(state and state.api_user == str(api_user) and state.observed_at <= now
                and state.day == beijing_day(state.observed_at))


def confirmed_today(state, api_user, now):
    return bool(valid_observation(state, api_user, now) and state.day == beijing_day(now)
                and state.confirmed_at is not None and 0 <= state.confirmed_at <= now
                and beijing_day(state.confirmed_at) == state.day and state.evidence)


def merge_daily_state(previous, incoming, api_user, now):
    candidates = [state for state in (previous, incoming) if valid_observation(state, api_user, now)]
    if not candidates:
        return None
    state = max(candidates, key=lambda item: item.observed_at).model_copy()
    same_day = [item for item in candidates if item.day == state.day]
    needs_usage = any(item.used_quota is not None for item in same_day)
    complete = [item for item in same_day if item.quota is not None and (not needs_usage or item.used_quota is not None)]
    if complete and (state.quota is None or needs_usage and state.used_quota is None):
        balance = max(complete, key=lambda item: item.observed_at)
        state.quota, state.used_quota = balance.quota, balance.used_quota
    confirmations = [item for item in candidates if confirmed_today(item, api_user, state.observed_at)]
    confirmed = min(confirmations, key=lambda item: item.confirmed_at) if confirmations else None
    state.confirmed_at = confirmed.confirmed_at if confirmed else None
    state.evidence = confirmed.evidence if confirmed else ''
    signed_times = [item.last_signed_at for item in candidates if item.last_signed_at is not None and item.last_signed_at <= now]
    if confirmed:
        signed_times.append(confirmed.confirmed_at)
    state.last_signed_at = max(signed_times, default=None)
    return state


def observe_balance(previous, api_user, quota, used_quota, now, *, confirm=False, evidence='receipt'):
    identity, day = str(api_user), beijing_day(now)
    if previous and previous.api_user != identity:
        previous = None
    if previous and previous.observed_at > now:
        return previous
    same_day = bool(previous and previous.day == day and beijing_day(previous.observed_at) == day)
    quota = quota if finite_amount(quota) else None
    used_quota = used_quota if finite_amount(used_quota) else None
    gain = balance_credit(previous.model_dump(), {'quota': quota, 'used_quota': used_quota}) if same_day else None
    if same_day and (quota is None or previous.used_quota is not None and used_quota is None):
        quota, used_quota = previous.quota, previous.used_quota
    state = DailyCheckinState(api_user=identity, day=day, observed_at=now, quota=quota, used_quota=used_quota,
                             last_signed_at=previous.last_signed_at if previous and (previous.last_signed_at or 0) <= now else None)
    if confirmed_today(previous, identity, now):
        state.confirmed_at, state.evidence = previous.confirmed_at, previous.evidence
    elif confirm or fixed_reward(gain):
        state.confirmed_at = now
        state.last_signed_at = now
        state.evidence = evidence if confirm else 'balance_25'
    return state
