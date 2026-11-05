#!/usr/bin/env python3
"""Latency / throughput probe. Pure stdlib, no deps.

Measures the real client -> CDN -> origin path:
  - RTT on a reused connection (pure network round trip)
  - RTT on a fresh connection (DNS + TCP + TLS + request)
  - downlink throughput
  - uplink throughput
  - server-paced streaming: client compares arrival cadence with what the
    server actually wrote, which separates "sender slow" from "receiver slow"
"""
import json
import os
import random
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "index.html")
BLOB = random.randbytes(4 * 1024 * 1024)


def no_store(h):
    h.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
    h.send_header("Pragma", "no-cache")
    h.send_header("CDN-Cache-Control", "no-store")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "latency-probe/1"

    def log_message(self, fmt, *args):
        pass

    def _nodelay(self):
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass

    def _close_after(self, q):
        return q.get("close", ["0"])[0] == "1"

    def _head_common(self, code, ctype, length, close):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        no_store(self)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Timing-Allow-Origin", "*")
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Content-Length", "0")
        no_store(self)
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        self._nodelay()
        close = self._close_after(q)

        if u.path in ("/", "/index.html"):
            with open(INDEX, "rb") as f:
                body = f.read()
            self._head_common(200, "text/html; charset=utf-8", len(body), False)
            self.wfile.write(body)
            return

        if u.path == "/api/ping":
            body = json.dumps({
                "server_ms": int(time.time() * 1000),
                "mono_ms": int(time.monotonic() * 1000),
            }).encode()
            self._head_common(200, "application/json", len(body), close)
            self.wfile.write(body)
            return

        if u.path == "/api/download":
            kb = max(1, min(64 * 1024, int(q.get("kb", ["1024"])[0])))
            n = kb * 1024
            self._head_common(200, "application/octet-stream", n, close)
            sent = 0
            while sent < n:
                take = min(256 * 1024, n - sent)
                self.wfile.write(BLOB[sent % (256 * 1024):sent % (256 * 1024) + take])
                sent += take
            return

        if u.path == "/api/stream":
            chunks = max(1, min(20000, int(q.get("chunks", ["300"])[0])))
            size = max(1024, min(1024 * 1024, int(q.get("size", ["16384"])[0])))
            interval = max(0, min(5000, int(q.get("interval", ["20"])[0])))
            total = chunks * size
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            # Chunked, length unknown — mirrors how the real SSE endpoint
            # behaves. A declared Content-Length lets proxies buffer the whole
            # body, which would hide the very stall we are hunting.
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Timing-Allow-Origin", "*")
            no_store(self)
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            payload = BLOB[:size]
            frame = ("%x\r\n" % size).encode() + payload + b"\r\n"
            t0 = time.monotonic()
            try:
                for i in range(chunks):
                    self.wfile.write(frame)
                    self.wfile.flush()
                    if interval:
                        target = t0 + (i + 1) * interval / 1000.0
                        delay = target - time.monotonic()
                        if delay > 0:
                            time.sleep(delay)
            except (BrokenPipeError, ConnectionResetError):
                return
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        if u.path == "/api/info":
            body = json.dumps({
                "host": self.headers.get("Host", ""),
                "proto": self.request_version,
                "client": self.client_address[0],
                "xff": self.headers.get("X-Forwarded-For", ""),
                "cf_ray": self.headers.get("CF-Ray", ""),
                "cf_colo": self.headers.get("CF-IPCountry", ""),
            }).encode()
            self._head_common(200, "application/json", len(body), close)
            self.wfile.write(body)
            return

        self._head_common(404, "text/plain", 9, close)
        self.wfile.write(b"not found")

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        self._nodelay()
        close = self._close_after(q)
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        left = n
        while left > 0:
            buf = self.rfile.read(min(256 * 1024, left))
            if not buf:
                break
            left -= len(buf)
        body = json.dumps({"received": n - left}).encode()
        self._head_common(200, "application/json", len(body), close)
        self.wfile.write(body)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8005
    ThreadingHTTPServer.allow_reuse_address = True
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    print("latency probe on 0.0.0.0:%d" % port, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
