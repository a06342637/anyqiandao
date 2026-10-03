import argparse
import hashlib
import json
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.update_common import APP_ID, PROJECT_ID, source_files, version_tuple


def build(output):
    version = (ROOT / 'VERSION').read_text().strip()
    version_tuple(version)
    if (ROOT / '.project-id').read_text().strip() != PROJECT_ID:
        raise SystemExit('Invalid project marker')
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f'{APP_ID}-v{version}.tar.gz'
    count = 0
    with tarfile.open(archive, 'w:gz') as package:
        for path, name in source_files(ROOT):
            package.add(path, arcname=name, recursive=False)
            count += 1
    document = {'app': APP_ID, 'version': version, 'archive': archive.name, 'size': archive.stat().st_size,
                'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    (output / 'release.json').write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Release v{version}: {count} source files, {document["size"]} bytes. Runtime data, secrets and verification tools are excluded.')
    print(f'Archive: {archive}')
    print(f'SHA256: {document["sha256"]}')
    return archive


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / '.release')
    build(parser.parse_args().output.resolve())
