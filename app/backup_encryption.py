"""Versioned password-encrypted backup envelope: scrypt + AES-256-GCM."""
import base64
import secrets
import threading

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b'ASBACKUP1'
HEADER_SIZE = len(MAGIC) + 16 + 12
SLOTS = threading.BoundedSemaphore(2)


def derive(password, salt):
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise ValueError('备份密码需要 12–1024 位，请使用独立的强密码')
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode('utf-8'))


def encrypt_bytes(content, password):
    with SLOTS:
        salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
        header = MAGIC + salt + nonce
        return header + AESGCM(derive(password, salt)).encrypt(nonce, content, header)


def decrypt_bytes(content, password):
    if not content.startswith(MAGIC) or len(content) < HEADER_SIZE + 16:
        raise ValueError('不是有效的加密备份文件')
    with SLOTS:
        salt = content[len(MAGIC):len(MAGIC) + 16]
        nonce = content[len(MAGIC) + 16:HEADER_SIZE]
        try:
            return AESGCM(derive(password, salt)).decrypt(nonce, content[HEADER_SIZE:], content[:HEADER_SIZE])
        except InvalidTag:
            raise ValueError('备份密码不正确或文件已损坏，未恢复任何数据') from None


def encrypt_file(source, destination, password):
    with SLOTS:
        salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
        header = MAGIC + salt + nonce
        cipher = Cipher(algorithms.AES(derive(password, salt)), modes.GCM(nonce)).encryptor()
        cipher.authenticate_additional_data(header)
        with source.open('rb') as incoming, destination.open('xb') as outgoing:
            outgoing.write(header)
            while block := incoming.read(1024 * 1024):
                outgoing.write(cipher.update(block))
            outgoing.write(cipher.finalize())
            outgoing.write(cipher.tag)
    return destination


def password_header(value):
    try:
        if len(value) > 8192:
            raise ValueError()
        return base64.b64decode(value, validate=True).decode('utf-8')
    except (ValueError, UnicodeError):
        raise ValueError('备份密码格式不正确') from None
