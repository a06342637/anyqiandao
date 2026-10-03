set -euo pipefail
ROOT=/opt/any-signin-assistant
if [[ -d "$ROOT" ]]; then
    if [[ ! -f "$ROOT/.project-id" ]] || [[ "$(cat "$ROOT/.project-id")" != any-signin-assistant-managed-v1 ]]; then
        printf 'Unexpected existing project directory; refusing to overwrite.\n' >&2
        exit 1
    fi
fi
if [[ "$(findmnt -T /opt -n -o SOURCE)" != "$(findmnt -T / -n -o SOURCE)" ]]; then
    printf 'Project path is not on the system filesystem.\n' >&2
    exit 1
fi
install -d -m 755 -- "$ROOT"
tar --extract --gzip --file /home/ubuntu/.any-signin-assistant-upload.tar.gz --directory "$ROOT" --no-same-owner --no-same-permissions
rm -- /home/ubuntu/.any-signin-assistant-upload.tar.gz
cd -- "$ROOT"
chmod +x scripts/*.sh
nohup bash scripts/deploy.sh https://signin.example.com > deploy-output.log 2>&1 < /dev/null &
printf 'Isolated deployment started in %s with PID %s.\n' "$ROOT" "$!"
