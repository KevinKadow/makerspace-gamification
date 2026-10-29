"""python -m ledger.anomaly: standalone service, preflight, and explicit setup."""
import argparse
import json
import logging
import os
import signal

from .config import Config
from .ingest import verify_source
from .schema import load_manifest
from .state import State
from ..storage import connect_database


def preflight_manifest(state, manifest):
    """Include retained effective collections without installing or changing a schema."""
    try:
        active = state.get("schemas", "active", {})
    except Exception as exc:
        if getattr(exc, "sqlstate", None) != "42P01":
            raise
        active = {}  # A running, newly provisioned PostgreSQL has no runtime tables yet.
    return {**manifest, "collections": {**active.get("collections", {}), **manifest["collections"]}}


def main():
    parser = argparse.ArgumentParser(description="Ledger read-only anomaly analytics")
    parser.add_argument("action", choices=("run", "preflight", "ready", "bootstrap-metabase"))
    parser.add_argument("--source-only", action="store_true", help="Preflight before the first PostgreSQL startup")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = Config.from_env()
    manifest = load_manifest(config.schema_path)
    source = connect_database(config.source_uri, config.source_database or None)
    if args.action == "preflight":
        required = manifest
        try:
            if not args.source_only:
                state = State(config.database_url)
                try:
                    state.ready()
                    required = preflight_manifest(state, manifest)
                finally:
                    state.close()
            verify_source(source, required)
        finally:
            source.client.close()
        print(json.dumps({"status": "passed", "collections": len(required["collections"]),
                          "required_collections": sorted(required["collections"]),
                          "retained_collections": sorted(set(required["collections"]) - set(manifest["collections"])),
                          "rails_commit": manifest.get("provenance", {}).get("rails_commit")}))
        return
    state = State(config.database_url)
    try:
        if args.action == "ready":
            from .service import readiness
            print(json.dumps(readiness(state, source)))
        elif args.action == "bootstrap-metabase":
            from .metabase import bootstrap
            result = bootstrap(os.environ)
            state.put("metabase", "bootstrap", result)
            print(json.dumps(result))
        else:
            from .service import Service
            state.initialize(os.environ.get("ANOMALY_REPORTING_USER", "anomaly_reporter"), config.minimum_cohort)
            if not state.acquire_leader():
                raise RuntimeError("Only one analytics runtime may own this state database")
            service = Service(state, source, manifest, config)
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, lambda *_: service.ingestor.stop.set())
            service.run()
    finally:
        source.client.close()
        state.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Connection strings, raw source values and provider bodies are never printed.
        logging.error("Anomaly command failed: %s", type(exc).__name__)
        if isinstance(exc, PermissionError) and str(exc).startswith("Read/changeStream preflight failed for: "):
            logging.error("%s", str(exc))
        raise SystemExit(1)
