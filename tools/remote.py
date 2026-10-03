import argparse
import asyncio
import os
import shlex
from pathlib import Path

import asyncssh

HOST = os.environ['ANY_DEPLOY_SSH_HOST']
FINGERPRINT = os.environ['ANY_DEPLOY_SSH_FINGERPRINT']


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['run', 'upload', 'download'])
    parser.add_argument('file')
    parser.add_argument('--destination')
    args = parser.parse_args()
    key = await asyncio.wait_for(asyncssh.get_server_host_key(HOST, 22, config=None), 20)
    if key is None or key.get_fingerprint() != FINGERPRINT:
        raise SystemExit('Server fingerprint changed. Authentication refused.')
    password = os.environ.pop('ANY_DEPLOY_SSH_PASSWORD')
    async with asyncssh.connect(HOST, username='ubuntu', password=password, known_hosts=([key], [], []),
                               client_keys=[], agent_path=None, config=None, connect_timeout=20) as connection:
        if args.action == 'run':
            script = Path(args.file).read_text(encoding='utf-8')
            process = await connection.create_process('sudo -n -i bash -s', encoding='utf-8')
            process.stdin.write(script)
            process.stdin.write_eof()
            async def forward(stream):
                async for line in stream:
                    print(line, end='', flush=True)
            await asyncio.gather(forward(process.stdout), forward(process.stderr))
            await process.wait()
            if process.exit_status:
                raise SystemExit(process.exit_status)
        elif args.action == 'upload':
            destination = args.destination or '/home/ubuntu/.any-signin-assistant-upload.tar.gz'
            if destination not in ('/home/ubuntu/.any-signin-assistant-upload.tar.gz', '/home/ubuntu/.any-signin-assistant-release.json'):
                raise SystemExit('Unexpected upload target')
            async with connection.start_sftp_client() as sftp:
                await sftp.put(args.file, destination)
            print('Project release uploaded to the system filesystem.')
        else:
            if args.file not in ('/opt/any-signin-assistant/部署信息.txt', '/opt/any-signin-assistant/.verification-dist.tar.gz'):
                raise SystemExit('Unexpected download target')
            result = await connection.run('sudo -n cat -- ' + shlex.quote(args.file), check=True, encoding=None)
            destination = Path(args.destination).resolve()
            if not destination.is_relative_to(Path(__file__).resolve().parents[1]):
                raise SystemExit('Destination outside project')
            destination.write_bytes(result.stdout)
            print('Requested project artifact saved on D; file contents were not printed.')


asyncio.run(main())
