"""R01 HTTP adapter. Serialized decisions, persistent per-team memory."""
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import time
from .policy import Agent
from .protocol import loads, empty_response

LOG = logging.getLogger(__name__)


def make_server(port: int, host: str = "0.0.0.0", journal=None):
    agents = {}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            started = time.perf_counter()
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0:
                    raise ValueError("Content-Length required")
                payload = loads(self.rfile.read(length).decode("utf-8"))
                key = (str(payload["teamOur"]["teamId"]), payload["teamOur"]["type"])
                with lock:
                    agent = agents.setdefault(key, Agent())
                    response = agent.decide(payload)
                    if journal is not None:
                        try:
                            journal.record(payload, response, agent.trace, (time.perf_counter() - started) * 1000)
                        except Exception:
                            LOG.exception("journal write failed; preserving decision response")
                LOG.info("round=%s team=%s actions=%s", payload["roundNo"], key,
                         len(response["roleCommandMap"]))
            except Exception:
                LOG.exception("request/decision failed; returning empty commands")
                response = empty_response()
            body = json.dumps(response, ensure_ascii=False, allow_nan=False).encode("utf-8")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionError):
                LOG.warning("client disconnected")

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
