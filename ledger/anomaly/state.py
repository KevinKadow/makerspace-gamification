"""Transactional local PostgreSQL state. Only curated views are exportable."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
from threading import RLock
from .security_state import SecurityState, DDL as SECURITY_DDL


def stamp():
    return datetime.now(timezone.utc).isoformat()


DDL = """
CREATE TABLE IF NOT EXISTS anomaly_documents (
  namespace text NOT NULL, key text NOT NULL, data jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(namespace,key));
CREATE INDEX IF NOT EXISTS anomaly_documents_updated ON anomaly_documents(namespace,updated_at);
CREATE INDEX IF NOT EXISTS anomaly_documents_prefix ON anomaly_documents(namespace,key text_pattern_ops);
CREATE TABLE IF NOT EXISTS anomaly_events (
  id text PRIMARY KEY, collection text NOT NULL, data jsonb NOT NULL,
  processed boolean NOT NULL DEFAULT false, created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS anomaly_events_pending ON anomaly_events(processed,created_at);
CREATE TABLE IF NOT EXISTS anomaly_checkpoints (collection text PRIMARY KEY, token jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now());
CREATE SCHEMA IF NOT EXISTS reporting;
CREATE OR REPLACE VIEW reporting.cohort_metrics AS
 SELECT data->>'report' report, data->>'dimension' dimension, data->>'bucket' bucket,
 (data->>'value')::double precision value, (data->>'sample_size')::integer sample_size,
 data->>'as_of' as_of FROM anomaly_documents WHERE namespace='report_metrics'
 UNION ALL
 SELECT 'security_' || (data->>'report'), 'security', data->>'cohort',
 (data->>'value')::double precision, (data->>'sample_size')::integer,
 data->>'period_end' FROM anomaly_documents WHERE namespace='security_report_metrics'
 AND (data->>'sample_size')::integer >= 5;
CREATE OR REPLACE VIEW reporting.findings_summary AS
 WITH counts AS (
 SELECT data->>'collection' collection, data->>'rule' rule, data->>'severity' severity,
 count(*)::bigint count, count(DISTINCT data->>'entity') population,
 min(data->>'detected_at') first_seen, max(data->>'detected_at') last_seen
 FROM anomaly_documents WHERE namespace='findings' AND data->>'status'='open' GROUP BY 1,2,3)
 SELECT collection,rule,severity,count,first_seen,last_seen FROM counts
 WHERE collection IN (SELECT collection FROM counts GROUP BY collection HAVING min(population) >= 5);
CREATE OR REPLACE VIEW reporting.pipeline_health AS
 SELECT key component, data->>'status' status, updated_at, data->>'rails_commit' rails_commit,
 data->>'contract_hash' contract_hash, data->>'extractor_version' extractor_version,
 data->>'build_time' build_time FROM anomaly_documents WHERE namespace='health';
CREATE OR REPLACE VIEW reporting.model_quality AS
 SELECT data->>'outcome' outcome, data->>'model_version' model_version, data->>'metric' metric,
 (data->>'value')::double precision value, (data->>'sample_size')::integer sample_size,
 data->>'as_of' as_of FROM anomaly_documents WHERE namespace='model_metrics' AND (data->>'sample_size')::integer >= 5;
"""


class State(SecurityState):
    def __init__(self, url):
        import psycopg
        from psycopg.rows import dict_row
        self.connection = psycopg.connect(url, autocommit=True, row_factory=dict_row, connect_timeout=10)
        self.lock = RLock()

    def initialize(self, reporting_user="anomaly_reporter", minimum_cohort=5):
        from psycopg import sql
        if type(minimum_cohort) is not int or minimum_cohort < 5:
            raise ValueError("Reporting cohorts require at least five members")
        with self.atomic():
            self.connection.execute(SECURITY_DDL)
            self.connection.execute(DDL.replace("min(population) >= 5", f"min(population) >= {minimum_cohort}")
                                    .replace("::integer >= 5", f"::integer >= {minimum_cohort}"))
            # Only an existing deployment-provisioned role can receive these privileges.
            if self.connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (reporting_user,)).fetchone():
                self.connection.execute(sql.SQL("GRANT USAGE ON SCHEMA reporting TO {}").format(sql.Identifier(reporting_user)))
                for view in ("cohort_metrics", "findings_summary", "pipeline_health", "model_quality"):
                    self.connection.execute(sql.SQL("GRANT SELECT ON reporting.{} TO {}").format(
                        sql.Identifier(view), sql.Identifier(reporting_user)))

    def ready(self):
        with self.lock:
            self.connection.execute("SELECT 1").fetchone()

    def acquire_leader(self):
        with self.lock:
            return self.connection.execute("SELECT pg_try_advisory_lock(731834920) AS acquired").fetchone()["acquired"]

    @contextmanager
    def atomic(self):
        with self.lock, self.connection.transaction():
            yield self

    def get(self, namespace, key, default=None):
        with self.lock:
            row = self.connection.execute("SELECT data FROM anomaly_documents WHERE namespace=%s AND key=%s",
                                          (namespace, str(key))).fetchone()
            return row["data"] if row else deepcopy(default)

    def put(self, namespace, key, data):
        from psycopg.types.json import Jsonb
        with self.lock:
            self.connection.execute("INSERT INTO anomaly_documents(namespace,key,data) VALUES(%s,%s,%s) "
                "ON CONFLICT(namespace,key) DO UPDATE SET data=EXCLUDED.data,updated_at=now()",
                (namespace, str(key), Jsonb(data)))

    def delete(self, namespace, key):
        with self.lock:
            self.connection.execute("DELETE FROM anomaly_documents WHERE namespace=%s AND key=%s", (namespace, str(key)))

    def rows(self, namespace, prefix=None):
        # Bounded pages; callers can stream large historical feature sets.
        last = ""
        while True:
            with self.lock:
                query = "SELECT key,data FROM anomaly_documents WHERE namespace=%s AND key>%s "
                params = [namespace, last]
                if prefix is not None:
                    query += "AND key LIKE %s "
                    params.append(prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
                rows = self.connection.execute(query + "ORDER BY key LIMIT 500", params).fetchall()
            if not rows:
                return
            for row in rows:
                yield row["key"], row["data"]
            last = rows[-1]["key"]

    def capture(self, event_id, collection, data, token=None):
        from psycopg.types.json import Jsonb
        with self.atomic():
            inserted = self.connection.execute("INSERT INTO anomaly_events(id,collection,data) VALUES(%s,%s,%s) "
                "ON CONFLICT DO NOTHING RETURNING id", (event_id, collection, Jsonb(data))).fetchone()
            if token is not None:
                self.advance_checkpoint(collection, token)
            return bool(inserted)

    def advance_checkpoint(self, collection, token):
        """Commit an idle post-batch token without retaining a synthetic event."""
        from psycopg.types.json import Jsonb
        with self.lock:
            self.connection.execute("INSERT INTO anomaly_checkpoints(collection,token) VALUES(%s,%s) "
                "ON CONFLICT(collection) DO UPDATE SET token=EXCLUDED.token,updated_at=now() "
                "WHERE anomaly_checkpoints.token IS DISTINCT FROM EXCLUDED.token", (collection, Jsonb(token)))

    def checkpoint(self, collection):
        with self.lock:
            row = self.connection.execute("SELECT token FROM anomaly_checkpoints WHERE collection=%s", (collection,)).fetchone()
            return row["token"] if row else None

    def pending(self, limit=100):
        with self.lock:
            return self.connection.execute("SELECT id,collection,data FROM anomaly_events WHERE NOT processed "
                "ORDER BY created_at,id LIMIT %s", (limit,)).fetchall()

    def processed(self, event_id):
        with self.lock:
            self.connection.execute("UPDATE anomaly_events SET processed=true WHERE id=%s", (event_id,))

    def prune(self, event_days, history_months):
        with self.atomic():
            self.security_prune(history_months)
            self.connection.execute("DELETE FROM anomaly_events WHERE processed AND created_at < now() - %s * interval '1 day'", (event_days,))
            self.connection.execute("DELETE FROM anomaly_documents WHERE namespace IN ('features','outcomes','transitions') "
                "AND COALESCE(data->>'as_of', data->>'at')::timestamptz < now() - %s * interval '1 month'", (history_months,))
            self.connection.execute("DELETE FROM anomaly_documents WHERE namespace='query_results' AND key IN "
                "(SELECT key FROM anomaly_documents WHERE namespace='query_receipts' "
                "AND (data->>'completed_at')::timestamptz < now() - %s * interval '1 day')", (event_days,))
            self.connection.execute("DELETE FROM anomaly_documents WHERE namespace='query_receipts' "
                "AND (data->>'completed_at')::timestamptz < now() - %s * interval '1 day'", (event_days,))
            self.connection.execute("DELETE FROM anomaly_documents WHERE namespace IN ("
                "'detector_receipts','alert_budgets','slack_sent_budgets','alert_cooldowns') "
                "AND updated_at < now() - %s * interval '1 day'", (event_days,))
            self.connection.execute("DELETE FROM anomaly_documents WHERE namespace='deliveries' AND data->>'status' IN ('delivered','cancelled') "
                "AND updated_at < now() - %s * interval '1 day'", (event_days,))

    def close(self):
        self.connection.close()


class MemoryState(SecurityState):
    """Deterministic transactional test double; never selected by deployment configuration."""
    def __init__(self):
        self.documents, self.events, self.tokens = {}, {}, {}
        self.lock = RLock()

    @contextmanager
    def atomic(self):
        with self.lock:
            previous = deepcopy((self.documents, self.events, self.tokens))
            try:
                yield self
            except BaseException:
                self.documents, self.events, self.tokens = previous
                raise

    def get(self, namespace, key, default=None):
        return deepcopy(self.documents.get((namespace, str(key)), default))

    def put(self, namespace, key, data):
        self.documents[namespace, str(key)] = deepcopy(data)

    def delete(self, namespace, key):
        self.documents.pop((namespace, str(key)), None)

    def rows(self, namespace, prefix=None):
        return iter(sorted((k, deepcopy(v)) for (n, k), v in self.documents.items()
                           if n == namespace and (prefix is None or k.startswith(prefix))))

    def capture(self, event_id, collection, data, token=None):
        with self.atomic():
            new = event_id not in self.events
            self.events.setdefault(event_id, {"id": event_id, "collection": collection, "data": deepcopy(data), "processed": False})
            if token is not None:
                self.advance_checkpoint(collection, token)
            return new

    def advance_checkpoint(self, collection, token):
        with self.lock:
            self.tokens[collection] = deepcopy(token)

    def checkpoint(self, collection):
        return deepcopy(self.tokens.get(collection))

    def pending(self, limit=100):
        return [deepcopy(v) for v in self.events.values() if not v["processed"]][:limit]

    def processed(self, event_id):
        self.events[event_id]["processed"] = True

    def ready(self):
        pass
