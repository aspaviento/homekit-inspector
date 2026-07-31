#!/usr/bin/env python3
"""Serve a generated HomeKit Inspector report on a local network.

This server is intentionally static and read-only. It is meant for private LAN
use after `homekit_inspector.html` has been generated on a Mac and copied to the
served directory.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8099
DEFAULT_INDEX = "homekit_inspector.html"


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc


def safe_resolve(root: Path, request_path: str) -> Path | None:
    parsed_path = unquote(urlparse(request_path).path)
    relative = parsed_path.lstrip("/") or DEFAULT_INDEX
    candidate = (root / relative).resolve()
    if root == candidate or root in candidate.parents:
        return candidate
    return None


class InspectorHandler(SimpleHTTPRequestHandler):
    server_version = "HomeKitInspectorHTTP/1.0"

    @property
    def root(self) -> Path:
        return self.server.root

    @property
    def index_name(self) -> str:
        return self.server.index_name

    @property
    def auth_header(self) -> str:
        return self.server.auth_header

    def do_GET(self) -> None:
        if self.path == "/health":
            self.write_json(
                {
                    "ok": True,
                    "index": self.index_name,
                    "root": str(self.root),
                    "indexExists": (self.root / self.index_name).is_file(),
                }
            )
            return
        if not self.check_auth():
            return
        target = safe_resolve(self.root, self.path)
        if not target:
            self.send_error(HTTPStatus.FORBIDDEN, "Forbidden")
            return
        if target.is_dir():
            target = target / self.index_name
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        self.serve_file(target)

    def do_HEAD(self) -> None:
        if self.path == "/health":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            return
        if not self.check_auth():
            return
        target = safe_resolve(self.root, self.path)
        if target and target.is_dir():
            target = target / self.index_name
        if not target or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        self.send_file_headers(target)

    def list_directory(self, path):  # noqa: N802 - inherited API
        self.send_error(HTTPStatus.FORBIDDEN, "Directory listing disabled")
        return None

    def check_auth(self) -> bool:
        if not self.auth_header:
            return True
        if self.headers.get("Authorization") == self.auth_header:
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="HomeKit Inspector"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def serve_file(self, path: Path) -> None:
        try:
            content = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        self.send_file_headers(path, len(content))
        if self.command != "HEAD":
            self.wfile.write(content)

    def send_file_headers(self, path: Path, content_length: int | None = None) -> None:
        if content_length is None:
            content_length = path.stat().st_size
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

    def write_json(self, payload: dict) -> None:
        content = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)


class InspectorServer(ThreadingHTTPServer):
    root: Path
    index_name: str
    auth_header: str


def build_auth_header() -> str:
    username = os.getenv("HOMEKIT_INSPECTOR_USERNAME", "")
    password = os.getenv("HOMEKIT_INSPECTOR_PASSWORD", "")
    if not username and not password:
        return ""
    if not username or not password:
        raise SystemExit("Set both HOMEKIT_INSPECTOR_USERNAME and HOMEKIT_INSPECTOR_PASSWORD, or neither")
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def main() -> None:
    root = Path(os.getenv("HOMEKIT_INSPECTOR_ROOT", ".")).expanduser().resolve()
    index_name = os.getenv("HOMEKIT_INSPECTOR_INDEX", DEFAULT_INDEX)
    host = os.getenv("HOMEKIT_INSPECTOR_HOST", DEFAULT_HOST)
    port = env_int("HOMEKIT_INSPECTOR_PORT", DEFAULT_PORT)
    if not root.is_dir():
        raise SystemExit(f"HOMEKIT_INSPECTOR_ROOT is not a directory: {root}")
    server = InspectorServer((host, port), InspectorHandler)
    server.root = root
    server.index_name = index_name
    server.auth_header = build_auth_header()
    print(f"Serving HomeKit Inspector from {root} on http://{host}:{port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
