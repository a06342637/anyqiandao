import argparse
import hashlib
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.update_common import APP_ID, EXCLUDED, PROJECT_ID, SOURCE_ITEMS, UpdateError, source_files, version_tuple

TOOL_SUFFIXES = {'.py', '.sh', '.ps1', '.cmd', '.md'}
LOCAL_DIRECTORIES = EXCLUDED | {'.venv', '.release', '.pytest_cache', '.ruff_cache'}


def project_files(root):
    root = Path(root).resolve()
    candidates = list(source_files(root))
    tools = root / 'tools'
    if tools.is_symlink():
        raise UpdateError('Tool directory must not be a symbolic link')
    for directory, directories, filenames in os.walk(tools):
        directories[:] = sorted(name for name in directories if name not in LOCAL_DIRECTORIES)
        for name in directories:
            if (Path(directory) / name).is_symlink():
                raise UpdateError('Tool directory must not contain symbolic links')
        for name in sorted(filenames):
            path = Path(directory) / name
            if path.suffix.lower() in TOOL_SUFFIXES and not name.startswith('.env'):
                candidates.append((path, path.relative_to(root).as_posix()))
    documents = [*root.glob('验收记录*.md'), root / '验收记录.txt', root / '项目文件与清理说明.md', root / '部署教程.txt']
    candidates.extend((path, path.name) for path in documents if path.exists() and path.name not in SOURCE_ITEMS)
    for path, name in sorted(candidates, key=lambda item: item[1]):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise UpdateError('Source file resolves outside the project or is a symbolic link')
        if not any(part in LOCAL_DIRECTORIES for part in Path(name).parts) and path.is_file():
            yield path, name


def build(output, root=ROOT):
    root = Path(root).resolve()
    output = Path(output).resolve()
    if (root / '.project-id').read_text(encoding='utf-8').strip() != PROJECT_ID:
        raise UpdateError('Invalid project marker')
    version = (root / 'VERSION').read_text(encoding='utf-8').strip()
    version_tuple(version)
    for name in (*SOURCE_ITEMS, 'tools'):
        if (root / name).is_dir() and output.is_relative_to(root / name):
            raise UpdateError('Export destination must not be inside a source directory')
    files = list(project_files(root))
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f'{APP_ID}-v{version}-source.zip'
    with tempfile.NamedTemporaryFile(dir=output, prefix='.source-', suffix='.zip', delete=False) as pending:
        temporary = Path(pending.name)
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as package:
            for path, name in files:
                package.write(path, arcname=f'{APP_ID}/{name}')
        os.replace(temporary, archive)
    finally:
        temporary.unlink(missing_ok=True)
    return archive


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Export project source and test tools without local data or dependencies.')
    parser.add_argument('--output', type=Path, default=ROOT / '.release')
    archive = build(parser.parse_args().output)
    with zipfile.ZipFile(archive) as package:
        report = {'archive': str(archive), 'files': len(package.infolist()),
                  'source_bytes': sum(item.file_size for item in package.infolist()),
                  'zip_bytes': archive.stat().st_size, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    print(json.dumps(report, ensure_ascii=False, indent=2))
