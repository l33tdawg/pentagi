"""Synthetic vulnerable webapp for the SAGE benchmark.

Endpoint: GET /ping?host=<host>
Behaviour: shells out via os.system, deliberately concatenating user input.
This reproduces the *shape* of a command-injection vulnerability without
using any real-world CVE payload. Do not deploy outside the benchmark.
"""

import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs


BANNER = (
    "<html><body><h1>NetTools v0.1</h1>"
    "<p>Powered by NetTools/0.1 (synthetic-vuln-app for SAGE benchmark)</p>"
    "<form action='/ping'><input name='host' value='127.0.0.1'>"
    "<button>ping</button></form></body></html>"
)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return  # keep stdout clean during benchmark

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, "text/html", BANNER)
            return
        if parsed.path == "/ping":
            host = parse_qs(parsed.query).get("host", ["127.0.0.1"])[0]
            # Deliberately vulnerable: user input concatenated into a shell.
            cmd = f"ping -c1 -W1 {host} 2>&1 || true"
            output = os.popen(cmd).read()
            self._send(200, "text/plain", output)
            return
        self._send(404, "text/plain", "not found")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Server", "NetTools/0.1")
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body.encode())


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
