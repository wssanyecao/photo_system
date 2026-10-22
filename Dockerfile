ARG PYTHON_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_IMAGE}

ARG DEBIAN_MIRROR=https://mirrors.tuna.tsinghua.edu.cn
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    PIP_INDEX_URL=${PIP_INDEX_URL}

# Debian 包版本由目标镜像的受控 snapshot/tag 决定；安全更新不在 Dockerfile 中锁死。
# hadolint ignore=DL3008
RUN sed -i \
      -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}/debian|g" \
      -e "s|http://deb.debian.org/debian-security|${DEBIAN_MIRROR}/debian-security|g" \
      -e "s|http://security.debian.org/debian-security|${DEBIAN_MIRROR}/debian-security|g" \
      /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends openssh-client rsync tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" photo-gateway \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home photo-gateway \
    && install -d -m 0700 -o "${APP_UID}" -g "${APP_GID}" /home/photo-gateway/.ssh

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY photo_gateway ./photo_gateway

USER photo-gateway:photo-gateway
EXPOSE 18080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18080/api/v1/health', timeout=3).read()"

CMD ["python", "-m", "photo_gateway", "serve", "--config", "/etc/photo-gateway/config.yaml"]
