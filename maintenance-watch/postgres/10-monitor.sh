#!/bin/sh
set -eu
psql -v ON_ERROR_STOP=1 --single-transaction --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=bi_password="$MAINTENANCE_BI_PASSWORD" \
  --file=- --file=/opt/maintenance/storage_schema.sql --file=/opt/maintenance/powerbi.sql <<'SQL'
CREATE ROLE maintenance_bi LOGIN PASSWORD :'bi_password';
SQL
