#!/usr/bin/env python3
"""Serve and optionally receive a HomeKit Inspector report on a local network.

The server never accesses HomeKit. Authenticated publication can atomically
replace the generated HTML file; all other served content remains read-only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import tempfile
import threading
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8099
DEFAULT_INDEX = "homekit_inspector.html"
DEFAULT_MAX_UPLOAD_BYTES = 20 * 1024 * 1024
REPORT_ENDPOINT = "/api/v1/report"
STATUS_ENDPOINT = "/api/v1/status"


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

    @property
    def upload_token(self) -> str:
        return self.server.upload_token

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
        if urlparse(self.path).path == STATUS_ENDPOINT:
            if not self.check_auth():
                return
            self.write_json(report_status(self.root / self.index_name))
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

    def do_POST(self) -> None:
        if urlparse(self.path).path != REPORT_ENDPOINT:
            self.write_json({"ok": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        if not self.upload_token:
            self.write_json(
                {"ok": False, "error": "Report upload is not configured"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if not self.check_upload_auth():
            return
        if self.headers.get("Transfer-Encoding"):
            self.write_json(
                {"ok": False, "error": "Transfer-Encoding is not supported"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            self.write_json(
                {"ok": False, "error": "Content-Length is required"},
                HTTPStatus.LENGTH_REQUIRED,
            )
            return
        try:
            content_length = int(length_header)
        except ValueError:
            content_length = -1
        if content_length <= 0:
            self.write_json(
                {"ok": False, "error": "Content-Length must be positive"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        if content_length > self.server.max_upload_bytes:
            self.write_json(
                {"ok": False, "error": "Report exceeds the upload size limit"},
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "text/html":
            self.write_json(
                {"ok": False, "error": "Content-Type must be text/html"},
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            )
            return
        expected_hash = self.headers.get("X-Report-SHA256", "").lower()
        if len(expected_hash) != 64 or any(
            character not in "0123456789abcdef" for character in expected_hash
        ):
            self.write_json(
                {"ok": False, "error": "X-Report-SHA256 must be a SHA-256 digest"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        if not self.server.upload_lock.acquire(blocking=False):
            self.write_json(
                {"ok": False, "error": "Another report upload is in progress"},
                HTTPStatus.CONFLICT,
            )
            return
        try:
            content = self.rfile.read(content_length)
            if len(content) != content_length:
                self.write_json(
                    {"ok": False, "error": "Incomplete request body"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            actual_hash = hashlib.sha256(content).hexdigest()
            if not hmac.compare_digest(actual_hash, expected_hash):
                self.write_json(
                    {"ok": False, "error": "Report hash does not match request body"},
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                )
                return
            if not valid_inspector_report(content):
                self.write_json(
                    {"ok": False, "error": "Report is not a valid HomeKit Inspector HTML file"},
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                )
                return
            atomic_replace(self.root / self.index_name, content)
            self.write_json(
                {"ok": True, "sha256": actual_hash, "bytes": len(content)},
                HTTPStatus.CREATED,
            )
        except OSError:
            self.write_json(
                {"ok": False, "error": "Unable to store report"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        finally:
            self.server.upload_lock.release()

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

    def check_upload_auth(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.upload_token}"
        if hmac.compare_digest(supplied, expected):
            return True
        self.write_json(
            {"ok": False, "error": "Unauthorized"}, HTTPStatus.UNAUTHORIZED
        )
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

    def write_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        content = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        self.send_response(status)
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
    upload_token: str
    max_upload_bytes: int
    upload_lock: threading.Lock


def valid_inspector_report(content: bytes) -> bool:
    try:
        html = content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return "HomeKit Inspector" in html and "const data =" in html


def atomic_replace(destination: Path, content: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def report_status(path: Path) -> dict:
    if not path.is_file():
        return {"ok": True, "indexExists": False}
    try:
        stat = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return {"ok": False, "indexExists": True}
    return {
        "ok": True,
        "indexExists": True,
        "sha256": digest,
        "bytes": stat.st_size,
        "modifiedAt": int(stat.st_mtime),
    }


def build_auth_header() -> str:
    username = os.getenv("HOMEKIT_INSPECTOR_USERNAME", "")
    password = os.getenv("HOMEKIT_INSPECTOR_PASSWORD", "")
    if not username and not password:
        return ""
    if not username or not password:
        raise SystemExit("Set both HOMEKIT_INSPECTOR_USERNAME and HOMEKIT_INSPECTOR_PASSWORD, or neither")
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def load_upload_token() -> str:
    raw_path = os.getenv("HOMEKIT_INSPECTOR_UPLOAD_TOKEN_FILE", "")
    if not raw_path:
        return ""
    path = Path(raw_path).expanduser().resolve()
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"Unable to read HOMEKIT_INSPECTOR_UPLOAD_TOKEN_FILE: {exc}") from exc
    if len(token) < 32:
        raise SystemExit("HOMEKIT_INSPECTOR_UPLOAD_TOKEN_FILE must contain at least 32 characters")
    if any(character.isspace() for character in token):
        raise SystemExit("HOMEKIT_INSPECTOR_UPLOAD_TOKEN_FILE must not contain whitespace")
    if path.stat().st_mode & 0o077:
        raise SystemExit("HOMEKIT_INSPECTOR_UPLOAD_TOKEN_FILE must use owner-only permissions")
    return token


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
    server.upload_token = load_upload_token()
    server.max_upload_bytes = env_int(
        "HOMEKIT_INSPECTOR_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES
    )
    if server.max_upload_bytes <= 0:
        raise SystemExit("HOMEKIT_INSPECTOR_MAX_UPLOAD_BYTES must be positive")
    server.upload_lock = threading.Lock()
    print(f"Serving HomeKit Inspector from {root} on http://{host}:{port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
