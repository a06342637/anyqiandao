import base64
import hashlib
import hmac
import json
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class Vault:
    def __init__(self, key: bytes):
        self.key = key
        self.cipher = AESGCM(key)

    def seal(self, value, context: str) -> str:
        nonce = secrets.token_bytes(12)
        plaintext = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
        ciphertext = self.cipher.encrypt(nonce, plaintext, context.encode())
        return base64.urlsafe_b64encode(nonce + ciphertext).decode()

    def open(self, value: str, context: str):
        packed = base64.urlsafe_b64decode(value)
        return json.loads(self.cipher.decrypt(packed[:12], packed[12:], context.encode()))

    def fingerprint(self, value: str) -> str:
        return hmac.new(self.key, value.encode(), hashlib.sha256).hexdigest()
