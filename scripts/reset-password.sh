#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$ROOT"
VERSION="$(tr -d '\r\n' < VERSION)"
docker run --rm -it --entrypoint python --mount "type=bind,src=$ROOT,dst=/workspace" \
    -e APP_SECRET_KEY_FILE=/workspace/secrets/app.key \
    -e APP_ADMIN_PASSWORD_HASH_FILE=/workspace/secrets/admin.hash \
    -e APP_DATA_DIR=/workspace/data "any-signin-assistant:$VERSION" -m app.manage reset-password
docker compose -p any-signin-assistant restart app
printf '请同时更新或移除 部署信息.txt 中的旧密码。\n'
