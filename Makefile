SHELL := /bin/bash
.DEFAULT_GOAL := help

PYTHON ?= python3
VENV ?= .venv
BIN := $(VENV)/bin
PIP := $(BIN)/python -m pip
ALEMBIC := $(BIN)/alembic
PAPERSTEWARD := $(BIN)/papersteward
PYTEST := $(BIN)/pytest
RUFF := $(BIN)/ruff
MYPY := $(BIN)/mypy
DEMO_PORT ?= 8000
START_ARGS ?=

.PHONY: help bootstrap check test test-integration e2e migrate-check demo secret-scan docker-build docker-up docker-down

help:
	@echo "PaperSteward 常用命令："
	@echo "  make bootstrap        创建 .venv、安装依赖、准备 .env、迁移数据库"
	@echo "  make check            ruff format/check + mypy + 非 e2e 测试"
	@echo "  make test             单元/契约/集成测试（不含 e2e）"
	@echo "  make test-integration 仅集成测试"
	@echo "  make e2e              启动真实 Uvicorn 的 walking skeleton 测试"
	@echo "  make migrate-check    临时库执行 upgrade/downgrade/upgrade"
	@echo "  make demo             迁移、写入演示数据、启动 API 并验证 health/papers"
	@echo "  make secret-scan      扫描常见密钥模式"
	@echo "  make docker-build     docker compose build"
	@echo "  make docker-up        构建并启动（脚本使用 legacy builder + host 网络）"
	@echo "  make docker-down      docker compose down"

$(BIN)/python:
	$(PYTHON) -m venv $(VENV)

bootstrap: $(BIN)/python
	$(BIN)/python -c 'import sys; assert sys.version_info >= (3, 11), "需要 Python 3.11+；可用 make bootstrap PYTHON=python3.12"'
	$(PIP) install -e ".[dev]"
	@test -f .env || cp .env.example .env
	$(ALEMBIC) upgrade head
	@echo "bootstrap 完成：$(BIN)/python；数据库已迁移"

check:
	$(RUFF) format --check .
	$(RUFF) check .
	$(MYPY)
	$(PYTEST) -m "not e2e and not live" -q

test:
	$(PYTEST) -m "not e2e and not live" -q

test-integration:
	$(PYTEST) tests/integration -q

e2e:
	$(PYTEST) -m e2e -q

migrate-check:
	@set -e; tmp=$$(mktemp -d); \
	trap 'rm -rf "$$tmp"' EXIT; \
	export PAPERSTEWARD_DATABASE_URL="sqlite+pysqlite:///$$tmp/migrate.db"; \
	$(ALEMBIC) upgrade head; \
	$(ALEMBIC) downgrade base; \
	$(ALEMBIC) upgrade head; \
	echo "migrate-check: upgrade/downgrade/upgrade OK"

demo:
	BIN="$(abspath $(BIN))" DEMO_PORT="$(DEMO_PORT)" ./scripts/demo.sh

secret-scan:
	@if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then \
		echo "secret-scan: 需要 git 工作区"; \
		exit 1; \
	fi
	@if git grep --untracked -nE '(sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN (RSA|OPENSSH|EC) PRIVATE KEY-----)' -- . ':!docs' ':!.venv' ':!.env' ':!var' ':!*.example'; then \
		echo "secret-scan: 发现疑似密钥，请检查"; \
		exit 1; \
	else \
		echo "secret-scan: OK"; \
	fi

docker-build:
	DOCKER_BUILDKIT=0 docker compose -f docker-compose.yml -f docker-compose.hostnet.yml build

docker-up:
	DOCKER_BUILDKIT=0 docker compose -f docker-compose.yml -f docker-compose.hostnet.yml up --build

docker-down:
	docker compose down

.PHONY: start
start:
	$(PAPERSTEWARD) start $(START_ARGS)
