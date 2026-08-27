#!/usr/bin/env python3
"""Serve recorded VMware API responses on ports 8080/8081/8082.

The orchestrator derives its three API base URLs from MCP_SERVER by appending
ports, so a stand-in has to occupy all three. Pointing MCP_SERVER at
http://localhost makes the whole stack run with no datacentre access at all -
useful on a plane, in a meeting room, or anywhere the real estate is not
reachable.

Fixtures are matched by port and path. An unmatched request returns 404 with
the names it looked for, so a missing recording is obvious rather than
appearing as an empty answer.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

FIXTURES = Path(__file__).parent / "fixtures"
PORTS = (8080, 8081, 8082)


def fixture_name(port: int, path: str) -> str:
    """Mirror the naming used by the capture script."""
    cleaned = path.strip("/").replace("/", "_").replace("?", "_")
    cleaned = cleaned.replace("&", "_").replace("=", "_")
    return f"{port}-{cleaned}"


def find_fixture(port: int, path: str):
    """Exact match first, then the path without its query string, then prefix."""
    parsed = urlparse(path)
    candidates = [
        fixture_name(port, path),
        fixture_name(port, parsed.path),
    ]
    for name in candidates:
        candidate = FIXTURES / f"{name}.json"
        if candidate.exists():
            return candidate, candidates

    # Fall back to the longest recorded path that prefixes this one, so
    # /ni/flows?source=x still resolves to a /ni/flows recording.
    stem = fixture_name(port, parsed.path)
    matches = sorted(
        (f for f in FIXTURES.glob(f"{port}-*.json") if stem.startswith(f.stem) or f.stem.startswith(stem)),
        key=lambda f: len(f.stem),
        reverse=True,
    )
    return (matches[0] if matches else None), candidates


class ReplayHandler(BaseHTTPRequestHandler):
    port = 0

    def _respond(self):
        path = self.path
        fixture, tried = find_fixture(self.port, path)

        if fixture is None:
            body = json.dumps({
                "error": "no fixture recorded for this request",
                "port": self.port,
                "path": path,
                "tried": tried,
                "hint": "Record it with capture.sh against a live estate.",
            }).encode()
            self.send_response(404)
        else:
            body = fixture.read_bytes()
            self.send_response(200)

        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _respond
    do_POST = _respond

    def log_message(self, fmt, *args):
        sys.stderr.write(f"  [{self.port}] {fmt % args}\n")


def serve(port: int):
    handler = type(f"Handler{port}", (ReplayHandler,), {"port": port})
    HTTPServer(("0.0.0.0", port), handler).serve_forever()


def main() -> int:
    if not FIXTURES.exists():
        print(f"No fixtures directory at {FIXTURES}", file=sys.stderr)
        return 1

    recorded = sorted(FIXTURES.glob("*.json"))
    print(f"Replaying {len(recorded)} fixtures on ports {', '.join(map(str, PORTS))}")
    for f in recorded:
        print(f"  {f.stem}")

    for port in PORTS[:-1]:
        threading.Thread(target=serve, args=(port,), daemon=True).start()
    serve(PORTS[-1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
