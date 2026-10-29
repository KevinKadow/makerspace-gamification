"""Private, single-flight supervisor for an on-demand vLLM-Omni subprocess.

Run on the GPU host with ``python -m ledger.avatar_runtime``. Bot containers
need only the authenticated HTTP interface, never a Docker socket.
"""
import base64
import hashlib
import hmac
import json
import logging
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests


log = logging.getLogger(__name__)


@contextmanager
def runtime_phase(runtime, name):
    started = time.monotonic()
    try:
        yield
    except Exception as exc:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        process = runtime.process
        exit_code = process.poll() if process is not None else None
        log.error("avatar_runtime phase=%s outcome=error exception=%s upstream_http_status=%s process_exit_code=%s duration_ms=%.2f",
                  name, type(exc).__name__, status if type(status) is int else "none",
                  exit_code if type(exit_code) is int else "none", (time.monotonic() - started) * 1000)
        raise
    else:
        log.info("avatar_runtime phase=%s outcome=success duration_ms=%.2f",
                 name, (time.monotonic() - started) * 1000)


class RuntimeBusy(RuntimeError):
    pass


class Runtime:
    def __init__(self, directory, command=None, base_url="http://127.0.0.1:8091"):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.command = command or ["vllm", "serve", "Qwen/Qwen-Image-2.1", "--omni",
                                   "--host", "127.0.0.1", "--port", "8091"]
        self.base_url = base_url
        self.lock = threading.Lock()
        self.process = None
        self.active = None
        self.touched = time.monotonic()
        self.closed = False

    def path(self, key):
        return self.directory / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def status(self, key):
        path = self.path(key)
        if path.exists():
            return json.loads(path.read_text())
        return {"status": "working" if self.active == key else "missing"}

    def _unload(self):
        if self.process is not None:
            # vLLM's child GPU workers must also exit before another load.
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=20)
            # The API parent can exit before a GPU child. Kill the remaining
            # process group as well; container init reaps the descendants.
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process = None

    def unload(self):
        if not self.lock.acquire(blocking=False):
            raise RuntimeBusy("Generation is still running")
        try:
            self._unload()
        finally:
            self.lock.release()

    def ack(self, key):
        # A missing receipt while generation is running is not a completed ack.
        if not self.lock.acquire(blocking=False):
            raise RuntimeBusy("Generation is still running")
        try:
            self.path(key).unlink(missing_ok=True)
        finally:
            self.lock.release()

    def _load(self):
        if self.closed:
            raise RuntimeError("Runtime is shutting down")
        if self.process is not None and self.process.poll() is None:
            return
        self._unload()
        # Never allow vLLM request logs to print reference images or prompts.
        try:
            with runtime_phase(self, "model_load"):
                self.process = subprocess.Popen(self.command, start_new_session=True,
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                deadline = time.monotonic() + 1200
                while not self.closed and time.monotonic() < deadline and self.process.poll() is None:
                    try:
                        if requests.get(self.base_url + "/health", timeout=2).ok:
                            return
                    except requests.RequestException:
                        pass
                    time.sleep(1)
                raise RuntimeError("Image runtime did not become healthy")
        except BaseException:
            self._unload()
            raise

    def generate(self, request):
        key = request["request_id"]
        fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        if not self.lock.acquire(blocking=False):
            raise RuntimeBusy("Another generation is running")
        try:
            saved = self.status(key)
            if saved["status"] == "done":
                if saved.get("fingerprint") != fingerprint:
                    raise ValueError("Request ID reused with different inputs")
                return saved
            refs = request["references"]
            if not 1 <= len(refs) <= 4 or not isinstance(request["prompt"], str) or len(request["prompt"]) > 4000:
                raise ValueError("Invalid avatar request")
            self.active = key
            self._load()
            files = [("image", (f"ref-{i}.png", base64.b64decode(ref, validate=True), "image/png"))
                     for i, ref in enumerate(refs)]
            with runtime_phase(self, "inference"):
                response = requests.post(self.base_url + "/v1/images/edits", files=files,
                    data={"model": "Qwen/Qwen-Image-2.1", "prompt": request["prompt"], "size": "1280x1280",
                          "n": "1", "num_inference_steps": "40", "true_cfg_scale": "1.0",
                          "seed": str(request["seed"]), "output_format": "png"}, timeout=(5, 1200))
                response.raise_for_status()
                data = response.json()
                result = {"status": "done", "fingerprint": fingerprint, "image": data["data"][0]["b64_json"],
                          "usage": data.get("usage"), "metrics": data.get("metrics", {}),
                          "encoder_tokens": None, "encoder_tokens_source": "unavailable"}
            # Atomic disk receipt survives a lost HTTP response and worker restart.
            path = self.path(key)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(result))
            temporary.replace(path)
            return result
        except BaseException:
            # A transport timeout must terminate inference, not leave an orphan.
            self._unload()
            raise
        finally:
            self.active = None
            self.touched = time.monotonic()
            self.lock.release()


def serve():
    token = os.environ["LEDGER_AVATAR_RUNTIME_KEY"]
    if len(token) < 24:
        raise ValueError("Use a runtime key of at least 24 characters")
    runtime = Runtime(os.environ.get("LEDGER_AVATAR_RUNTIME_SPOOL", "/var/lib/ledger-image"),
                      json.loads(os.environ["LEDGER_AVATAR_RUNTIME_COMMAND"]) if os.environ.get("LEDGER_AVATAR_RUNTIME_COMMAND") else None)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.respond(False)

        def do_POST(self):
            self.respond(True)

        def respond(self, post):
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.send_error(401)
                return
            try:
                if not post and self.path == "/health":
                    data = {"ok": True, "loaded": runtime.process is not None, "busy": runtime.active is not None}
                elif post:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 32 * 1024 * 1024:
                        raise ValueError("Invalid request size")
                    body = json.loads(self.rfile.read(size))
                    if self.path == "/generate":
                        data = runtime.generate(body)
                    elif self.path == "/status":
                        data = runtime.status(body["request_id"])
                    elif self.path == "/unload":
                        runtime.unload()
                        data = {"loaded": False}
                    elif self.path == "/ack":
                        runtime.ack(body["request_id"])
                        data = {"ok": True}
                    else:
                        self.send_error(404)
                        return
                else:
                    self.send_error(404)
                    return
                encoded = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except RuntimeBusy:
                self.send_error(409)
            except Exception as exc:
                # Never log exception text, request bodies or provider responses.
                log.error("avatar_runtime phase=request outcome=error exception=%s http_status=503", type(exc).__name__)
                self.send_error(503)

    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("LEDGER_AVATAR_RUNTIME_PORT", "8090"))), Handler)
    def watchdog():
        while not runtime.closed:
            time.sleep(5)
            if time.monotonic() - runtime.touched > 120:
                try:
                    runtime.unload()
                except RuntimeBusy:
                    pass
    threading.Thread(target=watchdog, daemon=True).start()
    def shutdown(*_):
        runtime.closed = True
        # Shutdown from another thread avoids HTTPServer's signal deadlock.
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever()
    finally:
        runtime.closed = True
        with runtime.lock:
            runtime._unload()
        server.server_close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    serve()
