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
from .slack_app import SlackUI, build_app
from .sources import FIELDS, Sources
from .storage import connect, connect_database, enqueue
from .worker import Worker, ingest_mqtt


def dependencies():
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
    ledger = Ledger(store, sources)
    api = ChatAPI(os.environ.get("LEDGER_LLM_BASE_URL", "http://localhost:8000/v1"), os.environ.get("LEDGER_LLM_MODEL", DEFAULT_MODEL), os.environ.get("LEDGER_LLM_API_KEY", ""))
    composer = Composer(store, api)
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
        if subscribe and not reason_code.is_failure:
            c.subscribe([(f"{collection}/+", 1) for collection in FIELDS])
    def on_message(c, userdata, msg):
        try:
            ingest_mqtt(store, msg.topic, msg.payload)
        except Exception as exc:
            logging.warning("MQTT trigger not persisted: %s; periodic reconciliation will recover", type(exc).__name__)
    client.on_connect, client.on_message = on_connect, on_message
    client.connect(os.environ["MQTT_HOST"], int(os.environ.get("MQTT_PORT", "1883")), 60)
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
    parser.add_argument("action", choices=["init", "bootstrap", "serve", "worker", "reconcile", "dry-run"])
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--queue", choices=["all", "inbox", "outbox", "channels"], default="all")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.action == "serve":
        with make_server("0.0.0.0", args.port, make_app()) as server:
            server.serve_forever()
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
        connection = broker(ledger.store, subscribe=args.queue in ("all", "inbox")) if args.queue != "channels" else None
        worker = Worker(ledger, composer, client, connection, os.environ["SLACK_BOT_USER_ID"])
        stop = Event()
        def run(queue):
            last = 0
            channel_kinds = ["remove", "invite", "provision_slot"]
            while not stop.is_set():
                try:
                    if queue == "inbox" and time.monotonic() - last >= 300:
                        ledger.store.atomic(lambda s: enqueue(s, "ledger_inbox", f"periodic:{int(time.time() // 300)}", "reconcile", {}))
                        last = time.monotonic()
                    worked = worker.step("ledger_inbox") if queue == "inbox" else worker.step(
                        "ledger_outbox", kinds=channel_kinds if queue == "channels" else None,
                        exclude=channel_kinds if queue == "outbox" else None)
                    if not worked:
                        stop.wait(0.25)
                except Exception as exc:
                    logging.warning("Worker queue %s unavailable: %s", queue, type(exc).__name__)
                    stop.wait(2)
        queues = ["inbox", "outbox", "channels"] if args.queue == "all" else [args.queue]
        threads = [Thread(target=run, args=(q,), name=q, daemon=True) for q in queues]
        for thread in threads:
            thread.start()
        try:
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
