#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$EUID" -ne 0 || "$ROOT" == / || "$(cat "$ROOT/.project-id")" != any-signin-assistant-managed-v1 ]]; then
    printf '请使用 sudo 在本项目目录安装更新执行器。\n' >&2
    exit 1
fi
if [[ "$ROOT" == *$'\n'* || "$ROOT" == *'%'* || "$ROOT" == *'"'* || "$ROOT" == *'\'* ]]; then
    printf '项目路径含不支持的字符，拒绝生成服务配置。\n' >&2
    exit 1
fi
for directory in "$ROOT/updates" "$ROOT/updates/requests" "$ROOT/updates/status" "$ROOT/updates/work"; do
    if [[ -L "$directory" ]]; then printf '更新目录不能是符号链接。\n' >&2; exit 1; fi
done
install -d -m 0755 "$ROOT/updates" "$ROOT/updates/status"
install -d -m 0700 -o 10001 -g 10001 "$ROOT/updates/requests"
install -d -m 0700 "$ROOT/updates/work"
if [[ "${1:-}" == --prepare-only ]]; then exit 0; fi
DEST=/usr/local/lib/any-signin-assistant-updater
install -d -m 0755 "$DEST"
install -m 0644 "$ROOT/scripts/update_runner.py" "$DEST/update_runner.py.next"
install -m 0644 "$ROOT/app/update_common.py" "$DEST/update_common.py.next"
mv -- "$DEST/update_runner.py.next" "$DEST/update_runner.py"
mv -- "$DEST/update_common.py.next" "$DEST/update_common.py"
if [[ ! -f "$ROOT/.release-files.json" || "${1:-}" == --reset-baseline ]]; then
    python3 "$DEST/update_runner.py" --root "$ROOT" --record-baseline
fi
if ! command -v systemctl >/dev/null || [[ ! -d /run/systemd/system ]]; then
    printf '当前主机没有运行 systemd；应用可正常使用，在线更新执行器需手动运行。\n'
    exit 0
fi
cat > /etc/systemd/system/any-signin-assistant-updater.service <<EOF
[Unit]
Description=Any Sign-in Assistant managed updater
After=docker.service network-online.target
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 "$DEST/update_runner.py" --root "$ROOT"
WorkingDirectory=$ROOT
Restart=always
RestartSec=5
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths="$ROOT" "$DEST" /etc/systemd/system /run/systemd
PrivateTmp=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

[Install]
WantedBy=multi-user.target
EOF
if command -v systemd-analyze >/dev/null; then
    systemd-analyze verify /etc/systemd/system/any-signin-assistant-updater.service
fi
systemctl daemon-reload
if [[ "${1:-}" != --refresh-only ]]; then
    systemctl enable any-signin-assistant-updater.service >/dev/null
    systemctl restart any-signin-assistant-updater.service
    printf '在线更新执行器已安装；Web 容器不挂载 Docker socket。\n'
fi
