#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "$ROOT"
if [[ "$ROOT" == / || "$EUID" -ne 0 || ! -f .project-id || "$(cat .project-id)" != any-signin-assistant-managed-v1 ]]; then
    printf '请在项目目录执行 sudo bash scripts/reset-password.sh。\n' >&2
    exit 1
fi
if [[ ! -t 0 ]]; then
    printf '请在交互式 SSH 终端运行本脚本，不能通过管道读取新密码。\n' >&2
    exit 1
fi
for path in .env secrets secrets/app.key secrets/admin.hash data data/assistant.sqlite3 updates updates/.deploy.lock; do
    if [[ -L "$path" ]]; then printf '配置或数据路径不能是符号链接。\n' >&2; exit 1; fi
done
for path in .env secrets/app.key secrets/admin.hash data/assistant.sqlite3; do
    if [[ ! -f "$path" ]]; then printf '已有配置不完整，请恢复原文件后再重置。\n' >&2; exit 1; fi
done
install -d -m 0755 updates
exec 9> updates/.deploy.lock
if ! flock -n 9; then printf '部署或更新正在执行，请稍后重置密码。\n' >&2; exit 1; fi
CONTAINER="$(docker compose -p any-signin-assistant ps -a -q app)"
if [[ -z "$CONTAINER" || "$CONTAINER" == *$'\n'* ]]; then printf '未找到唯一的本项目 app 容器，请先完成部署。\n' >&2; exit 1; fi
MANAGED_ROOT="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$CONTAINER")"
if [[ "$(realpath -m -- "$MANAGED_ROOT")" != "$ROOT" ]]; then printf '容器不属于当前部署目录，已停止。\n' >&2; exit 1; fi
IMAGE_ID="$(docker inspect --format '{{.Image}}' "$CONTAINER")"
IMAGE_REF="$(docker inspect --format '{{.Config.Image}}' "$CONTAINER")"
PASSWORD=''
CONFIRM=''
APP_STOPPED=0
finish() {
    result=$?
    trap - EXIT
    unset PASSWORD CONFIRM
    if [[ "$APP_STOPPED" == 1 ]]; then
        if ! APP_IMAGE="$IMAGE_REF" docker compose -p any-signin-assistant up -d --no-build --force-recreate --wait --wait-timeout 180 app; then
            printf '应用未恢复健康，请检查本项目容器日志；原密钥和账号数据保留。\n' >&2
            result=1
        fi
    fi
    exit "$result"
}
trap finish EXIT
trap 'printf "\n已取消密码输入。\n" >&2; exit 130' INT
trap 'exit 143' TERM
while true; do
    if ! IFS= read -r -s -p '输入新的管理员密码（5–1024 字符，不回显）：' PASSWORD; then printf '\n未读取到密码，已取消。\n' >&2; exit 1; fi
    printf '\n'
    if (( ${#PASSWORD} < 5 || ${#PASSWORD} > 1024 )); then printf '密码需要 5–1024 个字符，请重新输入。\n'; continue; fi
    if ! IFS= read -r -s -p '再次输入新密码：' CONFIRM; then printf '\n未读取到确认密码，已取消。\n' >&2; exit 1; fi
    printf '\n'
    if [[ "$PASSWORD" == "$CONFIRM" ]]; then break; fi
    printf '两次输入不一致，请重新输入。\n'
done
APP_STOPPED=1
docker compose -p any-signin-assistant stop --timeout 90 app
printf '%s' "$PASSWORD" | docker run --rm -i --user 10001:10001 --network none --entrypoint python \
    --mount "type=bind,src=$ROOT/secrets,dst=/reset/secrets" \
    --mount "type=bind,src=$ROOT/data,dst=/reset/data" \
    -e APP_SECRET_KEY_FILE=/reset/secrets/app.key \
    -e APP_ADMIN_PASSWORD_HASH_FILE=/reset/secrets/admin.hash \
    -e APP_DATA_DIR=/reset/data "$IMAGE_ID" -m app.manage reset-password --password-stdin
unset PASSWORD CONFIRM
APP_IMAGE="$IMAGE_REF" docker compose -p any-signin-assistant up -d --no-build --force-recreate --wait --wait-timeout 180 app
APP_STOPPED=0
printf '管理员密码已重置，应用已恢复健康。\n'
printf '请同时更新或移除 部署信息.txt 中的旧密码。\n'
