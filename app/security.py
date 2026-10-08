"""Transport policy and narrowly scoped compatibility for local health probes."""
import ipaddress
import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

LOOPBACK_HOSTS = {'127.0.0.1', 'localhost', '::1'}
DEFAULT_TRUSTED_PROXIES = ('127.0.0.1', '::1', 'gateway')


def proxy_sources(value):
    if value.strip().lower() in ('', 'none'):
        return ()
    entries = tuple(dict.fromkeys(item.strip() for item in value.split(',') if item.strip()))
    try:
        if len(entries) > 64:
            raise ValueError()
        for entry in entries:
            if entry != 'gateway' and ipaddress.ip_network(entry, strict=False).prefixlen == 0:
                raise ValueError()
    except ValueError:
        raise ValueError('可信代理仅支持具体 IP、有限 CIDR 或 gateway，不支持通配所有来源') from None
    return entries


def canonical_origin(value):
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if (parsed.scheme not in ('http', 'https') or not host or parsed.username or parsed.password
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            return None
        try:
            address = ipaddress.ip_address(host)
            host = '[' + address.compressed + ']' if address.version == 6 else str(address)
        except ValueError:
            host = host.encode('idna').decode('ascii').lower()
            if len(host) > 253 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host):
                return None
        if port is not None and not 1 <= port <= 65535:
            return None
        suffix = '' if port in (None, 443 if parsed.scheme == 'https' else 80) else f':{port}'
        return f'{parsed.scheme}://{host}{suffix}'
    except (ValueError, UnicodeError):
        return None


def trusted_proxy(request, config):
    # Use the real TCP peer, never CF-Connecting-IP / X-Forwarded-For.
    try:
        peer = ipaddress.ip_address(request.client.host if request.client else '')
    except ValueError:
        return False
    for entry in config.trusted_proxy_ips:
        if entry == 'gateway':
            if str(peer) == container_gateway():
                return True
        elif peer in ipaddress.ip_network(entry, strict=False):
            return True
    return False


def forwarded_scheme(request, config):
    if not trusted_proxy(request, config):
        return None
    schemes = []
    forwarded = request.headers.getlist('x-forwarded-proto')
    if forwarded:
        if len(forwarded) != 1 or forwarded[0].strip().lower() not in ('http', 'https'):
            return 'invalid'
        schemes.append(forwarded[0].strip().lower())
    visitors = request.headers.getlist('cf-visitor')
    if visitors:
        try:
            if len(visitors) != 1 or len(visitors[0]) > 1024:
                return 'invalid'
            visitor = json.loads(visitors[0])
            scheme = visitor.get('scheme') if isinstance(visitor, dict) else None
            if scheme not in ('http', 'https'):
                return 'invalid'
            schemes.append(scheme)
        except (ValueError, TypeError):
            return 'invalid'
    return schemes[0] if schemes and len(set(schemes)) == 1 else 'invalid' if schemes else None


def request_origin(request, config):
    """One origin shared by HTTPS checks, CSRF validation and cookie attributes."""
    authority = request.url.netloc
    public = canonical_origin(config.public_url)
    if request.url.scheme == 'https':
        return canonical_origin('https://' + authority)
    scheme = forwarded_scheme(request, config)
    if scheme == 'invalid':
        return None
    if scheme:
        # Tunnel's default preserves Host. Do not trust caller-supplied Origin
        # or X-Forwarded-Host to decide which origin should be accepted.
        if request.url.hostname in LOOPBACK_HOSTS and (public or '').startswith('https://'):
            return public if scheme == 'https' else canonical_origin('http://' + authority)
        return canonical_origin(scheme + '://' + authority)
    # Preserve explicitly configured HTTPS deployments (including built-in Caddy).
    if (public or '').startswith('https://'):
        if request.headers.get('x-forwarded-proto', 'https').strip().lower() != 'https':
            return None
        return public
    return canonical_origin(str(request.base_url))


def uses_https(request, config):
    return (request_origin(request, config) or '').startswith('https://')


def loopback(value):
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def secure_transport(request, config):
    if uses_https(request, config):
        return True
    public = urlsplit(config.public_url)
    peer = request.client.host if request.client else ''
    return (public.hostname in LOOPBACK_HOSTS and request.url.hostname in LOOPBACK_HOSTS
            and (loopback(peer) or peer == container_gateway()) and not any(key in request.headers for key in ('forwarded', 'x-forwarded-for', 'x-forwarded-proto', 'cf-visitor')))


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
            and not any(key in request.headers for key in ('forwarded', 'x-forwarded-for', 'x-forwarded-proto', 'cf-visitor')))
