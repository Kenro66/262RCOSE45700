#!/usr/bin/env bash
# Run from the repository root and pipe to psql -X -v ON_ERROR_STOP=1.
set -euo pipefail
export LC_ALL=C
shopt -s nullglob

cat <<'SQL'
BEGIN;
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(41700, 1);
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    filename text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
SQL

for path in storage/migrations/*.sql; do
    filename=${path##*/}
    if [[ ! "$filename" =~ ^[0-9]{3}_[a-z0-9_]+\.sql$ ]]; then
        printf 'Invalid migration filename: %s\n' "$filename" >&2
        exit 1
    fi

    printf "SELECT NOT EXISTS (SELECT 1 FROM public.schema_migrations WHERE filename = '%s') AS pending\n" "$filename"
    printf '\\gset\n\\if :pending\n\\echo Applying %s\n' "$filename"
    cat -- "$path"
    printf "\nINSERT INTO public.schema_migrations (filename) VALUES ('%s');\n" "$filename"
    printf '\\endif\n'
done

printf 'COMMIT;\n'
