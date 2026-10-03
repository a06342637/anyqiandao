"""Transport policy and narrowly scoped compatibility for local health probes."""
import ipaddress
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

LOOPBACK_HOSTS = {'127.0.0.1', 'localhost', '::1'}


def loopback(value):
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def secure_transport(request, config):
    if request.url.scheme == 'https':
        return True
    public = urlsplit(config.public_url)
    # The configured HTTPS origin terminates TLS at Caddy; its backend is local.
    # Forwarded headers never turn an HTTP deployment into a trusted HTTPS one.
    if public.scheme == 'https' and request.headers.get('x-forwarded-proto', 'https') == 'https':
        return True
    peer = request.client.host if request.client else ''
    return (public.hostname in LOOPBACK_HOSTS and request.url.hostname in LOOPBACK_HOSTS
            and (loopback(peer) or peer == container_gateway()) and not any(key in request.headers for key in ('forwarded', 'x-forwarded-for')))


@lru_cache(maxsize=1)
def container_gateway():
    try:
        for line in Path('/proc/net/route').read_text().splitlines()[1:]:
            fields = line.split()
            if fields[1] == '00000000':
                return str(ipaddress.IPv4Address(bytes.fromhex(fields[2])[::-1]))
    except (OSError, ValueError, IndexError):
        pass
    return None


def local_health_probe(request):
    peer = request.client.host if request.client else ''
    return (request.url.hostname in LOOPBACK_HOSTS
            and (loopback(peer) or peer == container_gateway())
            and not any(key in request.headers for key in ('forwarded', 'x-forwarded-for', 'x-forwarded-proto')))
