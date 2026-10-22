#!/usr/bin/env bash
set -Eeuo pipefail

# Debian 13 amd64 Docker Engine + Buildx + Compose 安装脚本。
# 只安装和配置 Docker，不创建任何业务项目目录，不修改防火墙/网络。

DOCKER_APT_MIRROR="${DOCKER_APT_MIRROR:-https://mirrors.tuna.tsinghua.edu.cn/docker-ce/linux/debian}"
DOCKER_DATA_ROOT="${DOCKER_DATA_ROOT:-/data/docker}"
APT_RETRIES="${APT_RETRIES:-5}"

# 未设置时使用默认加速器；显式传入空值时删除已有 registry-mirrors 配置。
if [[ -z "${DOCKER_REGISTRY_MIRROR+x}" ]]; then
  DOCKER_REGISTRY_MIRROR="https://docker.1ms.run"
fi

# sudo 运行时默认把发起 sudo 的普通用户加入 docker 组。显式传入
# `DOCKER_USER=` 可关闭；直接以 root 登录时无需加入用户组。
if [[ -z "${DOCKER_USER+x}" ]]; then
  DOCKER_USER="${SUDO_USER:-}"
fi

key_tmp=""
daemon_input=""
daemon_tmp=""

cleanup_tmp() {
  local path
  for path in "${key_tmp}" "${daemon_input}" "${daemon_tmp}"; do
    [[ -z "${path}" ]] || rm -f -- "${path}"
  done
}
trap cleanup_tmp EXIT

apt_retry() {
  DEBIAN_FRONTEND=noninteractive apt-get \
    -o "Acquire::Retries=${APT_RETRIES}" \
    -o Acquire::http::Timeout=30 \
    -o Acquire::https::Timeout=30 \
    "$@"
}

download_docker_key() {
  local url
  key_tmp=$(mktemp)
  for url in \
    "${DOCKER_APT_MIRROR%/}/gpg" \
    "https://download.docker.com/linux/debian/gpg"; do
    echo "[download] Docker signing key: ${url}"
    if curl --ipv4 --fail --location --silent --show-error \
      --connect-timeout 10 --max-time 120 \
      --retry 5 --retry-delay 2 --retry-all-errors \
      "${url}" -o "${key_tmp}" \
      && [[ -s "${key_tmp}" ]] \
      && grep -q "BEGIN PGP PUBLIC KEY BLOCK" "${key_tmp}"; then
      return 0
    fi
    : >"${key_tmp}"
  done
  echo "Docker signing key 下载失败（国内镜像和官方地址均不可用）" >&2
  return 1
}

verify_hello_world() {
  local attempt
  for attempt in 1 2 3; do
    echo "[verify] hello-world (${attempt}/3)"
    if docker run --rm hello-world; then
      return 0
    fi
    sleep $((attempt * 2))
  done
  echo "Docker 已安装且 daemon 配置已生效，但外部镜像拉取验证失败。" >&2
  echo "请检查 registry mirror、DNS、IPv4/IPv6 路由和出口网络后重试：" >&2
  echo "  docker run --rm hello-world" >&2
  return 7
}

if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 sudo 运行本脚本" >&2
  exit 2
fi

source /etc/os-release
if [[ "${ID:-}" != "debian" || "${VERSION_ID:-}" != "13" ]]; then
  echo "仅支持 Debian 13；当前为 ${PRETTY_NAME:-unknown}" >&2
  exit 2
fi
if [[ "$(dpkg --print-architecture)" != "amd64" ]]; then
  echo "docker-host 脚本要求 amd64 架构" >&2
  exit 2
fi

install -d -m 0755 /data
echo "[data] $(findmnt -T /data -no SOURCE,FSTYPE,TARGET,OPTIONS)"
df -h /data

if command -v docker >/dev/null 2>&1 && systemctl is-active --quiet docker; then
  current_root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)
  if [[ -n "${current_root}" && "${current_root}" != "${DOCKER_DATA_ROOT}" ]]; then
    echo "检测到现有 Docker data-root=${current_root}，拒绝自动切换到 ${DOCKER_DATA_ROOT}。" >&2
    echo "请先单独规划已有镜像/容器迁移；本脚本不会移动或删除现有 Docker 数据。" >&2
    exit 4
  fi
