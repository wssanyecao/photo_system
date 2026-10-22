#!/usr/bin/env bash
set -Eeuo pipefail

trap 'rc=$?; echo "[error] 构建/同步失败，line=${LINENO}, exit=${rc}" >&2; exit "${rc}"' ERR

# 在 macOS/ARM64 构建 docker-host 所需的 linux/amd64 镜像，导出并同步到 docker-host。

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT="${SCRIPT_DIR}"
ARTIFACT_DIR="${ARTIFACT_DIR:-${REPO_ROOT}/.artifacts/images}"
REMOTE_HOST="${REMOTE_HOST:-docker-host}"
REMOTE_IMAGE_DIR="${REMOTE_IMAGE_DIR:-/data/compose/photo-system/images}"
APP_UID="${APP_UID:-1000}"
APP_GID="${APP_GID:-1000}"
ALLOW_DIRTY="${ALLOW_DIRTY:-0}"

fail() {
  echo "[error] $*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "缺少命令: $1"
}

for command_name in docker git rsync shasum; do
  require_command "${command_name}"
done

cd "${REPO_ROOT}"
[[ -f Dockerfile ]] || fail "未找到 ${REPO_ROOT}/Dockerfile"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail "当前目录不是 Git 仓库"

if [[ "${ALLOW_DIRTY}" != "1" ]] && [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
  fail "工作区存在未提交或未跟踪文件；请先提交，或明确使用 ALLOW_DIRTY=1"
fi

docker info >/dev/null 2>&1 || fail "Docker daemon 不可用"
docker buildx version >/dev/null 2>&1 || fail "Docker Buildx 不可用"

# 时间标签采用上海时区，文件名按字典序即可得到构建先后。
IMAGE_TAG=$(TZ=Asia/Shanghai date '+%Y%m%d-%H%M%S')
IMAGE_NAME="photo-system:${IMAGE_TAG}"
IMAGE_ARCHIVE="photo-system-${IMAGE_TAG}-linux-amd64.tar"
ARCHIVE_PATH="${ARTIFACT_DIR}/${IMAGE_ARCHIVE}"
CHECKSUM_PATH="${ARCHIVE_PATH}.sha256"
GIT_REVISION=$(git rev-parse HEAD)
CREATED_AT=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

mkdir -p "${ARTIFACT_DIR}"
[[ ! -e "${ARCHIVE_PATH}" && ! -e "${CHECKSUM_PATH}" ]] \
  || fail "同一时间标签的构建产物已存在: ${IMAGE_TAG}"

echo "[build] image=${IMAGE_NAME} platform=linux/amd64 uid=${APP_UID} gid=${APP_GID}"
docker buildx build \
  --platform linux/amd64 \
  --pull=false \
  --load \
  --build-arg "APP_UID=${APP_UID}" \
  --build-arg "APP_GID=${APP_GID}" \
  --label "org.opencontainers.image.created=${CREATED_AT}" \
  --label "org.opencontainers.image.revision=${GIT_REVISION}" \
  -t "${IMAGE_NAME}" \
  .

platform=$(docker image inspect "${IMAGE_NAME}" --format '{{.Os}}/{{.Architecture}}')
[[ "${platform}" == "linux/amd64" ]] || fail "镜像架构错误: ${platform}"

identity=$(docker run --rm --platform linux/amd64 --entrypoint id "${IMAGE_NAME}")
[[ "${identity}" == *"uid=${APP_UID}"* && "${identity}" == *"gid=${APP_GID}"* ]] \
  || fail "镜像运行身份错误: ${identity}"

echo "[export] ${ARCHIVE_PATH}"
docker save --output "${ARCHIVE_PATH}" "${IMAGE_NAME}"
(
  cd "${ARTIFACT_DIR}"
  shasum -a 256 "${IMAGE_ARCHIVE}" >"${IMAGE_ARCHIVE}.sha256"
)

echo "[sync] ${REMOTE_HOST}:${REMOTE_IMAGE_DIR}/"
rsync -rtvP \
  --partial-dir=.rsync-partial \
  --delay-updates \
  "${ARCHIVE_PATH}" "${CHECKSUM_PATH}" \
  "${REMOTE_HOST}:${REMOTE_IMAGE_DIR}/"

echo "[done] image=${IMAGE_NAME}"
echo "[done] archive=${IMAGE_ARCHIVE}"
echo "[next] 在 docker-host 执行 /data/compose/photo-system/load_start.sh"
