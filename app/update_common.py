import hashlib
import json
import os
import re
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path, PurePosixPath

APP_ID = 'any-signin-assistant'
DEFAULT_REPOSITORY = 'a06342637/anyqiandao'
PROJECT_ID = 'any-signin-assistant-managed-v1'
SOURCE_ITEMS = ('app', 'frontend', 'scripts', 'deploy', 'docs', 'Dockerfile', 'compose.yml', 'requirements.txt',
                'VERSION', 'README.md', 'CHANGELOG.md', '.dockerignore', '.gitignore', '.gitattributes', '.project-id', '使用说明.txt', '部署教程.txt')
EXCLUDED = {'node_modules', 'dist', '__pycache__', '.git', '.local', 'data', 'secrets', 'backups', 'updates'}
MAX_ARCHIVE = 64 * 1024 * 1024
TERMINAL = {'complete', 'failed', 'rolled_back'}


class UpdateError(RuntimeError):
    pass


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'v?(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})', value):
        raise UpdateError('发布版本必须为 v主版本.次版本.修订号，例如 v0.3.1')
    return tuple(int(part) for part in value.removeprefix('v').split('.'))


def repository_name(value):
    value = value.strip().removeprefix('https://github.com/').rstrip('/').removesuffix('.git')
    if value and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', value):
        raise UpdateError('请输入 GitHub 的 用户名/仓库名 或对应的 HTTPS 仓库地址')
    return value


def safe_release_url(value):
    parsed = urllib.parse.urlsplit(value)
    allowed = {'api.github.com', 'github.com', 'objects.githubusercontent.com', 'release-assets.githubusercontent.com'}
    if parsed.scheme != 'https' or parsed.hostname not in allowed or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise UpdateError('发布资源必须来自 GitHub 官方 HTTPS 下载地址')
    return value


class ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        safe_release_url(url)
        return super().redirect_request(request, response, code, message, headers, url)


def fetch_bytes(url, limit, timeout=30):
    safe_release_url(url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), ReleaseRedirect())
    request = urllib.request.Request(url, headers={'User-Agent': APP_ID, 'Accept': 'application/vnd.github+json' if 'api.github.com/' in url else 'application/octet-stream'})
    try:
        with opener.open(request, timeout=timeout) as response:
            content = response.read(limit + 1)
    except urllib.error.HTTPError as error:
        if error.code in (403, 429):
            raise UpdateError('GitHub 暂时限流或拒绝访问，请稍后再检测版本') from None
        if error.code == 404:
            raise UpdateError('未找到公开发布版本或发布附件，请检查本项目的更新仓库') from None
        raise UpdateError(f'下载发布信息失败（HTTP {error.code}）') from None
    except (OSError, TimeoutError, urllib.error.URLError):
        raise UpdateError('无法连接 GitHub 发布服务，请检查服务器网络后重试') from None
    if len(content) > limit:
        raise UpdateError('发布文件超过大小限制，已拒绝下载')
    return content


def latest_release(repository):
    repository = repository_name(repository)
    if not repository:
        raise UpdateError('请先配置本项目的发布仓库；签到脚本上游不是此程序的更新源')
    try:
        release = json.loads(fetch_bytes(f'https://api.github.com/repos/{repository}/releases/latest', 1024 * 1024))
        tag = release['tag_name']
        version_tuple(tag)
        if release.get('draft') or release.get('prerelease'):
            raise UpdateError('只支持已正式发布的稳定版本')
        version = tag.removeprefix('v')
        archive_name = f'{APP_ID}-v{version}.tar.gz'
        base = f'https://github.com/{repository}/releases/download/{tag}/'
        assets = {asset['name']: asset['browser_download_url'] for asset in release['assets']}
        if assets.get('release.json') != base + 'release.json' or assets.get(archive_name) != base + archive_name:
            raise UpdateError('发布缺少本项目的 release.json 或源码包，不能安装其他项目的版本')
        manifest = json.loads(fetch_bytes(assets['release.json'], 16384))
        if manifest.get('app') != APP_ID or manifest.get('version') != version or manifest.get('archive') != archive_name:
            raise UpdateError('发布清单与项目或版本不一致，已拒绝更新')
        if not re.fullmatch(r'[0-9a-f]{64}', manifest.get('sha256', '')):
            raise UpdateError('发布清单缺少有效的 SHA-256 校验值')
        size = manifest.get('size')
        if type(size) is not int or not 0 < size <= MAX_ARCHIVE:
            raise UpdateError('发布包大小不合法')
        return {'version': version, 'tag': tag, 'repository': repository, 'archive_url': assets[archive_name],
                'sha256': manifest['sha256'], 'size': size, 'release_url': f'https://github.com/{repository}/releases/tag/{tag}',
                'published_at': release.get('published_at'), 'notes': str(release.get('body') or '')[:12000]}
    except (KeyError, TypeError, ValueError):
        raise UpdateError('远程发布数据格式不正确，未执行更新') from None


