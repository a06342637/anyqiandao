set -euo pipefail
ROOT=/opt/any-signin-assistant
if [[ ! -f "$ROOT/.project-id" ]] || [[ "$(cat "$ROOT/.project-id")" != any-signin-assistant-managed-v1 ]]; then
    printf 'Managed project directory not found; refusing.\n' >&2
    exit 1
fi
STAGE="$(mktemp -d /tmp/any-signin-docs.XXXXXX)"
tar --extract --gzip --file /home/ubuntu/.any-signin-assistant-upload.tar.gz --directory "$STAGE" --no-same-owner --no-same-permissions
rm -- /home/ubuntu/.any-signin-assistant-upload.tar.gz
# Documentation and tooling only; no container is rebuilt or restarted.
for item in README.md CHANGELOG.md 使用说明.txt 验收记录.txt docs tools .dockerignore; do
    if [[ -e "$STAGE/$item" ]]; then
        rm -rf -- "$ROOT/$item"
        cp -a -- "$STAGE/$item" "$ROOT/$item"
    fi
done
rm -rf -- "$STAGE"
ls -la "$ROOT" | grep -E 'txt|md|tools|docs'
