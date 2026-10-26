#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lightweight Multi-Cloud Fleet Telemetry & Uptime Master Server
Collects heartbeat telemetry from distributed agents, computes real-time stats,
and renders the high-density Nezha/Hotaru-style web dashboard with security authentication.
"""

import os
import sys
import json
import time
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# Configuration via environment variables
PORT = int(os.getenv("PROBE_PORT", "28888"))
PROBE_TOKEN = os.getenv("PROBE_TOKEN", "secure_probe_token_example")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "AdminPass123!")
REQUIRE_LOGIN = os.getenv("REQUIRE_LOGIN", "true").lower() in ("true", "1", "yes")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
CONFIG_PATH = os.getenv("NODES_CONFIG", os.path.join(os.path.dirname(BASE_DIR), "config", "nodes.example.json"))

# Load node registry
NODES = {}
if os.path.exists(CONFIG_PATH):
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw_nodes = json.load(f)
            for nid, nmeta in raw_nodes.items():
                NODES[nid] = {
                    **nmeta,
                    "last_seen": 0,
                    "data": {}
                }
    except Exception as e:
        print(f"[-] Warning: Failed to load nodes config: {e}")

# Session storage for authenticated cookies
ACTIVE_SESSIONS = set()

def load_template(filename):
    filepath = os.path.join(TEMPLATE_DIR, filename)
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()
    return f"Template {filename} not found."

class TelemetryHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Silent running for stealth and low overhead

    def is_authenticated(self):
        if not REQUIRE_LOGIN:
            return True
        cookie_header = self.headers.get("Cookie", "")
        if "probe_session=" in cookie_header:
            for part in cookie_header.split(";"):
                part = part.strip()
                if part.startswith("probe_session="):
                    token = part.split("=", 1)[1]
                    if token in ACTIVE_SESSIONS:
                        return True
        return False

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # Health check
        if path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
            return

        # Login page
        if path == "/login":
            html = load_template("login.html")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
            return

        # Main Dashboard
        if path == "/" or path == "/index.html":
            if not self.is_authenticated():
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
                return

            html = load_template("index.html")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
            return

        # Telemetry API Status
        if path == "/api/status" or path == "/data":
            if not self.is_authenticated():
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "Unauthorized"}')
                return

            now = time.time()
            nodes_list = []
            for nid, node in NODES.items():
                is_online = (now - node.get("last_seen", 0)) < 15
                item = {
                    **node,
                    "online": is_online
                }
                nodes_list.append(item)

            data = {
                "updated_at": int(now),
                "nodes": nodes_list
            }
            res_bytes = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(res_bytes)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # Agent Telemetry Report
        if path == "/api/report" or path == "/report":
            auth = self.headers.get("X-Probe-Token") or self.headers.get("Authorization")
            if auth and auth.startswith("Bearer "):
                auth = auth[7:]

            if auth != PROBE_TOKEN:
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b'{"error": "Forbidden"}')
                return

            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                payload = json.loads(body.decode("utf-8"))
                nid = payload.get("id")
                if nid and nid in NODES:
                    NODES[nid]["data"] = payload.get("data", {})
                    NODES[nid]["last_seen"] = time.time()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status": "ok"}')
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(f'{{"error": "{str(e)}"}}\n'.encode("utf-8"))
            return

        # Dashboard Authentication Login
        if path == "/api/login" or path == "/login":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                payload = json.loads(body.decode("utf-8"))
                pwd = payload.get("password", "")

                if pwd == ADMIN_PASSWORD:
                    import secrets
                    session_id = secrets.token_hex(24)
                    ACTIVE_SESSIONS.add(session_id)

                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Set-Cookie", f"probe_session={session_id}; Path=/; HttpOnly; SameSite=Lax")
                    self.end_headers()
                    self.wfile.write(b'{"success": true}')
                else:
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success": false, "error": "Invalid password"}')
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(f'{{"error": "{str(e)}"}}\n'.encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

def main():
    print(f"🚀 Fleet Status Monitor Master Server listening on 0.0.0.0:{PORT}...")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), TelemetryHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
        server.server_close()

if __name__ == "__main__":
    main()
