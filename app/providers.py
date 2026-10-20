"""Fixed provider identities. Credentials never choose an arbitrary destination."""
from typing import Literal

from app.checkin_state import finite_amount, fixed_reward

Provider = Literal['anyrouter', 'agentrouter']
ORIGINS = {'anyrouter': 'https://anyrouter.top', 'agentrouter': 'https://agentrouter.org'}
LABELS = {'anyrouter': 'AnyRouter', 'agentrouter': 'AgentRouter'}


def origin_for(provider):
    if provider not in ORIGINS:
        raise ValueError('不支持的账号平台')
    return ORIGINS[provider]


def account_fingerprint(vault, username, provider='anyrouter'):
    origin_for(provider)
    digest = vault.fingerprint(username)
    return digest if provider == 'anyrouter' else provider + ':' + digest


def verified_reward(provider, gain):
    return fixed_reward(gain) if provider == 'anyrouter' else finite_amount(gain) and gain > 0