def read_json(path, default=None, limit=262144):
    try:
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        with os.fdopen(os.open(path, flags), 'rb') as source:
            content = source.read(limit + 1)
        if len(content) > limit:
            return default
        return json.loads(content)
    except (OSError, ValueError):
        return default


def local_release(archive, manifest_path):
    archive = Path(archive)
    manifest = read_json(manifest_path, {}, limit=16384)
    if not isinstance(manifest, dict):
        raise UpdateError('本地发布清单格式不正确')
    version = manifest.get('version')
    version_tuple(version)
    if manifest.get('app') != APP_ID or manifest.get('archive') != f'{APP_ID}-v{version}.tar.gz':
        raise UpdateError('本地发布清单与项目或版本不一致')
    size = manifest.get('size')
    if type(size) is not int or not 0 < size <= MAX_ARCHIVE or not re.fullmatch(r'[0-9a-f]{64}', str(manifest.get('sha256', ''))):
        raise UpdateError('本地发布清单缺少合法的文件大小或 SHA-256')
    if archive.is_symlink() or not archive.is_file() or archive.stat().st_size != size:
        raise UpdateError('本地源码包不存在、包含链接或大小不匹配')
    return manifest


def atomic_json(path, document, mode=0o600):
    path = Path(path)
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as output:
            json.dump(document, output, ensure_ascii=False, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def source_files(root):
    root = Path(root).resolve()
    for name in SOURCE_ITEMS:
        item = root / name
        if item.is_symlink():
            raise UpdateError('源码目录包含符号链接，拒绝自动更新')
        candidates = []
        if item.is_dir():
            for directory, directories, filenames in os.walk(item):
                directories[:] = sorted(name for name in directories if name not in EXCLUDED)
                for directory_name in directories:
                    if (Path(directory) / directory_name).is_symlink():
                        raise UpdateError('源码目录包含符号链接，拒绝自动更新')
                candidates.extend(Path(directory) / filename for filename in sorted(filenames))
        elif item.exists():
            candidates = [item]
        for path in candidates:
            relative = path.relative_to(root)
            if any(part in EXCLUDED for part in relative.parts) or path.name.startswith('.env') or path.suffix in ('.pyc', '.log', '.tsbuildinfo'):
                continue
            if path.is_symlink():
                raise UpdateError('源码包含符号链接，拒绝自动更新')
            if path.is_file():
                yield path, relative.as_posix()


def source_manifest(root):
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for path, name in source_files(root)}


def local_changes(root):
    baseline = read_json(Path(root) / '.release-files.json')
    if not isinstance(baseline, dict) or not baseline:
        raise UpdateError('缺少源码基线，请先在服务器运行 scripts/install-updater.sh 完成初始化')
    current = source_manifest(root)
    return sorted(name for name in baseline.keys() | current.keys() if baseline.get(name) != current.get(name))


def extract_source(archive, destination, expected_version):
    destination = Path(destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    size, seen = 0, set()
    with tarfile.open(archive, 'r:gz') as source:
        for member in source:
            path = PurePosixPath(member.name)
            if not member.name or path.is_absolute() or '..' in path.parts or '\\' in member.name or not path.parts or path.parts[0] not in SOURCE_ITEMS:
                raise UpdateError('发布包包含不允许的路径')
            if any(part in EXCLUDED for part in path.parts) or any(part.startswith('.env') for part in path.parts):
                raise UpdateError('发布包包含运行数据或私密配置')
            if not member.isfile() and not member.isdir():
                raise UpdateError('发布包包含链接或特殊文件，拒绝解压')
            size += member.size
            if member.size > 16 * 1024 * 1024 or size > 128 * 1024 * 1024 or len(seen) >= 10000 or member.name in seen:
                raise UpdateError('发布包超过解压限制或包含重复路径')
            seen.add(member.name)
            target = destination.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as content, target.open('xb') as output:
                    output.write(content.read())
                target.chmod(0o644)
    required = ('.project-id', 'VERSION', 'Dockerfile', 'compose.yml', 'requirements.txt', 'app/main.py', 'frontend/package.json')
    if any(not (destination / name).is_file() for name in required):
        raise UpdateError('发布包缺少必要的项目文件')
    if (destination / '.project-id').read_text().strip() != PROJECT_ID or (destination / 'VERSION').read_text().strip() != expected_version:
        raise UpdateError('源码包的项目标记或版本不匹配')
