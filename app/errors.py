class TaskError(Exception):
    def __init__(self, code, message, *, retry_proxy=False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_proxy = retry_proxy


class HostKeyRequired(TaskError):
    def __init__(self, public_key, fingerprint, changed=False):
        super().__init__('host_key_required', 'SSH 主机指纹发生变化，请核对' if changed else '请在设置中核对并确认 SSH 主机指纹')
        self.public_key = public_key
        self.fingerprint = fingerprint
