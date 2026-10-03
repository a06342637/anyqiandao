"""Reject runtime/private files and recognizable credentials in the source tree."""
import ipaddress
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {'.local', '.venv', 'node_modules', 'data', 'secrets', 'backups', 'updates', '.release'}
PRIVATE_SUFFIXES = {'.sqlite', '.sqlite3', '.db', '.key', '.pem', '.p12', '.pfx'}
PATTERNS = {
    'private key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'GitHub token': re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{40,})'),
    'cloud access key': re.compile(rb'\b(?:AKIA[A-Z0-9]{16}|LTAI[A-Za-z0-9]{16,})\b'),
    'personal Windows path': re.compile(rb'[A-Za-z]:[/\\]Users[/\\](?!Public\b|USER\b|example\b)[A-Za-z0-9_.-]+[/\\]', re.I),
}


def main():
    paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).split(b'\0')
    findings = []
    for raw in paths:
        if not raw:
            continue
        relative = Path(raw.decode('utf-8'))
        path = ROOT / relative
        if not path.is_file():
            continue
        if any(part in FORBIDDEN for part in relative.parts) or relative.suffix in PRIVATE_SUFFIXES or relative.name in ('.env', '部署信息.txt'):
            findings.append((str(relative), 'runtime/private file'))
            continue
        if relative.suffix in ('.png', '.jpg', '.jpeg', '.gif', '.webp'):
            findings.append((str(relative), 'unreviewed screenshot/image'))
            continue
        content = path.read_bytes()
        for label, pattern in PATTERNS.items():
            if pattern.search(content):
                findings.append((str(relative), label))
        if relative.suffix in ('.py', '.sh', '.md', '.txt', '.tsx', '.ts', '.json'):
            addresses = re.sub(rb'(?:Chrome|Firefox|Safari|Version)/[\d.]+', b'Browser/VERSION', content)
            for match in re.finditer(rb'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])', addresses):
                try:
                    if ipaddress.ip_address(match.group().decode()).is_global:
                        findings.append((str(relative), 'literal public IP; use a documentation address or environment variable'))
                        break
                except ValueError:
                    pass
    for path, label in findings:
        print(f'{path}: {label}')
    if findings:
        raise SystemExit(1)
    print('Source privacy checks passed; no runtime data, personal machine paths or recognizable credentials.')


if __name__ == '__main__':
    main()
