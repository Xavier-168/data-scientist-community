import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


base = Path(os.environ["YIRENGONGIS_BASE_DIR"])
state = Path(os.environ["YIRENGONGIS_STATE_DIR"])
payload = json.loads((base / "package_manifest.json").read_text(encoding="utf-8"))["payload"]
delay = state / "ready-delay-ms"
if delay.is_file():
    time.sleep(int(delay.read_text(encoding="utf-8")) / 1000)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get("X-YRG-Session") != os.environ["YIRENGONGIS_SESSION_TOKEN"]:
            self.send_response(401)
            self.end_headers()
            return
        if self.path == "/supervised/health":
            body = {
                "ok": True,
                "package_id": payload["package_id"],
                "build_version": payload["build_version"],
                "port": self.server.server_port,
            }
        elif self.path == "/package-info":
            body = {
                "ok": True,
                "package_id": payload["package_id"],
                "build_version": payload["build_version"],
            }
        elif self.path == "/progress":
            body = {"ok": True, "running": False}
        else:
            self.send_response(404)
            self.end_headers()
            return
        encoded = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args):
        pass


preferred = int(os.environ["YIRENGONGIS_RUNNER_PORT"])
try:
    server = ThreadingHTTPServer(("127.0.0.1", preferred), Handler)
except OSError:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
frame = {
    "event": "ready",
    "port": server.server_port,
    "package_id": payload["package_id"],
    "build_version": payload["build_version"],
}
print("YRG_SIDECAR_READY " + json.dumps(frame, separators=(",", ":")), flush=True)
server.serve_forever()
