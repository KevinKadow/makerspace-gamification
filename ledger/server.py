"""Select HTTP ingress or a single reconnecting Slack Socket Mode consumer."""
import logging
import os
import signal
from wsgiref.simple_server import make_server

from slack_bolt.adapter.socket_mode.websocket_client import SocketModeHandler

HTTP_COMMAND = ["gunicorn", "--bind", "0.0.0.0:3000", "--workers", "2", "--timeout", "30",
                "--access-logfile", "-", "--access-logformat",
                "%(m)s %(U)s status=%(s)s duration_s=%(L)s", "ledger.cli:make_app()"]


def socket_mode_enabled():
    value = os.environ.get("SLACK_SOCKET_MODE", "false").strip().lower()
    if value not in {"true", "false", "1", "0", "yes", "no", ""}:
        raise ValueError("SLACK_SOCKET_MODE must be true or false")
    return value in {"true", "1", "yes"}


def serve_socket_mode(port=3000):
    from .cli import make_app
    token = os.environ.get("SLACK_APP_TOKEN", "").strip()
    if not token.startswith("xapp-"):
        raise ValueError("Socket Mode requires SLACK_APP_TOKEN (xapp-) with connections:write")
    app = make_app(socket_mode=True)
    handler = SocketModeHandler(app.bolt, token)
    app.transport_ready = handler.client.is_connected

    def stop(signum, frame):
        raise SystemExit(0)

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        # Bind health before connecting; startup failure closes both resources.
        with make_server("0.0.0.0", port, app) as server:
            handler.connect()
            logging.info("Slack Socket Mode started (websocket-client); HTTP serves health/readiness only")
            server.serve_forever()
    finally:
        handler.close()
        signal.signal(signal.SIGTERM, previous)


def main():
    if not socket_mode_enabled():
        os.execvp(HTTP_COMMAND[0], HTTP_COMMAND)
        return
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    serve_socket_mode()


if __name__ == "__main__":
    main()
