import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import deploy_config
from scripts.update_runner import Updater


class ConfigurationTests(unittest.TestCase):
    def test_default_port_is_available_and_nonzero(self):
        port = deploy_config.select_port('')
        self.assertGreater(port, 1023)
        self.assertLessEqual(port, 65535)
        with socket.socket() as listener:
            listener.bind(('0.0.0.0', port))

    def test_custom_available_port_is_preserved(self):
        port = deploy_config.select_port('')
        self.assertEqual(deploy_config.select_port(str(port)), port)

    def test_occupied_port_is_rejected(self):
        with socket.socket() as listener:
            listener.bind(('0.0.0.0', 0))
            with self.assertRaises(OSError):
                deploy_config.select_port(str(listener.getsockname()[1]))

    def test_invalid_ports_are_rejected(self):
        for value in ('0', '-1', '65536', 'abc', '12.3', '+123', ' 123', '１２３', '123\n'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                deploy_config.select_port(value)

    def test_https_does_not_reserve_its_public_port_for_the_app(self):
        port = deploy_config.select_port('', 'https://example.com')
        self.assertNotIn(port, (80, 443))
        for value in ('80', '443'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                deploy_config.select_port(value, 'https://example.com')

    def test_http_url_and_listening_port_must_agree(self):
        port = deploy_config.select_port('')
        url = f'http://127.0.0.1:{port}'
        self.assertEqual(deploy_config.select_port('', url), port)
        with self.assertRaises(ValueError):
            deploy_config.select_port(str(port + 1), url)

    def test_public_url_rejects_paths_injection_and_invalid_origins(self):
        for value in ('ftp://example.com', 'https://user:secret@example.com', 'https://example.com/path',
                      'https://example.com?query=1', 'https://example.com#fragment', 'https://example.com:8443',
                      'http://example.com:0', 'https://example.com\nAPP_PORT=80', 'http://${HOST}:123'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                deploy_config.public_url(value)
        self.assertIsNone(deploy_config.public_url(''))
        self.assertEqual(deploy_config.public_url('https://example.com/').hostname, 'example.com')

    def test_username_validation(self):
        for value in ('admin', 'alice@example.com', 'A_user-1.2'):
            self.assertEqual(deploy_config.username(value), value)
        for value in ('', '-admin', 'has space', 'name\nAPP_IMAGE=other', 'a' * 65):
            with self.subTest(value=value), self.assertRaises(ValueError):
                deploy_config.username(value)

    def test_explicit_address_avoids_network(self):
        with patch.object(deploy_config.urllib.request, 'urlopen') as request:
            self.assertEqual(deploy_config.server_address('198.51.100.20'), '198.51.100.20')
            request.assert_not_called()

    def test_public_address_failure_has_a_controlled_fallback(self):
        with patch.object(deploy_config.urllib.request, 'urlopen', side_effect=OSError('offline')):
            with patch.object(deploy_config.subprocess, 'check_output', return_value='10.0.0.2 2001:db8::1'):
                with redirect_stderr(io.StringIO()) as output:
                    self.assertEqual(deploy_config.server_address(''), '10.0.0.2')
                self.assertIn('APP_INSTALL_HOST', output.getvalue())


class InitializationTests(unittest.TestCase):
    def test_enter_password_generates_secret_and_refuses_reinitialization(self):
        from argon2 import PasswordHasher
        from app.manage import initialize
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = SimpleNamespace(root=root, public_url='http://198.51.100.20:23456', allow_http=True,
                                   port=23456, username='admin', owner=None, password_stdin=True,
                                   bind_host='0.0.0.0', https_proxy=False)
            with patch('sys.stdin', io.StringIO('')), redirect_stdout(io.StringIO()) as output:
                initialize(args)
            information = (root / '部署信息.txt').read_text(encoding='utf-8')
            password = next(line.split('：', 1)[1] for line in information.splitlines() if line.startswith('管理员密码：'))
            self.assertGreaterEqual(len(password), 24)
            self.assertNotIn(password, output.getvalue())
            self.assertTrue(PasswordHasher().verify((root / 'secrets/admin.hash').read_text().strip(), password))
            original_key = (root / 'secrets/app.key').read_bytes()
            with patch('sys.stdin', io.StringIO('')), self.assertRaises(RuntimeError):
                initialize(args)
            self.assertEqual((root / 'secrets/app.key').read_bytes(), original_key)


@unittest.skipIf(os.name == 'nt', 'POSIX deployment lock is verified on Linux')
class DeploymentLockTests(unittest.TestCase):
    def test_deployment_lock_is_shared_and_released(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / '.project-id').write_text('any-signin-assistant-managed-v1')
            (root / 'updates').mkdir()
            updater = Updater(root)
            with updater.deployment_lock() as acquired:
                self.assertTrue(acquired)
                with Updater(root).deployment_lock() as concurrent:
                    self.assertFalse(concurrent)
            with updater.deployment_lock() as acquired:
                self.assertTrue(acquired)


DOCKER_STUB = '''#!/usr/bin/env python3
import json, os, pathlib, sys
root = pathlib.Path.cwd()
arguments = sys.argv[1:]
with (root / 'commands.jsonl').open('a') as output:
    output.write(json.dumps(arguments) + '\\n')
failure = os.environ.get('MOCK_FAILURE', '')
if arguments[0] == 'context':
    print('default')
elif arguments[0] == 'ps':
    print('123456abcdef' if os.environ.get('MOCK_MANAGED_ROOT') else '')
elif arguments[0] == 'inspect':
    print(os.environ['MOCK_MANAGED_ROOT'])
elif arguments[0] == 'build' and failure == 'build':
    sys.exit(31)
elif arguments[0] == 'run':
    password = sys.stdin.read()
    (root / 'received-password').write_text(password)
    def argument(name):
        return arguments[arguments.index(name) + 1]
    values = {'APP_PUBLIC_URL': argument('--public-url'), 'APP_PORT': argument('--port'),
              'APP_ADMIN_USERNAME': argument('--username'), 'APP_IMAGE': 'old:test',
              'APP_ENABLE_HTTPS_PROXY': '1' if '--https-proxy' in arguments else '0'}
    (root / '.env').write_text(''.join(f'{key}={value}\\n' for key, value in values.items()))
    (root / 'secrets').mkdir()
    (root / 'secrets/app.key').write_text('synthetic-key')
    (root / 'secrets/admin.hash').write_text('synthetic-hash')
elif arguments[0] == 'compose' and 'up' in arguments and failure == 'health':
    sys.exit(32)
'''


@unittest.skipUnless(os.name != 'nt' and getattr(os, 'geteuid', lambda: -1)() == 0,
                     'Bash integration runs in an isolated Linux container as root')
class BashDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='assistant-deploy-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'scripts').mkdir()
        (self.root / 'bin').mkdir()
        for name in ('deploy.sh', 'deploy_config.py'):
            shutil.copy2(ROOT / 'scripts' / name, self.root / 'scripts' / name)
        (self.root / '.project-id').write_text('any-signin-assistant-managed-v1')
        (self.root / 'VERSION').write_text('0.3.7')
        self.write('scripts/install-runtime.sh', 'printf "runtime\\n" >> steps\n')
        self.write('scripts/install-updater.sh', 'mkdir -p updates/status updates/requests updates/work\nprintf "updater:%s\\n" "$1" >> steps\n')
        self.write('scripts/backup.sh', '''python3 - <<'PY'
import sqlite3
with sqlite3.connect('data/assistant.sqlite3') as connection:
    connection.execute("UPDATE meta SET value='1' WHERE key='queue_paused'")
    connection.execute("UPDATE meta SET value='backup' WHERE key='pause_reason'")
PY
printf 'backup\\n' >> steps
if [[ "${MOCK_FAILURE:-}" == backup ]]; then exit 33; fi
''')
        self.write('bin/docker', DOCKER_STUB)
        self.write('bin/systemctl', '#!/usr/bin/env bash\nexit 0\n')
        self.environment = os.environ.copy()
        for name in list(self.environment):
            if name.startswith('APP_INSTALL_') or name in ('APP_IMAGE', 'DOCKER_HOST', 'DOCKER_CONTEXT'):
                self.environment.pop(name)
        self.environment.update(PATH=str(self.root / 'bin') + os.pathsep + self.environment['PATH'],
                                APP_INSTALL_HOST='198.51.100.20')

    def write(self, name, value):
        path = self.root / name
        path.write_text(value, encoding='utf-8')
        path.chmod(0o755)
        return path

    def execute(self):
        return subprocess.run(['bash', 'scripts/deploy.sh'], cwd=self.root, env=self.environment,
                              input='', text=True, capture_output=True, timeout=30)

    def configuration(self):
        return dict(line.split('=', 1) for line in (self.root / '.env').read_text().splitlines())

    def existing(self):
        self.write('.env', 'APP_PUBLIC_URL=https://existing.example\nAPP_PORT=23456\nAPP_ADMIN_USERNAME=owner\nAPP_ENABLE_HTTPS_PROXY=1\nAPP_IMAGE=old:retained\n')
        (self.root / 'secrets').mkdir()
        self.write('secrets/app.key', 'retained-key')
        self.write('secrets/admin.hash', 'retained-password-hash')
        (self.root / 'data').mkdir()
        with sqlite3.connect(self.root / 'data/assistant.sqlite3') as connection:
            connection.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)')
            connection.executemany('INSERT INTO meta VALUES (?,?)', [('queue_paused', '0'), ('pause_reason', 'original')])

    def queue(self):
        with sqlite3.connect(self.root / 'data/assistant.sqlite3') as connection:
            return dict(connection.execute('SELECT key,value FROM meta'))

    def interactive(self, responses):
        import pty
        import select
        master, slave = pty.openpty()
        process = subprocess.Popen(['bash', 'scripts/deploy.sh'], cwd=self.root, env=self.environment,
                                   stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        output = b''
        consumed = 0
        deadline = time.monotonic() + 30
        try:
            for prompt, answer in responses:
                expected = prompt.encode()
                while expected not in output[consumed:]:
                    self.assertLess(time.monotonic(), deadline, output.decode(errors='replace'))
                    if select.select([master], [], [], 1)[0]:
                        output += os.read(master, 65536)
                consumed = output.index(expected, consumed) + len(expected)
                os.write(master, (answer + '\n').encode())
            while process.poll() is None:
                self.assertLess(time.monotonic(), deadline, output.decode(errors='replace'))
                if select.select([master], [], [], 0.2)[0]:
                    try:
                        output += os.read(master, 65536)
                    except OSError:
                        break
            return process.wait(timeout=5), output.decode(errors='replace')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)

    def test_noninteractive_defaults(self):
        result = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.configuration()['APP_ADMIN_USERNAME'], 'admin')
        self.assertGreater(int(self.configuration()['APP_PORT']), 1023)
        self.assertEqual((self.root / 'received-password').read_text(), '')

    def test_installed_runtime_is_reused_without_package_changes(self):
        shutil.copy2(ROOT / 'scripts/install-runtime.sh', self.root / 'scripts/install-runtime.sh')
        self.write('bin/apt-get', '#!/usr/bin/env bash\ntouch unexpected-package-install\nexit 99\n')
        result = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / 'unexpected-package-install').exists())

    def test_remote_docker_host_is_rejected(self):
        shutil.copy2(ROOT / 'scripts/install-runtime.sh', self.root / 'scripts/install-runtime.sh')
        self.environment['DOCKER_HOST'] = 'tcp://remote.invalid:2375'
        result = self.execute()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('DOCKER_HOST', result.stderr)
        self.assertFalse((self.root / '.env').exists())

    def test_three_enter_keys_use_defaults(self):
        result, output = self.interactive([('空闲端口）：', ''), ('默认 admin）：', ''), ('1024 位）：', '')])
        self.assertEqual(result, 0, output)
        self.assertEqual(self.configuration()['APP_ADMIN_USERNAME'], 'admin')
        self.assertEqual((self.root / 'received-password').read_text(), '')

    def test_invalid_interactive_values_are_reprompted(self):
        password = 'synthetic-password-123'
        result, output = self.interactive([('空闲端口）：', 'invalid'), ('空闲端口）：', ''),
                                          ('默认 admin）：', 'bad name'), ('默认 admin）：', 'operator'),
                                          ('1024 位）：', 'short'), ('1024 位）：', password)])
        self.assertEqual(result, 0, output)
        self.assertEqual(self.configuration()['APP_ADMIN_USERNAME'], 'operator')
        self.assertEqual((self.root / 'received-password').read_text(), password)
        self.assertNotIn(password, (self.root / 'commands.jsonl').read_text())

    def test_incomplete_configuration_never_initializes(self):
        (self.root / 'secrets').mkdir()
        self.write('secrets/admin.hash', 'retained')
        result = self.execute()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / '.env').exists())
        self.assertEqual((self.root / 'secrets/admin.hash').read_text(), 'retained')

    def test_other_install_directory_is_not_taken_over(self):
        self.environment['MOCK_MANAGED_ROOT'] = '/different/project'
        result = self.execute()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('原部署目录', result.stderr)
        self.assertFalse((self.root / '.env').exists())

    def test_busy_deployment_lock_blocks_before_build(self):
        import fcntl
        (self.root / 'updates').mkdir()
        with (self.root / 'updates/.deploy.lock').open('a') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.execute()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('正在进行', result.stderr)
        self.assertFalse((self.root / '.env').exists())

    def test_pending_online_update_blocks_manual_deployment(self):
        self.existing()
        (self.root / 'updates/requests').mkdir(parents=True)
        self.write('updates/requests/request.json', '{}')
        result = self.execute()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('待执行', result.stderr)
        self.assertEqual(self.configuration()['APP_IMAGE'], 'old:retained')

    def test_existing_configuration_secrets_and_queue_are_preserved(self):
        self.existing()
        self.environment.update(APP_INSTALL_USERNAME='replacement', APP_INSTALL_PASSWORD='do-not-use-this-password')
        result = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.configuration()['APP_ADMIN_USERNAME'], 'owner')
        self.assertEqual(self.configuration()['APP_PORT'], '23456')
        self.assertEqual((self.root / 'secrets/app.key').read_text(), 'retained-key')
        self.assertEqual((self.root / 'secrets/admin.hash').read_text(), 'retained-password-hash')
        self.assertEqual(self.queue(), {'queue_paused': '0', 'pause_reason': 'original'})
        self.assertFalse((self.root / 'received-password').exists())

    def test_build_failure_preserves_configuration_and_queue(self):
        self.existing()
        self.environment['MOCK_FAILURE'] = 'build'
        result = self.execute()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.configuration()['APP_IMAGE'], 'old:retained')
        self.assertEqual(self.queue()['queue_paused'], '0')

    def test_backup_failure_restores_original_queue(self):
        self.existing()
        self.environment['MOCK_FAILURE'] = 'backup'
        result = self.execute()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.configuration()['APP_IMAGE'], 'old:retained')
        self.assertEqual(self.queue(), {'queue_paused': '0', 'pause_reason': 'original'})

    def test_unhealthy_replacement_keeps_queue_paused(self):
        self.existing()
        self.environment['MOCK_FAILURE'] = 'health'
        result = self.execute()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.queue()['queue_paused'], '1')
        self.assertIn('队列保持暂停', result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
