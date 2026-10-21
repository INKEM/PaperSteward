#!/bin/sh
set -eu

echo "papersteward: applying database migrations"
alembic upgrade head

exec "$@"
