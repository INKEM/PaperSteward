#!/bin/sh
# 构建阶段需要访问 PyPI。部分主机的 Docker bridge 没有默认路由，
# 而 BuildKit 的 network.host 需要额外 entitlement；legacy builder + compose
# 的 build.network=host 可以在这类环境直接工作。
set -eu
cd "$(dirname "$0")/.."
exec env DOCKER_BUILDKIT=0 docker compose \
  -f docker-compose.yml -f docker-compose.hostnet.yml up --build "$@"
