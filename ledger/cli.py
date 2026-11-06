import argparse
import json
import logging
import os
import time
from pathlib import Path
from threading import Event, Thread
from wsgiref.simple_server import make_server

import paho.mqtt.client as mqtt
from slack_sdk import WebClient

from .domain import Ledger
from .http import HTTPApp
from .messages import DEFAULT_MODEL, ChatAPI, Composer
from .prompt_matrix import PromptMatrix
from .slack_app import SlackUI, build_app
from .sources import FIELDS, Sources
from .storage import connect, connect_database, enqueue
from .worker import Worker, ingest_mqtt


CHANNEL_KINDS = ["remove", "invite", "provision_slot", "review_channel_invite"]
RESULT_KINDS = ["summary_flush", "summary_delivery"]
INTERACTIVE_KINDS = ["conversation", "guidance"]


def outbox_filters(queue):
    """Disjoint lanes: slow routine delivery cannot claim interactive/results work."""
    if queue == "channels":
        return {"kinds": CHANNEL_KINDS}
    if queue == "results":
        return {"kinds": RESULT_KINDS}
    if queue == "interactive":
        return {"kinds": INTERACTIVE_KINDS}
    return {"exclude": CHANNEL_KINDS + RESULT_KINDS + INTERACTIVE_KINDS}


def database_ledger():
    # Legacy MONGO_URI is a compatibility fallback only. Never use one new
    # credential for the other connection when its counterpart is missing.
    legacy_uri = os.environ.get("MONGO_URI")
    source_uri = os.environ.get("MLAB_URI") or legacy_uri
    ledger_uri = os.environ.get("LEDGER_URI") or legacy_uri
    if not source_uri or not ledger_uri:
        raise ValueError("Configure both MLAB_URI and LEDGER_URI (or legacy MONGO_URI).")
    legacy_database = os.environ.get("MONGO_DATABASE")
    store = connect(ledger_uri, os.environ.get("LEDGER_DATABASE") or legacy_database)
    sources = Sources(connect_database(source_uri, os.environ.get("MLAB_DATABASE") or legacy_database))
    return Ledger(store, sources)


def dependencies():
    ledger = database_ledger()
    store = ledger.store
    api = ChatAPI(os.environ.get("LEDGER_LLM_BASE_URL", "http://localhost:8000/v1"), os.environ.get("LEDGER_LLM_MODEL", DEFAULT_MODEL), os.environ.get("LEDGER_LLM_API_KEY", ""))
    composer = Composer(store, api, matrix=PromptMatrix.from_env())
    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"], timeout=10, retry_handlers=[])
    return ledger, composer, client


def make_app():
    ledger, composer, client = dependencies()
    # Modal trigger IDs expire quickly. Ingress never spends the delivery worker's
    # longer timeout budget on Slack lookups or opening a view.
    client.timeout = 1
    # Authentication and database bootstrapping are explicit deployment steps.
    app = build_app(SlackUI(ledger, composer), os.environ["SLACK_BOT_TOKEN"], os.environ["SLACK_SIGNING_SECRET"],
                    os.environ["SLACK_TEAM_ID"], os.environ["SLACK_BOT_USER_ID"], client)
    return HTTPApp(app, ledger.store, ledger.sources)


