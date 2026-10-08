import argparse
import contextlib
import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path

try:
    import update_common as common
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app import update_common as common


class Updater:
    def __init__(self, root):
        self.root = Path(root).resolve()
        if self.root == Path('/') or (self.root / '.project-id').read_text().strip() != common.PROJECT_ID:
            raise common.UpdateError('项目目录标记不正确，拒绝运行更新器')
        self.directory = self.root / 'updates'
        for path in (self.directory, self.directory / 'status', self.directory / 'requests', self.directory / '.deploy.lock', self.root / 'data', self.root / 'backups'):
            if path.is_symlink() or not path.resolve().is_relative_to(self.root):
                raise common.UpdateError('运行目录存在不安全的链接')
        self.status_file = self.directory / 'status' / 'state.json'
        self.context_file = self.directory / 'status' / 'operation.json'
        self.state = common.read_json(self.status_file, {})
        self.context = common.read_json(self.context_file, {})
        self.log = None

    @contextlib.contextmanager
    def deployment_lock(self):
        import fcntl
        with (self.directory / '.deploy.lock').open('a') as guard:
            try:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(guard, fcntl.LOCK_UN)

    def transition(self, stage, message, progress=None):
        now = time.time()
        self.state.update(stage=stage, message=message, updated_at=now)
        if progress is not None:
            self.state['progress'] = progress
        self.state['history'] = (self.state.get('history', []) + [{'at': now, 'stage': stage, 'message': message}])[-45:]
        if stage in common.TERMINAL:
            self.state['finished_at'] = now
        common.atomic_json(self.status_file, self.state, 0o644)
        if stage in common.TERMINAL:
            try:
                with contextlib.closing(self.database()) as connection, connection:
                    connection.execute('INSERT INTO logs(kind,level,message,created,category) VALUES (?,?,?,?,?)',
                                       ('system', 'info' if stage == 'complete' else 'error', message, now, 'system' if stage == 'complete' else 'error'))
            except (sqlite3.Error, common.UpdateError):
                pass

    def save_context(self):
        common.atomic_json(self.context_file, self.context)

    def execute(self, command, timeout=300):
        environment = os.environ.copy()
        environment.pop('APP_IMAGE', None)
        try:
            return subprocess.run(command, cwd=self.root, env=environment, stdout=self.log, stderr=subprocess.STDOUT,
                                  check=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise common.UpdateError('操作超过安全时限，已停止更新并准备恢复；详情见服务器更新日志') from None
        except subprocess.CalledProcessError:
            raise common.UpdateError('构建或服务启动失败，详情见服务器 updates/work 下的 update.log') from None

    def compose(self, *arguments):
        return self.execute(['docker', 'compose', '-p', common.APP_ID, *arguments], timeout=300)

    def stop_app(self):
        result = subprocess.run(['docker', 'ps', '-aq', '--filter', f'label=com.docker.compose.project={common.APP_ID}',
                                 '--filter', 'label=com.docker.compose.service=app'], check=True, capture_output=True, text=True, timeout=30)
        for identifier in result.stdout.split():
            if not re.fullmatch('[0-9a-f]{12,64}', identifier):
                raise common.UpdateError('应用容器标识不正确')
            self.execute(['docker', 'stop', '--time', '90', identifier], timeout=120)

    def environment(self):
        values = {}
        for line in (self.root / '.env').read_text().splitlines():
            key, separator, value = line.partition('=')
            if separator and not key.startswith('#'):
                values[key.strip()] = value.strip().strip('"').strip("'")
        return values

    def set_image(self, image):
        path = self.root / '.env'
        lines = [line for line in path.read_text().splitlines() if not line.startswith('APP_IMAGE=')]
        temporary = self.root / f'.env.update-{self.state["id"]}'
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as output:
            output.write('\n'.join(lines + [f'APP_IMAGE={image}', '']))
        os.replace(temporary, path)

    def database(self):
        path = self.root / 'data' / 'assistant.sqlite3'
        if path.is_symlink() or not path.is_file():
            raise common.UpdateError('数据库不存在或指向不安全的链接')
        return sqlite3.connect(path, timeout=30)

    def counts(self):
        with contextlib.closing(self.database()) as connection:
            return {table: connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in ('accounts', 'schedules', 'proxies', 'checkins')}

    def pause(self):
        with contextlib.closing(self.database()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            values = dict(connection.execute("SELECT key,value FROM meta WHERE key IN ('queue_paused','pause_reason')"))
            self.context['previous_queue'] = values
            self.save_context()
            connection.executemany('INSERT OR REPLACE INTO meta VALUES (?,?)',
                                   [('maintenance', '1'), ('queue_paused', '1'), ('pause_reason', '版本更新维护中')])
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            with contextlib.closing(self.database()) as connection:
                running = connection.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0]
                backup = connection.execute("SELECT value FROM meta WHERE key='remote_backup_state'").fetchone()
                backing_up = bool(backup and json.loads(backup[0]).get('running'))
            if not running and not backing_up:
                return
            time.sleep(2)
        raise common.UpdateError('当前账号任务或备份在十分钟内未结束，取消本次更新，不会强行中断')

    def check_build_memory(self):
        try:
            memory = {line.split(':')[0]: int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}
        except (OSError, ValueError, IndexError):
            return
        available, swap = memory.get('MemAvailable', 0), memory.get('SwapFree', 0)
        if available < 256 * 1024 or available + swap < 768 * 1024:
            raise common.UpdateError('可用内存不足以安全构建镜像；请释放内存或配置至少 1GB Swap 后重试，原服务和数据保留')

    def resume(self):
        previous = self.context.get('previous_queue')
        if previous is None:
            return
        with contextlib.closing(self.database()) as connection, connection:
            connection.executemany('INSERT OR REPLACE INTO meta VALUES (?,?)',
                                   [('maintenance', '0'), ('queue_paused', previous.get('queue_paused', '0')), ('pause_reason', previous.get('pause_reason', ''))])

    def snapshot(self):
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup = self.root / 'backups' / f'update-{timestamp}-{self.state["id"][:8]}'
        backup.mkdir(mode=0o700, parents=True)
        for name in ('.env', '.release-files.json'):
            source = self.root / name
            if source.is_symlink():
                raise common.UpdateError('配置文件存在不安全链接')
            shutil.copy2(source, backup / name)
            (backup / name).chmod(0o600)
        (backup / 'secrets').mkdir(mode=0o700)
        for name in ('app.key', 'admin.hash'):
            source = self.root / 'secrets' / name
            if source.is_symlink():
                raise common.UpdateError('密钥文件存在不安全链接')
            shutil.copy2(source, backup / 'secrets' / name)
            (backup / 'secrets' / name).chmod(0o600)
        with contextlib.closing(self.database()) as source, contextlib.closing(sqlite3.connect(backup / 'assistant.sqlite3')) as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise common.UpdateError('备份完整性检查失败，未替换应用')
        (backup / 'assistant.sqlite3').chmod(0o600)
        with tarfile.open(backup / 'source.tar.gz', 'w:gz') as archive:
            for path, name in common.source_files(self.root):
                archive.add(path, arcname=name, recursive=False)
        self.context['backup_path'] = str(backup)
        self.context['counts'] = self.counts()
        self.save_context()
        self.state['backup_path'] = str(backup)

    def replace_sources(self, source):
        for name in common.SOURCE_ITEMS:
            destination = self.root / name
            if destination.is_symlink() or destination.parent.resolve() != self.root or not destination.resolve().is_relative_to(self.root):
                raise common.UpdateError('源码替换路径越界，已拒绝操作')
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink(missing_ok=True)
            item = source / name
            if item.is_dir():
                shutil.copytree(item, destination)
            elif item.is_file():
                shutil.copy2(item, destination)

    def healthy(self, expected_version, timeout=120):
        port = int(self.environment().get('APP_PORT', '18780'))
        if not 1 <= port <= 65535:
            raise common.UpdateError('应用端口配置不正确')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=5) as response:
                    result = json.load(response)
                if result.get('status') == 'ok' and result.get('version') == expected_version:
                    return
            except (OSError, ValueError):
                pass
            time.sleep(2)
        raise common.UpdateError('应用健康检查未通过')

    def rollback(self):
        backup = Path(self.context['backup_path'])
        if backup.parent.resolve() != (self.root / 'backups').resolve() or backup.is_symlink():
            raise common.UpdateError('回滚备份路径不合法')
        self.transition('rolling_back', '新版本未通过验证，正在恢复原源码、数据库和配置', 90)
        self.stop_app()
        stage = self.directory / 'work' / f'rollback-{uuid.uuid4().hex}'
        common.extract_source(backup / 'source.tar.gz', stage, self.state['previous_version'])
        self.replace_sources(stage)
        for name in ('.env', '.release-files.json'):
            if (self.root / name).is_symlink():
                raise common.UpdateError('回滚配置路径不安全')
            shutil.copy2(backup / name, self.root / name)
        database = self.root / 'data' / 'assistant.sqlite3'
        if database.is_symlink():
            raise common.UpdateError('回滚数据库路径不安全')
        for suffix in ('-wal', '-shm'):
            database.with_name(database.name + suffix).unlink(missing_ok=True)
        database.unlink(missing_ok=True)
        shutil.copyfile(backup / 'assistant.sqlite3', database)
        database.chmod(0o600)
        os.chown(database, 10001, 10001)
        self.compose('up', '-d', '--no-build', '--wait', '--wait-timeout', '180', 'app')
        self.healthy(self.state['previous_version'])
        self.resume()
        self.transition('rolled_back', '更新未成功，已自动恢复原版本及更新前数据；备份已保留', 100)

    def recover(self):
        if not self.state.get('id') or self.state.get('stage') in common.TERMINAL:
            return
        self.context = common.read_json(self.context_file, {})
        if self.context.get('id') != self.state['id']:
            self.transition('failed', '上次更新在初始化阶段中断，未替换应用', 100)
            return
        try:
            if self.context.get('committed'):
                self.healthy(self.state['version'])
                self.resume()
                self.transition('complete', '新版本健康检查正常，已恢复更新完成状态', 100)
            elif self.context.get('mutated'):
                self.rollback()
            else:
                if self.context.get('app_stopped'):
                    self.compose('up', '-d', '--no-build', '--wait', '--wait-timeout', '180', 'app')
                self.resume()
                self.transition('failed', '上次更新被中断，原版本和数据未被替换，可以重新检测更新', 100)
        except Exception:
            self.transition('failed', '中断恢复未完成，队列保持暂停；请根据备份人工恢复，勿删除数据或密钥', 100)

    def install(self, request, local_archive=None, manifest_path=None):
        version = request['version']
        self.state = {'id': request['id'], 'version': version, 'previous_version': (self.root / 'VERSION').read_text().strip(),
                      'started_at': time.time(), 'history': []}
        self.context = {'id': request['id'], 'mutated': False}
        self.save_context()
        self.transition('checking', '校验发布来源和本地源码改动', 5)
        work = self.directory / 'work' / request['id']
        work.mkdir(parents=True, mode=0o700, exist_ok=False)
        with (work / 'update.log').open('a', encoding='utf-8') as output:
            self.log = output
            try:
                changes = common.local_changes(self.root)
                if changes and not request['force']:
                    raise common.UpdateError(f'检测到 {len(changes)} 个本地源码改动；请备份后勾选强制更新，或先撤销改动')
                release = common.local_release(local_archive, manifest_path) if local_archive is not None else common.latest_release(request['repository'])
                if release['version'] != version:
                    raise common.UpdateError('远程版本已变化，请重新检测后确认更新')
                target, current = common.version_tuple(version), common.version_tuple(self.state['previous_version'])
                if target < current or target == current and not request['force']:
                    raise common.UpdateError('拒绝降级或未经确认的同版本覆盖')
                if shutil.disk_usage(self.root).free < 1024 * 1024 * 1024:
                    raise common.UpdateError('磁盘可用空间不足 1GB，请先释放空间；不会自动删除任何备份')
                self.transition('downloading', '读取本地源码包并校验 SHA-256' if local_archive is not None else '下载源码包并校验 SHA-256', 12)
                if local_archive is not None:
                    with Path(local_archive).open('rb') as source:
                        content = source.read(common.MAX_ARCHIVE + 1)
                else:
                    content = common.fetch_bytes(release['archive_url'], common.MAX_ARCHIVE, timeout=120)
                if len(content) != release['size'] or hashlib.sha256(content).hexdigest() != release['sha256']:
                    raise common.UpdateError('源码包大小或 SHA-256 不匹配，未执行任何发布代码')
                archive = work / 'release.tar.gz'
                archive.write_bytes(content)
                stage = work / 'source'
                common.extract_source(archive, stage, version)
                image = f'{common.APP_ID}:{version}-{request["id"][:12]}'
                self.transition('draining', '构建前暂停新任务，等待账号任务与远程备份完成', 20)
                self.pause()
                self.check_build_memory()
                self.transition('building', '任务与备份已暂停，正在构建镜像；网页可继续查看状态', 25)
                self.execute(['docker', 'build', '-t', image, str(stage)], timeout=3600)
                self.transition('backing_up', '备份源码、数据库、密钥和部署配置', 72)
                self.context['app_stopped'] = True
                self.save_context()
                self.stop_app()
                self.snapshot()
                self.context['mutated'] = True
                self.save_context()
                self.transition('restarting', '替换源码并重启本项目，页面将自动重新连接', 80)
                self.replace_sources(stage)
                self.set_image(image)
                self.compose('up', '-d', '--no-build', '--wait', '--wait-timeout', '180', 'app')
                self.transition('healthcheck', '核对版本、服务健康和账号/签到数据完整性', 93)
                self.healthy(version)
                if self.counts() != self.context['counts']:
                    raise common.UpdateError('更新前后数据数量不一致，准备回滚')
                common.atomic_json(self.root / '.release-files.json', common.source_manifest(self.root))
                self.execute(['bash', 'scripts/install-updater.sh', '--refresh-only'], timeout=60)
                self.context['committed'] = True
                self.save_context()
                self.resume()
                self.transition('complete', f'已更新到 v{version}，健康与数据检查通过', 100)
                return True
            except Exception as error:
                output.write(f'\nUpdate failed: {type(error).__name__}: {error}\n')
                output.flush()
                if self.context.get('mutated'):
                    try:
                        self.rollback()
                    except Exception as rollback_error:
                        output.write(f'Rollback failed: {type(rollback_error).__name__}\n')
                        self.transition('failed', '自动回滚未完成，队列保持暂停；请使用已保留的完整备份人工恢复', 100)
                else:
                    if self.context.get('app_stopped'):
                        self.compose('up', '-d', '--no-build', '--wait', '--wait-timeout', '180', 'app')
                    self.resume()
                    self.transition('failed', str(error) if isinstance(error, common.UpdateError) else '更新准备失败，原版本和数据未替换；请检查服务器更新日志', 100)
                return False
            finally:
                self.log = None

    def pending(self):
        path = self.directory / 'requests' / 'request.json'
        request = common.read_json(path, None, limit=4096)
        if not isinstance(request, dict):
            return False
        try:
            if set(request) != {'id', 'repository', 'version', 'force', 'requested_at'} or not re.fullmatch('[0-9a-f]{32}', request['id']) or type(request['force']) is not bool:
                raise ValueError()
            common.version_tuple(request['version'])
            if not common.repository_name(request['repository']) or not 0 <= time.time() - request['requested_at'] < 21600:
                raise ValueError()
        except (ValueError, TypeError, KeyError, common.UpdateError):
            path.unlink(missing_ok=True)
            return False
        path.unlink()
        return self.install(request)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--record-baseline', action='store_true')
    parser.add_argument('--install-archive', type=Path)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    if args.record_baseline:
        common.atomic_json(args.root / '.release-files.json', common.source_manifest(args.root))
        return
    import fcntl
    updater = Updater(args.root)
    if args.install_archive:
        if os.geteuid() != 0:
            raise SystemExit('本地升级需要管理员权限，请使用 sudo python3')
        for directory in (updater.directory, updater.directory / 'status', updater.directory / 'requests', updater.directory / 'work'):
            if directory.is_symlink() or not directory.resolve().is_relative_to(updater.root):
                raise SystemExit('更新目录包含不安全链接')
            mode = 0o755 if directory.name in ('updates', 'status') else 0o700
            directory.mkdir(mode=mode, parents=True, exist_ok=True)
            directory.chmod(mode)
        os.chown(updater.directory / 'requests', 10001, 10001)
    with (updater.directory / '.worker.lock').open('a') as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('更新执行器正在运行。手动升级前请先停止 any-signin-assistant-updater 服务，不要并行更新') from None
        stopped = threading.Event()
        def heartbeat():
            while not stopped.is_set():
                common.atomic_json(updater.directory / 'status' / 'heartbeat.json', {'protocol': 1, 'at': time.time()}, 0o644)
                stopped.wait(5)
        threading.Thread(target=heartbeat, daemon=True).start()
        try:
            if args.install_archive:
                with updater.deployment_lock() as acquired:
                    if not acquired:
                        raise SystemExit('手动部署正在执行，请等待完成，不要并行更新')
                    updater.recover()
                    if (updater.directory / 'requests' / 'request.json').exists():
                        raise SystemExit('还有网页提交的更新请求，请先处理完成后再进行手动升级')
                    manifest_path = args.manifest or args.install_archive.parent / 'release.json'
                    release = common.local_release(args.install_archive, manifest_path)
                    if not (updater.root / '.release-files.json').exists():
                        common.atomic_json(updater.root / '.release-files.json', common.source_manifest(updater.root))
                    request = {'id': uuid.uuid4().hex, 'version': release['version'], 'repository': '', 'force': args.force, 'requested_at': time.time()}
                    success = updater.install(request, local_archive=args.install_archive, manifest_path=manifest_path)
                    print(updater.state.get('message', ''), flush=True)
                    raise SystemExit(0 if success else 1)
            while True:
                with updater.deployment_lock() as acquired:
                    finished = False
                    if acquired:
                        updater.recover()
                        finished = updater.pending()
                if args.once or finished:
                    return
                time.sleep(2)
        finally:
            stopped.set()


if __name__ == '__main__':
    main()