fi

conflicts=()
for pkg in docker.io docker-compose docker-doc docker-buildx podman-docker containerd runc; do
  if dpkg-query -W -f='${Status}' "${pkg}" 2>/dev/null | grep -q 'install ok installed'; then
    conflicts+=("${pkg}")
  fi
done
if (( ${#conflicts[@]} > 0 )); then
  echo "发现冲突软件包，脚本不会自动卸载: ${conflicts[*]}" >&2
  exit 3
fi

apt_retry update
apt_retry install -y ca-certificates curl jq
install -m 0755 -d /etc/apt/keyrings
download_docker_key
install -m 0644 "${key_tmp}" /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

cat >/etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: ${DOCKER_APT_MIRROR}
Suites: ${VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

apt_retry update
apt_retry install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

install -d -m 0711 "${DOCKER_DATA_ROOT}"

install -d -m 0755 /etc/docker
daemon_source=/etc/docker/daemon.json
daemon_input=$(mktemp)
printf '{}\n' >"${daemon_input}"
daemon_had_existing=false
daemon_backup=""
if [[ -f "${daemon_source}" ]]; then
  jq empty "${daemon_source}"
  daemon_had_existing=true
  daemon_backup="${daemon_source}.bak.$(date +%Y%m%d-%H%M%S)"
  cp -a "${daemon_source}" "${daemon_backup}"
  cp "${daemon_source}" "${daemon_input}"
fi
daemon_tmp=$(mktemp)
jq -n --slurpfile old "${daemon_input}" \
  --arg data_root "${DOCKER_DATA_ROOT}" \
  --arg mirror "${DOCKER_REGISTRY_MIRROR}" '
    (($old[0] // {})
     + {"data-root": $data_root,
        "log-driver": "json-file",
        "log-opts": (($old[0]["log-opts"] // {}) + {"max-size": "20m", "max-file": "5"})}) as $merged
    | if $mirror == ""
      then $merged | del(."registry-mirrors")
      else $merged + {"registry-mirrors": [$mirror]}
      end
  ' >"${daemon_tmp}"
dockerd --validate --config-file="${daemon_tmp}"
install -m 0644 "${daemon_tmp}" "${daemon_source}"

systemctl daemon-reload
systemctl enable docker
if ! systemctl restart docker; then
  echo "Docker 使用新 daemon.json 启动失败，正在恢复修改前配置。" >&2
  journalctl -u docker.service -n 80 --no-pager >&2 || true
  if [[ "${daemon_had_existing}" == true ]]; then
    cp -a "${daemon_backup}" "${daemon_source}"
  else
    rm -f "${daemon_source}"
  fi
  systemctl restart docker || true
  exit 6
fi

if [[ -n "${DOCKER_USER}" && "${DOCKER_USER}" != "root" ]]; then
  id "${DOCKER_USER}" >/dev/null
  usermod -aG docker "${DOCKER_USER}"
  echo "[warning] ${DOCKER_USER} 已加入 docker 组（等同主机 root 权限），重新登录后生效"
fi

docker version
docker compose version
actual_root=$(docker info --format '{{.DockerRootDir}}')
actual_mirrors=$(docker info --format '{{json .RegistryConfig.Mirrors}}')
echo "DockerRootDir=${actual_root} RegistryMirrors=${actual_mirrors}"

if [[ "${actual_root}" != "${DOCKER_DATA_ROOT}" ]]; then
  echo "Docker data-root 未生效：期望 ${DOCKER_DATA_ROOT}，实际 ${actual_root}" >&2
  exit 8
fi
if [[ -n "${DOCKER_REGISTRY_MIRROR}" ]] && ! jq -e \
  --arg mirror "${DOCKER_REGISTRY_MIRROR%/}" \
  'map(rtrimstr("/")) | index($mirror) != null' \
  <<<"${actual_mirrors}" >/dev/null; then
  echo "Docker registry mirror 未生效：期望 ${DOCKER_REGISTRY_MIRROR}，实际 ${actual_mirrors}" >&2
  exit 9
fi

verify_hello_world

echo "Docker Engine、Buildx 与 Compose 安装完成。"
