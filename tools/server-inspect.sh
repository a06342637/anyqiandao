set -euo pipefail
printf '=== Storage location ===\n'
DOCKER_ROOT="$(docker info --format '{{.DockerRootDir}}')"
printf '%s\n' "$DOCKER_ROOT"
findmnt -T "$DOCKER_ROOT" -n -o SOURCE,TARGET,FSTYPE
findmnt -T /opt -n -o SOURCE,TARGET,FSTYPE
findmnt -T /home/ubuntu -n -o SOURCE,TARGET,FSTYPE
printf '=== Existing service baseline ===\n'
docker ps --format '{{.ID}} | {{.Names}} | {{.Status}} | {{.Ports}}'
ss -ltnp | grep -E ':(80|443|8443|18780|32768|32769)\b' || true
printf '=== HTTPS image availability ===\n'
curl -fsSI --max-time 30 https://mcr.microsoft.com/v2/playwright/python/manifests/v1.62.0-noble | head -n 4
printf '=== Firewall ===\n'
if command -v ufw >/dev/null; then ufw status; fi
