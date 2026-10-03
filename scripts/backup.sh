#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$ROOT"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$"
SNAPSHOT="$ROOT/backups/$STAMP"
umask 077
docker compose -p any-signin-assistant exec -T app python -m app.manage pause
printf '队列已暂停，正在等待当前任务结束（最多十分钟）。\n'
for ((attempt = 0; attempt < 300; attempt++)); do
    RUNNING="$(python3 - <<'PY'
import sqlite3
with sqlite3.connect('data/assistant.sqlite3', timeout=30) as connection:
    print(connection.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0])
PY
)"
    if [[ "$RUNNING" == 0 ]]; then break; fi
    sleep 2
done
if [[ "$RUNNING" != 0 ]]; then printf '任务仍未结束，停止备份，不强行中断任务。\n' >&2; exit 1; fi
docker compose -p any-signin-assistant exec -T app python -m app.manage backup --output "/data/.backup-$STAMP.sqlite3"
mkdir -p -- "$SNAPSHOT"
install -m 600 -- "$ROOT/data/.backup-$STAMP.sqlite3" "$SNAPSHOT/assistant.sqlite3"
install -m 600 -- "$ROOT/.env" "$SNAPSHOT/app.env"
install -d -m 700 -- "$SNAPSHOT/secrets"
install -m 600 -- "$ROOT/secrets/app.key" "$SNAPSHOT/secrets/app.key"
install -m 600 -- "$ROOT/secrets/admin.hash" "$SNAPSHOT/secrets/admin.hash"
cp -- VERSION compose.yml "$SNAPSHOT/"
RUNNING_IMAGE="$(docker compose -p any-signin-assistant images -q app | head -n 1)"
if [[ -n "$RUNNING_IMAGE" ]]; then
    docker image tag "$RUNNING_IMAGE" "any-signin-assistant:rollback-$STAMP"
    printf 'any-signin-assistant:rollback-%s\n' "$STAMP" > "$SNAPSHOT/rollback-image.txt"
fi
rm -- "$ROOT/data/.backup-$STAMP.sqlite3"
printf '完整数据备份：%s\n包含数据库、原密钥、管理密码哈希和配置，务必私密保存；本备份命令不自动恢复队列。\n' "$SNAPSHOT"
