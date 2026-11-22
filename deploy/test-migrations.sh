#!/usr/bin/env bash
set -euo pipefail

export POSTGRES_PASSWORD=ci-test-only
export COMPOSE_PROJECT_NAME="rag-migration-test-$$"
compose=(docker compose -f docker-compose.yml -f deploy/compose.production.yml)
db=("${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 -U rag -d p4rag)
pending=storage/migrations/998_ci_test.sql
rollback=storage/migrations/997_ci_test.sql
broken=storage/migrations/999_ci_test.sql
if [[ -e "$pending" || -e "$rollback" || -e "$broken" ]]; then
  printf '%s\n' 'Migration test paths already exist' >&2
  exit 1
fi
trap 'rm -f "$pending" "$rollback" "$broken"; "${compose[@]}" down -v' EXIT

"${compose[@]}" up -d --wait postgres
# Simulate a database with an earlier migration already applied manually.
"${db[@]}" --single-transaction -f /dev/stdin < storage/migrations/070_chat_persistence.sql
printf '%s\n' 'CREATE TABLE ci_migration_probe (value integer);' > "$pending"

migrate() { bash deploy/migrate.sh | "${db[@]}"; }
scalar() { "${db[@]}" -Atc "$1"; }

migrate
migrate
[[ $(scalar 'SELECT count(*) FROM public.schema_migrations') == 4 ]]
[[ $(scalar "SELECT to_regclass('chat_retrieval_attempts') IS NOT NULL") == t ]]

printf '%s\n' 'CREATE TABLE ci_migration_rollback (value integer);' > "$rollback"
printf '%s\n' 'SELECT 1 / 0;' > "$broken"
if migrate; then
  printf '%s\n' 'Expected invalid migration to fail' >&2
  exit 1
fi
[[ $(scalar "SELECT to_regclass('ci_migration_rollback') IS NULL") == t ]]
[[ $(scalar 'SELECT count(*) FROM public.schema_migrations') == 4 ]]
