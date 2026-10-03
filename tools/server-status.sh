set -uo pipefail
cd /opt/any-signin-assistant 2>/dev/null || { echo 'project directory missing'; exit 1; }
printf '=== deploy-output.log (tail) ===\n'
tail -n 25 deploy-output.log 2>/dev/null
printf '\n=== deploy process ===\n'
pgrep -af 'scripts/deploy.sh' || echo 'deploy.sh finished'
printf '\n=== compose ps ===\n'
docker compose -p any-signin-assistant --profile https ps 2>/dev/null
printf '\n=== files ===\n'
ls -la . data secrets 2>/dev/null | head -40
