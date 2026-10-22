#!/usr/bin/env bash
set -Eeuo pipefail

trap 'rc=$?; echo "[error] 导入/启动失败，line=${LINENO}, exit=${rc}" >&2; exit "${rc}"' ERR

# 在 docker-host 上选择最新的时间标签镜像，校验、导入、预检并启动。

PROJECT_DIR="${PROJECT_DIR:-/data/compose/photo-system}"
IMAGE_DIR="${IMAGE_DIR:-${PROJECT_DIR}/images}"
COMPOSE_FILE="${COMPOSE_FILE:-${PROJECT_DIR}/compose.yaml}"
ENV_FILE="${ENV_FILE:-${PROJECT_DIR}/.env}"
SERVICE_NAME="photo-gateway"
EXPECTED_UID="${EXPECTED_UID:-1000}"
EXPECTED_GID="${EXPECTED_GID:-1000}"
env_tmp=""

cleanup() {
  if [[ -n "${env_tmp}" && -e "${env_tmp}" ]]; then
    rm -f -- "${env_tmp}" || true
  fi
}

trap cleanup EXIT

fail() {
  echo "[error] $*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "缺少命令: $1"
}

for command_name in docker find sort sha256sum awk grep jq mktemp; do
  require_command "${command_name}"
done
docker info >/dev/null 2>&1 || fail "Docker daemon 不可用"
docker compose version >/dev/null 2>&1 || fail "Docker Compose 不可用"

[[ -d "${IMAGE_DIR}" ]] || fail "镜像目录不存在: ${IMAGE_DIR}"
[[ -f "${COMPOSE_FILE}" ]] || fail "Compose 文件不存在: ${COMPOSE_FILE}"
[[ -f "${ENV_FILE}" ]] || fail "环境文件不存在: ${ENV_FILE}"

# 仅接受本项目的完整时间标签文件；rsync 临时文件不会匹配。
IMAGE_ARCHIVE=$(find "${IMAGE_DIR}" -maxdepth 1 -type f \
  -name 'photo-system-????????-??????-linux-amd64.tar' -printf '%f\n' \
  | awk '/^photo-system-[0-9]{8}-[0-9]{6}-linux-amd64\.tar$/' \
  | LC_ALL=C sort \
  | tail -n 1)
[[ -n "${IMAGE_ARCHIVE}" ]] || fail "未找到时间标签格式的镜像文件"

if [[ ! "${IMAGE_ARCHIVE}" =~ ^photo-system-([0-9]{8}-[0-9]{6})-linux-amd64\.tar$ ]]; then
  fail "镜像文件名不符合约定: ${IMAGE_ARCHIVE}"
fi
IMAGE_TAG="${BASH_REMATCH[1]}"
IMAGE_NAME="photo-system:${IMAGE_TAG}"
ARCHIVE_PATH="${IMAGE_DIR}/${IMAGE_ARCHIVE}"
CHECKSUM_PATH="${ARCHIVE_PATH}.sha256"

[[ -f "${CHECKSUM_PATH}" ]] || fail "缺少校验文件: ${CHECKSUM_PATH}"
[[ "$(wc -l <"${CHECKSUM_PATH}" | tr -d ' ')" == "1" ]] \
  || fail "校验文件必须且只能包含一条记录"

expected_hash=$(awk 'NR == 1 {print $1}' "${CHECKSUM_PATH}")
listed_name=$(awk 'NR == 1 {$1=""; sub(/^[[:space:]]+/, ""); sub(/^\*/, ""); print}' "${CHECKSUM_PATH}")
[[ "${expected_hash}" =~ ^[0-9a-fA-F]{64}$ ]] || fail "SHA-256 格式无效"
[[ "${listed_name}" == "${IMAGE_ARCHIVE}" ]] \
  || fail "校验文件记录的文件名不匹配: ${listed_name}"

actual_hash=$(sha256sum "${ARCHIVE_PATH}" | awk '{print $1}')
[[ "${actual_hash}" == "${expected_hash}" ]] \
  || fail "SHA-256 校验失败: ${IMAGE_ARCHIVE}"
echo "[verified] sha256=${actual_hash} archive=${IMAGE_ARCHIVE}"

