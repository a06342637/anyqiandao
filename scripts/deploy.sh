#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "$ROOT"
umask 077
if [[ "$ROOT" == / || "$EUID" -ne 0 || ! -f .project-id || "$(cat .project-id)" != any-signin-assistant-managed-v1 ]]; then
    printf '请在本项目目录使用 sudo bash scripts/deploy.sh。\n' >&2
    exit 1
fi
if (( $# > 1 )); then printf '用法：sudo bash scripts/deploy.sh [https://你的域名]\n' >&2; exit 1; fi
for path in .env secrets secrets/app.key secrets/admin.hash data data/assistant.sqlite3 updates updates/.deploy.lock backups 部署信息.txt; do
    if [[ -L "$path" ]]; then printf '配置或数据路径不能是符号链接：%s\n' "$path" >&2; exit 1; fi
done
ADMIN_PASSWORD="${APP_INSTALL_PASSWORD:-}"
unset APP_INSTALL_PASSWORD
bash scripts/install-runtime.sh
CONTAINERS="$(docker ps -aq --filter label=com.docker.compose.project=any-signin-assistant)"
while IFS= read -r container; do
    if [[ -z "$container" ]]; then continue; fi
    MANAGED_ROOT="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$container")"
    if [[ -z "$MANAGED_ROOT" || "$(realpath -m -- "$MANAGED_ROOT")" != "$ROOT" ]]; then
        printf '本机已有本项目容器，原部署目录：%s。请将新源码覆盖到原目录后部署，避免接管其他数据。\n' "$MANAGED_ROOT" >&2
        exit 1
    fi
done <<< "$CONTAINERS"
if command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; then
    MANAGED_ROOT="$(systemctl show any-signin-assistant-updater.service --property=WorkingDirectory --value 2>/dev/null || true)"
    if [[ -n "$MANAGED_ROOT" && "$(realpath -m -- "$MANAGED_ROOT")" != "$ROOT" ]]; then
        printf '已有更新服务管理目录 %s；请回原目录部署，不覆盖它。\n' "$MANAGED_ROOT" >&2
        exit 1
    fi
fi
bash scripts/install-updater.sh --prepare-only
exec 9> updates/.deploy.lock
if ! flock -n 9; then printf '另一个部署或在线更新正在进行，请稍后重试。\n' >&2; exit 1; fi
VERSION="$(tr -d '\r\n' < VERSION)"
IMAGE="any-signin-assistant:${VERSION}"
EXISTING=0
QUEUE_STATE=''
SWITCH_STARTED=0
PUBLIC_URL="${1:-}"

restore_queue() {
    python3 - "$QUEUE_STATE" <<'PY'
import json, sqlite3, sys
previous = json.loads(sys.argv[1])
with sqlite3.connect('file:data/assistant.sqlite3?mode=rw', uri=True, timeout=30) as connection:
    connection.executemany('INSERT OR REPLACE INTO meta VALUES (?,?)',
                           [('queue_paused', previous.get('queue_paused', '0')), ('pause_reason', previous.get('pause_reason', ''))])
PY
}

finish() {
    result=$?
    trap - EXIT
    unset ADMIN_PASSWORD
    if [[ "$result" != 0 ]]; then
        if [[ -n "$QUEUE_STATE" && "$SWITCH_STARTED" == 0 ]]; then restore_queue || true; fi
        printf '\n部署未完成。配置、密钥和备份均保留；请排查报错后在同一目录重试。\n' >&2
        if [[ "$SWITCH_STARTED" == 1 && -n "$QUEUE_STATE" ]]; then
            printf '新服务尚未确认健康，队列保持暂停；请勿删除 data、secrets 或 backups。\n' >&2
        fi
    fi
    exit "$result"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ -e .env || -e secrets/app.key || -e secrets/admin.hash || -e data/assistant.sqlite3 || -e 部署信息.txt ]]; then
    if [[ ! -f .env || ! -f secrets/app.key || ! -f secrets/admin.hash ]]; then
        printf '已有配置不完整；请恢复原密钥与配置，拒绝重新初始化。\n' >&2
        exit 1
    fi
    EXISTING=1
    printf '\n检测到已有部署：保留原端口、管理员、密码、密钥和业务数据。\n'
    python3 - <<'PY'
import json
from pathlib import Path
path = Path('updates/status/state.json')
if Path('updates/requests/request.json').exists():
    raise SystemExit('存在待执行的在线更新请求，请先处理完成再部署。')
if path.exists():
    state = json.loads(path.read_text())
    if state.get('id') and state.get('stage') not in ('complete', 'failed', 'rolled_back'):
        raise SystemExit('在线更新正在执行，请等待结束后再进行手动部署。')