def broker(store, subscribe=True):
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=os.environ.get("MQTT_CLIENT_ID", "makerspace-ledger"), clean_session=False)
    if os.environ.get("MQTT_USER"):
        client.username_pw_set(os.environ["MQTT_USER"], os.environ.get("MQTT_PASSWORD"))
    if os.environ.get("MQTT_TLS", "false").lower() == "true":
        client.tls_set()
    def on_connect(c, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            logging.warning("MQTT connection rejected; background reconnect continues")
            return
        logging.info("MQTT connected source_subscription=%s", subscribe)
        if subscribe and not reason_code.is_failure:
            c.subscribe([(f"{collection}/+", 1) for collection in FIELDS if collection not in ("checkins", "cards")] + [("checkins/insert", 1)])
    def on_connect_fail(c, userdata):
        logging.warning("MQTT unavailable; reconnecting in background while Slack queues continue")
    def on_message(c, userdata, msg):
        try:
            ingest_mqtt(store, msg.topic, msg.payload, retained=bool(msg.retain))
        except Exception as exc:
            logging.warning("MQTT trigger not persisted: %s; periodic reconciliation will recover", type(exc).__name__)
    client.on_connect, client.on_message = on_connect, on_message
    client.on_connect_fail = on_connect_fail
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    # loop_start retries even the first connection on its network thread.
    # An unavailable broker must not prevent durable Slack queues from starting.
    client.connect_async(os.environ["MQTT_HOST"], int(os.environ.get("MQTT_PORT", "1883")), 60)
    client.loop_start()
    return client


def bootstrap(ledger, client):
    ledger.store.ready()
    ledger.sources.ready()
    ledger.store.indexes()
    ledger.seed()
    configured = json.loads(os.environ.get("LEDGER_CHANNELS") or "{}")
    ranks = ledger.store.get("ledger_catalog", "rank_display")["ranks"]
    entries = [("chat", "ledge-chat", 0)] + [(f"rank:{r['slot']}", "ledger-" + r["name"].lower().replace(" ", "-"), r["slot"]) for r in ranks if r["enabled"]]
    for key, name, slot in entries:
        old = ledger.store.get("ledger_channels", key)
        if old:
            configured[key] = old["channel_id"]
        if key in configured:
            channel = client.conversations_info(channel=configured[key])["channel"]
        else:
            channel = client.conversations_create(name=name, is_private=True)["channel"]
        if not channel.get("is_private") or channel.get("is_ext_shared"):
            raise ValueError("Ledger channels must be private and not externally shared")
        if not channel.get("is_member", True):
            raise ValueError("Invite The Ledger bot into each existing channel before bootstrap")
        ledger.store.atomic(lambda s: s.put("ledger_channels", {"_id": key, "kind": "channel", "channel_id": channel["id"], "slot": slot}))
    print("Ledger indexes, rules, and private channels are ready. Review and publish shop completion catalogs before pilot invitations.")


def main():
    parser = argparse.ArgumentParser(description="The Ledger")
    parser.add_argument("action", choices=["init", "bootstrap", "serve", "worker", "reconcile", "dry-run", "prompt-matrix", "prepare-reads"])
    parser.add_argument("--verify", action="store_true", help="Compare prepared optimized reads with legacy reads without sending messages")
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--queue", choices=["all", "inbox", "outbox", "channels", "engagement", "results", "interactive"], default="all")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.action == "prompt-matrix":
        matrix = PromptMatrix.from_env()
        status = matrix.refresh()
        print(json.dumps(status))
        if status["outcome"] != "loaded":
            raise SystemExit(1)
        return
    if args.action == "serve":
        with make_server("0.0.0.0", args.port, make_app()) as server:
            server.serve_forever()
        return
    if args.action == "prepare-reads":
        from .read_preparation import prepare, verify
        ledger = database_ledger()
        ledger.store.ready()
        ledger.sources.ready()
        result = prepare(ledger)
        if args.verify:
            result["verification"] = verify(ledger)
        print(json.dumps(result, default=str))
        if args.verify and result["verification"]["mismatches"]:
            raise SystemExit(1)
        return
    ledger, composer, client = dependencies()
    ledger.store.ready()
    ledger.sources.ready()
    if args.action == "init":
        ledger.store.indexes()
        ledger.seed()
    elif args.action == "bootstrap":
        bootstrap(ledger, client)
    elif args.action == "dry-run":
        # Source-only inspection; no consent, XP, channel, or notification changes.
        shops = ledger.sources.rows("shops", {"disabled": {"$ne": True}})
        tools = ledger.sources.rows("tools", {"disabled": {"$ne": True}, "open": {"$ne": True}})
        print(json.dumps({"eligible_shops": len(shops), "checkout_tools": len(tools),
                          "participants": len(ledger.store.select('ledger_participants'))}))
    elif args.action == "reconcile":
        ledger.store.atomic(lambda s: enqueue(s, "ledger_inbox", f"manual:{time.time_ns()}", "reconcile", {}))
    elif args.action == "worker":
        connection = broker(ledger.store, subscribe=args.queue in ("all", "inbox")) if args.queue in ("all", "inbox", "outbox") else None
        worker = Worker(ledger, composer, client, connection, os.environ["SLACK_BOT_USER_ID"])
        if args.queue in ("all", "inbox"):
            from .catalog_cache import schedule_refresh
            schedule_refresh(ledger.store)
        stop = Event()
        def run(queue):
            last = 0
            last_ticket_scan = 0
            last_metrics = 0
            while not stop.is_set():
                try:
                    if time.monotonic() - last_metrics >= 60:
                        from .read_metrics import log_metrics
                        log_metrics()
                        last_metrics = time.monotonic()
                    if queue == "inbox" and time.monotonic() - last >= 300:
                        ledger.store.atomic(lambda s: enqueue(s, "ledger_inbox", f"periodic:{int(time.time() // 300)}", "reconcile", {}))
                        if time.monotonic() - last_ticket_scan >= 3600:
                            ledger.store.atomic(lambda s: enqueue(s, "ledger_inbox", f"ticket-quest-reconcile:{int(time.time() // 3600)}", "ticket_quest_reconcile", {}))
                            last_ticket_scan = time.monotonic()
                        last = time.monotonic()
                    worked = worker.step("ledger_inbox", exclude=["engagement"]) if queue == "inbox" else worker.step("ledger_inbox", kinds=["engagement"]) if queue == "engagement" else worker.step(
                        "ledger_outbox", **outbox_filters(queue))
                    if not worked:
                        stop.wait(0.25)
                except Exception as exc:
                    logging.warning("Worker queue %s unavailable: %s", queue, type(exc).__name__)
                    stop.wait(2)
        queues = ["inbox", "outbox", "channels", "engagement", "results", "interactive"] if args.queue == "all" else [args.queue]
        threads = [Thread(target=run, args=(q,), name=q, daemon=True) for q in queues]
        for thread in threads:
            thread.start()
        logging.info("Ledger worker started queues=%s", ",".join(queues))
        try:
            if args.queue != "channels":
                # The independent channel thread is already running, even if Docs is unavailable.
                composer.refresh_matrix()
            while not stop.wait(1):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=20)
            if connection:
                connection.disconnect()
                connection.loop_stop()


if __name__ == "__main__":
    main()
