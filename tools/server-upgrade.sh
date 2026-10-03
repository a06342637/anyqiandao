set -euo pipefail
ROOT=/opt/any-signin-assistant
if [[ ! -f "$ROOT/.project-id" ]] || [[ "$(cat "$ROOT/.project-id")" != any-signin-assistant-managed-v1 ]]; then
    printf 'Managed project directory not found; refusing.\n' >&2
    exit 1
fi
STAGE="$(mktemp -d /tmp/any-signin-upgrade.XXXXXX)"
tar --extract --gzip --file /home/ubuntu/.any-signin-assistant-upload.tar.gz --directory "$STAGE" --no-same-owner --no-same-permissions
rm -- /home/ubuntu/.any-signin-assistant-upload.tar.gz
# Only application source is replaced; .env, data/, secrets/, backups/ and 部署信息.txt stay untouched.
for item in app frontend scripts deploy docs Dockerfile compose.yml requirements.txt VERSION README.md CHANGELOG.md .dockerignore .gitignore .project-id 使用说明.txt; do
    if [[ -e "$STAGE/$item" ]]; then
        rm -rf -- "$ROOT/$item"
        cp -a -- "$STAGE/$item" "$ROOT/$item"
    fi
done
rm -rf -- "$STAGE"
cd -- "$ROOT"
chmod +x scripts/*.sh
nohup bash scripts/deploy.sh https://signin.example.com > deploy-output.log 2>&1 < /dev/null &
printf 'Upgrade started in %s with PID %s (backup runs first).\n' "$ROOT" "$!"
