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
RUN apt-get update \
    && apt-get install -y --no-install-recommends passwd \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 papersteward \
    && useradd --uid 10001 --gid papersteward --create-home \
       --shell /usr/sbin/nologin papersteward \
    && mkdir -p /app/var \
    && chown -R papersteward:papersteward /app/var \
    && chmod +x /app/docker-entrypoint.sh

USER papersteward

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "papersteward.main:app", "--host", "0.0.0.0", "--port", "8000"]
