"""Decrypt a password-protected backup locally without starting the application."""
import argparse
import getpass
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.backup_encryption import decrypt_bytes


def main():
    parser = argparse.ArgumentParser(description='解密 ASB 备份，密码仅从终端安全读取，不写入命令行参数。')
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.source.stat().st_size > 256 * 1024 * 1024:
        raise SystemExit('备份超过离线解密工具的 256MB 限制')
    if args.output.exists():
        raise SystemExit('输出文件已存在，请选择新的文件名')
    try:
        content = decrypt_bytes(args.source.read_bytes(), getpass.getpass('备份密码：'))
    except ValueError as error:
        raise SystemExit(str(error)) from None
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(content)
    print('解密完成，内容格式：' + ('ZIP' if content.startswith(b'PK') else 'JSON') + '。请私密保管解密文件。')


if __name__ == '__main__':
    main()