PY
else
    printf '\nany签到助手 v%s · 首次部署\n' "$VERSION"
    python3 scripts/deploy_config.py url "$PUBLIC_URL"
    PORT_INPUT="${APP_INSTALL_PORT:-}"
    ADMIN_USERNAME="${APP_INSTALL_USERNAME:-admin}"
    while true; do
        if [[ -t 0 ]]; then read -r -p '应用端口（回车随机选择空闲端口）：' PORT_INPUT; fi
        if APP_PORT="$(python3 scripts/deploy_config.py port "$PORT_INPUT" "$PUBLIC_URL")"; then break; fi
        if [[ ! -t 0 ]]; then exit 1; fi
    done
    while true; do
        if [[ -t 0 ]]; then
            read -r -p '管理员用户名（回车默认 admin）：' ADMIN_USERNAME
            ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
        fi
        if python3 scripts/deploy_config.py username "$ADMIN_USERNAME" >/dev/null; then break; fi
        if [[ ! -t 0 ]]; then exit 1; fi
    done
    if [[ -t 0 ]]; then
        while true; do
            read -r -s -p '管理员密码（回车使用默认随机强密码；自定义 12–1024 位）：' ADMIN_PASSWORD
            printf '\n'
            if [[ -z "$ADMIN_PASSWORD" || ( ${#ADMIN_PASSWORD} -ge 12 && ${#ADMIN_PASSWORD} -le 1024 ) ]]; then break; fi
            printf '密码需要 12–1024 位，请重新输入。\n'
        done
    fi
    if [[ -n "$ADMIN_PASSWORD" && ( ${#ADMIN_PASSWORD} -lt 12 || ${#ADMIN_PASSWORD} -gt 1024 ) ]]; then
        printf '管理员密码需要 12–1024 位，或留空生成随机强密码。\n' >&2
        exit 1
    fi
    if [[ -z "$PUBLIC_URL" ]]; then
        PUBLIC_URL="http://127.0.0.1:${APP_PORT}"
    fi
    INIT_OPTIONS=(--root /workspace --public-url "$PUBLIC_URL" --port "$APP_PORT" --username "$ADMIN_USERNAME" --owner 10001 --password-stdin)
    if [[ "$PUBLIC_URL" == https://* ]]; then
        INIT_OPTIONS+=(--https-proxy --bind-host 127.0.0.1)
    elif [[ "$PUBLIC_URL" == http://127.0.0.1:* || "$PUBLIC_URL" == http://localhost:* ]]; then
        INIT_OPTIONS+=(--allow-http --bind-host 127.0.0.1)
    else
        INIT_OPTIONS+=(--allow-http --bind-host 0.0.0.0)
    fi
    printf '访问地址将为：%s\n用户名：%s\n密码不会写入构建日志。\n' "$PUBLIC_URL" "$ADMIN_USERNAME"
fi
printf '正在构建项目镜像，已有服务会继续运行；首次下载可能需要几分钟。\n'
docker build -t "$IMAGE" .
if [[ "$EXISTING" == 0 ]]; then
    printf '%s' "$ADMIN_PASSWORD" | docker run --rm -i --user 0:0 --entrypoint python --mount "type=bind,src=$ROOT,dst=/workspace" "$IMAGE" -m app.manage init "${INIT_OPTIONS[@]}"
    unset ADMIN_PASSWORD
elif [[ -f data/assistant.sqlite3 ]]; then
    QUEUE_STATE="$(python3 - <<'PY'
import json, sqlite3
with sqlite3.connect('data/assistant.sqlite3') as connection:
    print(json.dumps(dict(connection.execute("SELECT key,value FROM meta WHERE key IN ('queue_paused','pause_reason')"))))
PY
)"
    bash scripts/backup.sh
fi
PUBLIC_URL="$(sed -n 's/^APP_PUBLIC_URL=//p' .env | tr -d '\r')"
ADMIN_USERNAME="$(sed -n 's/^APP_ADMIN_USERNAME=//p' .env | tr -d '\r')"
HTTPS_PROXY="$(sed -n 's/^APP_ENABLE_HTTPS_PROXY=//p' .env | tr -d '\r')"
export APP_IMAGE="$IMAGE"
SWITCH_STARTED=1
if grep -q '^APP_IMAGE=' .env; then
    sed -i "s/^APP_IMAGE=.*/APP_IMAGE=$IMAGE/" .env
else
    printf 'APP_IMAGE=%s\n' "$IMAGE" >> .env
fi
if [[ "$HTTPS_PROXY" == 1 || ( -z "$HTTPS_PROXY" && "$PUBLIC_URL" == https://* ) ]]; then
    docker compose -p any-signin-assistant --profile https up -d --no-build --wait --wait-timeout 180
else
    docker compose -p any-signin-assistant up -d --no-build --wait --wait-timeout 180 app
fi
if [[ -n "$QUEUE_STATE" ]]; then
    restore_queue
    QUEUE_STATE=''
fi
bash scripts/install-updater.sh --reset-baseline
printf '\nany签到助手 v%s 已部署\n访问地址：%s\n账号：%s\n密码：请查看 %s/部署信息.txt\n' "$VERSION" "$PUBLIC_URL" "${ADMIN_USERNAME:-admin}" "$ROOT"
if [[ "$PUBLIC_URL" == http://* ]]; then
    printf '仅允许本机安全访问：请通过 SSH 隧道连接，或配置 HTTPS 反代后更新 APP_PUBLIC_URL；公网 HTTP 无法登录。\n'
fi
printf '仅管理本项目的容器；不会改动其他服务、磁盘或全局镜像。\n'
printf '如无法从外网访问，请在云安全组和防火墙中放行 .env 的 APP_PORT；脚本不会关闭防火墙。\n'