docker load --input "${ARCHIVE_PATH}"
docker image inspect "${IMAGE_NAME}" >/dev/null 2>&1 \
  || fail "docker load 后未找到预期镜像: ${IMAGE_NAME}"

platform=$(docker image inspect "${IMAGE_NAME}" --format '{{.Os}}/{{.Architecture}}')
[[ "${platform}" == "linux/amd64" ]] || fail "镜像架构错误: ${platform}"

identity=$(docker run --rm --platform linux/amd64 --entrypoint id "${IMAGE_NAME}")
[[ "${identity}" == *"uid=${EXPECTED_UID}"* && "${identity}" == *"gid=${EXPECTED_GID}"* ]] \
  || fail "镜像运行身份错误: ${identity}"
echo "[verified] image=${IMAGE_NAME} platform=${platform} identity=${identity}"

cd "${PROJECT_DIR}"

# 先用即将部署的标签完成全部 Compose/挂载/应用配置检查，不提前修改 .env。
compose_json=$(PHOTO_IMAGE_TAG="${IMAGE_TAG}" docker compose \
  --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" config --format json)
resolved_image=$(jq -r ".services[\"${SERVICE_NAME}\"].image" <<<"${compose_json}")
resolved_platform=$(jq -r ".services[\"${SERVICE_NAME}\"].platform" <<<"${compose_json}")
[[ "${resolved_image}" == "${IMAGE_NAME}" ]] \
  || fail "Compose 镜像不匹配: ${resolved_image}"
[[ "${resolved_platform}" == "linux/amd64" ]] \
  || fail "Compose 平台不匹配: ${resolved_platform}"

PHOTO_IMAGE_TAG="${IMAGE_TAG}" docker compose \
  --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" \
  run --rm --no-deps --pull never "${SERVICE_NAME}" \
  python -m photo_gateway check --config /etc/photo-gateway/config.yaml

compose_identity=$(PHOTO_IMAGE_TAG="${IMAGE_TAG}" docker compose \
  --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" \
  run --rm --no-deps --pull never --entrypoint sh "${SERVICE_NAME}" \
  -c 'id && test -w /data/photo-system/storage && test -r /etc/photo-gateway/config.yaml && test -r /home/photo-gateway/.ssh/nas_key && test -r /home/photo-gateway/.ssh/known_hosts')
[[ "${compose_identity}" == *"uid=${EXPECTED_UID}"* && "${compose_identity}" == *"gid=${EXPECTED_GID}"* ]] \
  || fail "Compose 实际运行身份不匹配: ${compose_identity}"
echo "[verified] compose_identity=${compose_identity} mounts=ok"

# 所有启动前检查通过后，才原子更新持久化标签。
tag_count=$(grep -c '^PHOTO_IMAGE_TAG=' "${ENV_FILE}" || true)
[[ "${tag_count}" -le 1 ]] || fail ".env 中存在重复 PHOTO_IMAGE_TAG"
env_tmp=$(mktemp "${ENV_FILE}.tmp.XXXXXX")
awk -v tag="${IMAGE_TAG}" '
  BEGIN {updated = 0}
  /^PHOTO_IMAGE_TAG=/ {
    if (!updated) print "PHOTO_IMAGE_TAG=" tag
    updated = 1
    next
  }
  {print}
  END {if (!updated) print "PHOTO_IMAGE_TAG=" tag}
' "${ENV_FILE}" >"${env_tmp}"
chmod --reference="${ENV_FILE}" "${env_tmp}"
mv -f "${env_tmp}" "${ENV_FILE}"
env_tmp=""

docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" \
  up -d --no-build --pull never --wait --wait-timeout 90

container_id=$(docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" \
  ps -q "${SERVICE_NAME}")
[[ -n "${container_id}" ]] || fail "服务容器未创建"
expected_image_id=$(docker image inspect "${IMAGE_NAME}" --format '{{.Id}}')
running_image_id=$(docker inspect "${container_id}" --format '{{.Image}}')
[[ "${running_image_id}" == "${expected_image_id}" ]] \
  || fail "运行容器不是刚导入的镜像"

docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" ps
echo "[done] 已启动 ${IMAGE_NAME}"
