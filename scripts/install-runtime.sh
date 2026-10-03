#!/usr/bin/env bash
set -euo pipefail
if [[ "$EUID" -ne 0 || "$(uname -s)" != Linux ]]; then
    printf '请在 Linux 服务器使用 sudo bash scripts/deploy.sh。\n' >&2
    exit 1
fi

prepare_apt() {
    if [[ "${APT_READY:-0}" == 1 ]]; then return; fi
    if [[ ! -f /etc/os-release ]] || ! command -v apt-get >/dev/null; then
        printf '自动安装支持 Ubuntu 22.04+ / Debian 12+；其他 Linux 请先安装 Docker、Compose、Python 3.10+ 和 flock。\n' >&2
        exit 1
    fi
    . /etc/os-release
    case "$ID" in
        ubuntu|debian) ;;
        *) printf '不自动更换当前发行版的软件源，请先手动安装运行依赖。\n' >&2; exit 1 ;;
    esac
    export DEBIAN_FRONTEND=noninteractive
    apt-get -o DPkg::Lock::Timeout=120 update
    apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends ca-certificates curl python3 util-linux
    APT_READY=1
}

docker_repository() {
    prepare_apt
    if ! grep -qsR 'download.docker.com/linux/' /etc/apt/sources.list /etc/apt/sources.list.d; then
        install -d -m 0755 /etc/apt/keyrings
        KEY_FILE="$(mktemp /etc/apt/keyrings/.assistant-docker-XXXXXX)"
        if ! curl --fail --silent --show-error --location --retry 3 --proto '=https' \
            "https://download.docker.com/linux/$ID/gpg" -o "$KEY_FILE"; then
            rm -f -- "$KEY_FILE"
            printf 'Docker 官方签名下载失败，请检查服务器网络后重试。\n' >&2
            exit 1
        fi
        chmod 0644 "$KEY_FILE"
        mv -- "$KEY_FILE" /etc/apt/keyrings/any-signin-assistant-docker.asc
        printf 'deb [arch=%s signed-by=/etc/apt/keyrings/any-signin-assistant-docker.asc] https://download.docker.com/linux/%s %s stable\n' \
            "$(dpkg --print-architecture)" "$ID" "${VERSION_CODENAME:?缺少发行版代号}" \
            > /etc/apt/sources.list.d/any-signin-assistant-docker.list
        chmod 0644 /etc/apt/sources.list.d/any-signin-assistant-docker.list
    fi
    apt-get -o DPkg::Lock::Timeout=120 update
}

if ! command -v python3 >/dev/null || ! command -v flock >/dev/null; then prepare_apt; fi
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    printf '主机 Python 需要 3.10+，推荐 Ubuntu 22.04+ 或 Debian 12+；不会替换系统 Python。\n' >&2
    exit 1
fi
if [[ -n "${DOCKER_HOST:-}" || -n "${DOCKER_CONTEXT:-}" && "${DOCKER_CONTEXT}" != default ]]; then
    printf '本脚本只部署当前服务器；请取消远程 DOCKER_HOST / DOCKER_CONTEXT 设置。\n' >&2
    exit 1
fi
if ! command -v docker >/dev/null; then
    prepare_apt
    for package in docker.io podman-docker containerd runc; do
        if [[ "$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true)" == 'install ok installed' ]]; then
            printf '已有 %s；为保护其他服务，不自动卸载或替换。请先修复 Docker 命令。\n' "$package" >&2
            exit 1
        fi
    done
    printf '正在从 Docker 官方软件源安装 Engine、Compose 和 Buildx。\n'
    docker_repository
    apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
if [[ "$(docker context show)" != default ]]; then
    printf '当前 Docker context 不是 default；为避免部署到别的主机，已停止。\n' >&2
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    prepare_apt
    if apt-cache show docker-compose-plugin >/dev/null 2>&1; then
        apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends docker-compose-plugin
    elif [[ "$(dpkg-query -W -f='${Status}' docker.io 2>/dev/null || true)" == 'install ok installed' ]] \
        && apt-cache show docker-compose-v2 >/dev/null 2>&1; then
        apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends docker-compose-v2
    else
        docker_repository
        apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends docker-compose-plugin
    fi
fi
if ! docker info >/dev/null 2>&1; then
    if command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; then
        systemctl enable --now docker
    else
        printf 'Docker 未运行，且当前环境没有 systemd；请启动 Docker 后重试。\n' >&2
        exit 1
    fi
fi
docker info >/dev/null
docker compose version
