"""Time each step of validate() for a stored credential; wraps BrowserSession methods with stopwatches."""
import asyncio
import functools
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.argv.append('--edge')

import tools.verify_real_account as harness  # noqa: E402  (installs the Edge substitution)
import app.router_service as router_service  # noqa: E402
from app.schemas import RuntimeSettings  # noqa: E402

started = time.monotonic()


def timed(name, function):
    @functools.wraps(function)
    async def wrapper(*args, **kwargs):
        begin = time.monotonic()
        try:
            return await function(*args, **kwargs)
        finally:
            print(f'{time.monotonic() - started:6.1f}s  {name} took {time.monotonic() - begin:.1f}s', flush=True)
    return wrapper


for name in ('__aenter__', '__aexit__', 'open', 'settle', 'api', 'cookies'):
    setattr(router_service.BrowserSession, name, timed(name, getattr(router_service.BrowserSession, name)))


async def main():
    username, password = sys.argv[1], sys.argv[2]
    service = router_service.RouterService()
    settings = RuntimeSettings()
    result = await service.extract(username, password, None, settings)
    print(f'{time.monotonic() - started:6.1f}s  extract done', flush=True)
    await service.validate({'session': result['session'], 'api_user': result['api_user']}, None, settings)
    print(f'{time.monotonic() - started:6.1f}s  validate done', flush=True)


asyncio.run(main())
