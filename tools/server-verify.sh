set -uo pipefail
cd /opt/any-signin-assistant
printf '=== all containers (existing ones must be untouched) ===\n'
docker ps --format '{{.Names}} | {{.Status}} | {{.Ports}}'
printf '\n=== deploy log: errors/warnings ===\n'
grep -iE 'error|warn|fail' deploy-output.log | grep -viE 'no-cache|--no-cache' | head -10 || true
printf '\n=== deploy log: last lines ===\n'
tail -n 6 deploy-output.log
printf '\n=== disk ===\n'
df -h / | tail -1
docker system df | head -3
printf '\n=== chromium launch inside the hardened app container ===\n'
docker compose -p any-signin-assistant exec -T app python - <<'EOF'
import asyncio
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--disable-quic'])
        context = await browser.new_context(viewport={'width': 800, 'height': 600})
        page = await context.new_page()
        await page.set_content('<title>ok</title><h1>hello</h1>')
        print('chromium version:', browser.version, '| title:', await page.title())
        await browser.close()
asyncio.run(main())
EOF
printf '\n=== outbound reachability from the app container ===\n'
docker compose -p any-signin-assistant exec -T app python -c "import httpx; r = httpx.get('https://anyrouter.top/login', timeout=20, trust_env=False); print('anyrouter.top/login ->', r.status_code)"
