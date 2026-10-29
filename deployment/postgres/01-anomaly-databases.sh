#!/usr/bin/env bash
# Runs once on a fresh official Postgres volume. Never prints credentials.
set -Eeuo pipefail
: "${ANOMALY_DB_PASSWORD:?Set ANOMALY_DB_PASSWORD}"
: "${ANOMALY_REPORTING_PASSWORD:?Set ANOMALY_REPORTING_PASSWORD}"
: "${METABASE_ANOMALY_DB_PASSWORD:?Set METABASE_ANOMALY_DB_PASSWORD}"
: "${METABASE_RAW_DB_PASSWORD:?Set METABASE_RAW_DB_PASSWORD}"
psql --username "$POSTGRES_USER" --dbname postgres --set ON_ERROR_STOP=1 <<'SQL'
\getenv anomaly_password ANOMALY_DB_PASSWORD
\getenv reporting_password ANOMALY_REPORTING_PASSWORD
\getenv mb_anomaly_password METABASE_ANOMALY_DB_PASSWORD
\getenv mb_raw_password METABASE_RAW_DB_PASSWORD
CREATE ROLE anomaly_writer LOGIN PASSWORD :'anomaly_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE anomaly_reporter LOGIN PASSWORD :'reporting_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
ALTER ROLE anomaly_reporter SET default_transaction_read_only = on;
CREATE ROLE metabase_anomaly LOGIN PASSWORD :'mb_anomaly_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE metabase_raw LOGIN PASSWORD :'mb_raw_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE DATABASE ledger_anomaly OWNER anomaly_writer;
CREATE DATABASE metabase_anomaly OWNER metabase_anomaly;
CREATE DATABASE metabase_raw OWNER metabase_raw;
REVOKE ALL ON DATABASE ledger_anomaly FROM PUBLIC;
REVOKE ALL ON DATABASE metabase_anomaly FROM PUBLIC;
REVOKE ALL ON DATABASE metabase_raw FROM PUBLIC;
GRANT CONNECT ON DATABASE ledger_anomaly TO anomaly_writer, anomaly_reporter;
GRANT CONNECT ON DATABASE metabase_anomaly TO metabase_anomaly;
GRANT CONNECT ON DATABASE metabase_raw TO metabase_raw;
\connect ledger_anomaly
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
-- Runtime creates tables and grants only enumerated reporting views to anomaly_reporter.
-- It must not grant SELECT on all tables or default SELECT on future tables.
SQL
