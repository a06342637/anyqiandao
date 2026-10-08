"""Cloudflare Tunnel and reverse proxy checks without contacting a real server."""
import json
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx

from app.config import Config
from app.main import create_app
from app.security import canonical_origin, proxy_sources
from tools import verify_remote_backup as fixtures


class ProxyTLSClient(httpx.AsyncBaseTransport):
    """Browser connects using HTTPS, but the app receives HTTP from cloudflared."""
    def __init__(self, app, peer, headers):
        self.transport = httpx.ASGITransport(app=app, client=(peer, 12345))
        self.headers = headers

    async def handle_async_request(self, request):
        forwarded = httpx.Request(request.method, request.url.copy_with(scheme='http'),
                                  headers=request.headers, content=await request.aread())
        forwarded.headers.update(self.headers)
        return await self.transport.handle_async_request(forwarded)

    async def aclose(self):
        await self.transport.aclose()


class ReverseProxyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.BackupTests.asyncSetUp
    asyncTearDown = fixtures.BackupTests.asyncTearDown

    def application(self, *, public_url='http://127.0.0.1:8001', proxies=None):
        extra = {'trusted_proxy_ips': proxies} if proxies is not None else {}
        return create_app(Config(self.config.key, self.config.admin_hash, self.root / 'tunnel-data',
                                 public_url, start_worker=False, **extra))

    async def exercise_tunnel(self, peer, headers, **kwargs):
        app = self.application(**kwargs)
        async with httpx.AsyncClient(transport=ProxyTLSClient(app, peer, headers), base_url='https://signin.example.invalid') as client:
            page = await client.get('/')
            self.assertEqual(page.status_code, 200, page.text[:100])
            self.assertIn('id="login"', page.text)
            self.assertIn('max-age=31536000', page.headers['strict-transport-security'])
            self.assertEqual((await client.get('/api/v1/branding')).status_code, 401)
            login = await client.post('/api/v1/auth/login', json={'password': 'synthetic-admin-password'},
                                      headers={'Origin': 'https://signin.example.invalid', 'Sec-Fetch-Site': 'same-origin'})
            self.assertEqual(login.status_code, 200, login.text)
            self.assertIn('secure', {part.strip().lower() for part in login.headers['set-cookie'].split(';')[1:]})
            client.headers.update({'Origin': 'https://signin.example.invalid', 'X-CSRF-Token': login.json()['csrf_token']})
            self.assertEqual((await client.get('/api/v1/auth/me')).status_code, 200)
            self.assertEqual((await client.put('/api/v1/settings', json={'site_name': 'synthetic-tunnel'})).status_code, 200)
            self.assertEqual((await client.get('/api/v1/accounts', headers={'Origin': 'https://evil.invalid'})).status_code, 403)
            self.assertEqual((await client.get('/api/v1/accounts', headers={'Origin': 'null'})).status_code, 403)
            logged_out = await client.post('/api/v1/auth/logout')
            self.assertEqual(logged_out.status_code, 200)
            self.assertIn('secure', {part.strip().lower() for part in logged_out.headers['set-cookie'].split(';')[1:]})
            self.assertEqual((await client.get('/api/v1/accounts')).status_code, 401)

    async def test_cloudflare_host_tunnel_with_loopback_public_url(self):
        await self.exercise_tunnel('127.0.0.1', {'X-Forwarded-Proto': 'https', 'CF-Visitor': '{"scheme":"https"}', 'X-Forwarded-For': '198.51.100.25'})

    async def test_cloudflare_through_docker_host_gateway(self):
        with patch('app.security.container_gateway', return_value='172.31.0.1'):
            await self.exercise_tunnel('172.31.0.1', {'X-Forwarded-Proto': 'https', 'CF-Visitor': '{"scheme":"https"}'})

    async def test_cloudflare_visitor_header_fallback(self):
        await self.exercise_tunnel('127.0.0.1', {'CF-Visitor': '{"scheme":"https"}'})

    async def test_generic_proxy_header_and_stale_configured_domain(self):
        await self.exercise_tunnel('127.0.0.1', {'X-Forwarded-Proto': 'https'}, public_url='https://old.example.invalid')

    async def test_explicit_sidecar_proxy(self):
        await self.exercise_tunnel('10.12.0.5', {'X-Forwarded-Proto': 'https'}, proxies=('10.12.0.5/32',))

    async def test_untrusted_peer_cannot_spoof_tls_or_peer_identity(self):
        app = self.application()
        with patch('app.security.container_gateway', return_value='172.31.0.1'):
            for peer in ('198.51.100.25', '10.12.0.6'):
                for headers in ({'X-Forwarded-Proto': 'https'}, {'CF-Visitor': '{"scheme":"https"}'},
                                {'X-Forwarded-Proto': 'https', 'X-Forwarded-For': '127.0.0.1', 'CF-Connecting-IP': '172.31.0.1'}):
                    async with httpx.AsyncClient(transport=ProxyTLSClient(app, peer, headers), base_url='https://signin.example.invalid') as client:
                        self.assertEqual((await client.get('/')).status_code, 426)
                        self.assertEqual((await client.post('/api/v1/auth/login', json={'password': 'synthetic-admin-password'})).status_code, 426)

    async def test_malformed_conflicting_and_http_headers_fail_closed(self):
        app = self.application()
        for headers in ({'X-Forwarded-Proto': 'https,http'}, {'X-Forwarded-Proto': 'http'}, {'CF-Visitor': 'not-json'},
                        {'CF-Visitor': '["https"]'}, {'CF-Visitor': '{"scheme":"ftp"}'},
                        {'X-Forwarded-Proto': 'https', 'CF-Visitor': '{"scheme":"http"}'}):
            async with httpx.AsyncClient(transport=ProxyTLSClient(app, '127.0.0.1', headers), base_url='https://signin.example.invalid') as client:
                response = await client.get('/')
                self.assertEqual(response.status_code, 426)
                self.assertNotIn('<form', response.text)

    async def test_forwarded_host_does_not_change_expected_origin(self):
        app = self.application()
        headers = {'X-Forwarded-Proto': 'https', 'X-Forwarded-Host': 'evil.invalid'}
        async with httpx.AsyncClient(transport=ProxyTLSClient(app, '127.0.0.1', headers), base_url='https://signin.example.invalid') as client:
            response = await client.post('/api/v1/auth/login', json={'password': 'synthetic-admin-password'}, headers={'Origin': 'https://evil.invalid'})
            self.assertEqual(response.status_code, 403)
            response = await client.post('/api/v1/auth/login', json={'password': 'synthetic-admin-password'}, headers={'Origin': 'https://signin.example.invalid'})
            self.assertEqual(response.status_code, 200)

    async def test_proxy_trust_can_be_disabled(self):
        app = self.application(proxies=())
        async with httpx.AsyncClient(transport=ProxyTLSClient(app, '127.0.0.1', {'X-Forwarded-Proto': 'https'}), base_url='https://signin.example.invalid') as client:
            self.assertEqual((await client.get('/')).status_code, 426)

    async def test_local_probe_stays_compatible_and_tunnel_hides_metadata(self):
        app = self.application()
        with patch('app.security.container_gateway', return_value='172.31.0.1'):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('172.31.0.1', 12345)), base_url='http://127.0.0.1:8001') as client:
                self.assertEqual((await client.get('/healthz')).json()['version'], app.version)
                self.assertEqual((await client.get('/healthz', headers={'CF-Visitor': '{"scheme":"https"}'})).json(), {'status': 'ok'})


class OriginTests(unittest.TestCase):
    def test_canonical_origins_and_invalid_values(self):
        self.assertEqual(canonical_origin('https://EXAMPLE.COM:443/'), 'https://example.com')
        self.assertEqual(canonical_origin('https://example.com:8443'), 'https://example.com:8443')
        self.assertEqual(canonical_origin('http://[::1]:8001'), 'http://[::1]:8001')
        for value in ('null', 'https://example.com/path', 'https://a@evil.invalid', 'https://a:bad', 'https://a,b', 'https://a?x=1'):
            self.assertIsNone(canonical_origin(value))

    def test_proxy_allowlist_validation(self):
        self.assertEqual(proxy_sources('127.0.0.1,::1,gateway'), ('127.0.0.1', '::1', 'gateway'))
        self.assertEqual(proxy_sources('none'), ())
        for value in ('*', '0.0.0.0/0', '::/0', 'not-an-ip'):
            with self.assertRaises(ValueError):
                proxy_sources(value)


if __name__ == '__main__':
    unittest.main(verbosity=2)
