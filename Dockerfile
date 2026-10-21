FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY alembic.ini ./
COPY migrations ./migrations
COPY docker-entrypoint.sh ./

# 运行 Alembic 需要 useradd/groupadd；显式安装以免 slim 镜像缺少。
# Build context 可能来自 umask 077 的工作区，最后统一补齐非 root 读/执行权限。
RUN apt-get update \
    && apt-get install -y --no-install-recommends passwd \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 papersteward \
    && useradd --uid 10001 --gid papersteward --create-home \
       --shell /usr/sbin/nologin papersteward \
    && mkdir -p /app/var \
    && chown -R papersteward:papersteward /app/var \
    && chmod -R a+rX /app/src /app/migrations \
    && chmod 644 /app/alembic.ini \
    && chmod 755 /app/docker-entrypoint.sh

USER papersteward

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "papersteward.main:app", "--host", "0.0.0.0", "--port", "8000"]
