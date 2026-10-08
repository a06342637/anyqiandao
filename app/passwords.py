"""Shared administrator password policy and persistent encrypted override."""
ADMIN_PASSWORD_MIN = 5
ADMIN_PASSWORD_MAX = 1024
ADMIN_HASH_CONTEXT = 'admin_password_hash'


def effective_admin_hash(store, config, encoded=None):
    encoded = store.meta(ADMIN_HASH_CONTEXT) if encoded is None else encoded
    return store.vault.open(encoded, ADMIN_HASH_CONTEXT) if encoded else config.admin_hash
