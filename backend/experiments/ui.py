"""Minimal web UI for deep_research.py (stdlib HTTP server, no extra dependencies).

Usage:
    python ui.py                 # http://127.0.0.1:8000
    python ui.py --host 0.0.0.0  # inside Docker; publish the port to localhost only

POST /api/research streams newline-delimited JSON: progress `log` events, then one `result`.
The UI has no authentication, so do not expose it publicly.
"""

import argparse
import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from deep_research import redact, run_research

INDEX_HTML = (Path(__file__).parent / "ui.html").read_bytes()
MAX_QUESTION_CHARS = 2000
run_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "risalite-research"

    def do_GET(self) -> None:
        if self.path not in ("/", "/index.html"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(INDEX_HTML)))
        self.end_headers()
        self.wfile.write(INDEX_HTML)

    def do_POST(self) -> None:
        if self.path != "/api/research":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            question = str(json.loads(self.rfile.read(length) or b"{}").get("question", "")).strip()
        except (ValueError, AttributeError):
            question = ""
        if not question or len(question) > MAX_QUESTION_CHARS:
            self.send_error(HTTPStatus.BAD_REQUEST, "question is required (max 2000 chars)")
            return
        if not run_lock.acquire(blocking=False):
            self.send_error(HTTPStatus.CONFLICT, "a research run is already in progress")
            return
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.stream_research(question)
        finally:
            run_lock.release()

    def stream_research(self, question: str) -> None:
        connected = True

        def emit(event: dict) -> None:
            nonlocal connected
            if not connected:
                return
            try:
                self.wfile.write(json.dumps(event).encode() + b"\n")
                self.wfile.flush()
            except OSError:
                connected = False

        def log(message: str) -> None:
            print(message, flush=True)
            emit({"type": "log", "message": redact(message)})

        try:
            result = run_research(question, log=log)
        except SystemExit as exc:  # missing env var
            emit({"type": "result", "ok": False, "error": str(exc)})
            return
        log(result.verify_line())
        emit(
            {
                "type": "result",
                "ok": result.ok,
                "error": result.error,
                "report": result.report,
                "verify": result.verify_line(),
                "searches": result.stats.searches,
            }
        )

    def log_message(self, format: str, *args) -> None:
        print(f"[http] {self.address_string()} {format % args}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Web UI for deep_research.py")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[ui] serving on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
