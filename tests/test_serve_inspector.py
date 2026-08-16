from __future__ import annotations

import contextlib
import base64
import hashlib
import json
import shutil
import ssl
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib import error, request

from scripts.homekit_inspector_cli import (
    PublishConfig,
    RefreshError,
    publish_report,
    request_signature as client_request_signature,
)
from scripts.serve_inspector import (
    DEFAULT_INDEX,
    InspectorHandler,
    InspectorServer,
    build_auth_header,
    load_publish_secret,
    safe_resolve,
    validate_transport,
)


PUBLISH_SECRET_HEX = "ab" * 32
PUBLISH_SECRET = bytes.fromhex(PUBLISH_SECRET_HEX)
VALID_REPORT = b"<!doctype html><title>HomeKit Inspector</title><script>const data = {};</script>"
ROOT = Path(__file__).resolve().parents[1]


class SilentInspectorHandler(InspectorHandler):
    def do_POST(self):  # noqa: N802 - inherited API
        if self.server.capture is not None:
            self.server.capture["authorization"] = self.headers.get("Authorization")
        super().do_POST()

    def log_message(self, format, *args):  # noqa: A002 - inherited API
        pass


@contextlib.contextmanager
def running_server(
    root: Path,
    secret: bytes = PUBLISH_SECRET,
    max_bytes: int = 1024 * 1024,
    tls_cert: Path | None = None,
    tls_key: Path | None = None,
    capture: dict | None = None,
):
    server = InspectorServer(("127.0.0.1", 0), SilentInspectorHandler)
    server.root = root
    server.index_name = DEFAULT_INDEX
    server.auth_header = ""
    server.publish_secret = secret
    server.max_upload_bytes = max_bytes
    server.upload_lock = threading.Lock()
    server.nonce_lock = threading.Lock()
    server.used_nonces = {}
    server.capture = capture
    server.tls_enabled = tls_cert is not None
    if tls_cert is not None and tls_key is not None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(tls_cert, tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        scheme = "https" if server.tls_enabled else "http"
        host = "localhost" if server.tls_enabled else "127.0.0.1"
        yield f"{scheme}://{host}:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def signed_upload(
    base_url: str,
    content: bytes,
    secret: bytes = PUBLISH_SECRET,
    digest: str | None = None,
    timestamp: str | None = None,
    nonce: str = "12" * 16,
) -> request.Request:
    report_digest = digest or hashlib.sha256(content).hexdigest()
    request_timestamp = timestamp or str(int(time.time()))
    signature = client_request_signature(
        secret, request_timestamp, nonce, report_digest, len(content)
    )
    return request.Request(
        f"{base_url}/api/v1/report",
        data=content,
        method="POST",
        headers={
            "Content-Type": "text/html",
            "X-Inspector-Protocol": "homekit-inspector-v1",
            "X-Inspector-Timestamp": request_timestamp,
            "X-Inspector-Nonce": nonce,
            "X-Report-SHA256": report_digest,
            "X-Inspector-Signature": signature,
        },
    )


class ServeInspectorTests(unittest.TestCase):
    def test_viewer_credentials_are_loaded_from_private_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            username = root / "view-username"
            password = root / "view-password"
            username.write_text("inspector\n", encoding="utf-8")
            password.write_text("cd" * 32 + "\n", encoding="ascii")
            username.chmod(0o600)
            password.chmod(0o600)
            with patch.dict(
                "os.environ",
                {
                    "HOMEKIT_INSPECTOR_VIEW_USERNAME_FILE": str(username),
                    "HOMEKIT_INSPECTOR_VIEW_PASSWORD_FILE": str(password),
                },
                clear=False,
            ):
                header = build_auth_header()
            decoded = base64.b64decode(header.removeprefix("Basic ")).decode("utf-8")
            self.assertEqual(decoded, "inspector:" + "cd" * 32)

    def test_plain_http_is_limited_to_loopback(self):
        validate_transport(
            "127.0.0.1",
            tls_enabled=False,
            view_auth_enabled=False,
            allow_unauthenticated_view=False,
        )
        validate_transport(
            "0.0.0.0",
            tls_enabled=True,
            view_auth_enabled=True,
            allow_unauthenticated_view=False,
        )
        with self.assertRaisesRegex(SystemExit, "requires TLS"):
            validate_transport(
                "0.0.0.0",
                tls_enabled=False,
                view_auth_enabled=True,
                allow_unauthenticated_view=False,
            )
        with self.assertRaisesRegex(SystemExit, "viewer authentication"):
            validate_transport(
                "0.0.0.0",
                tls_enabled=True,
                view_auth_enabled=False,
                allow_unauthenticated_view=False,
            )

    def test_load_publish_secret_requires_owner_only_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret_file = Path(tmp) / "publish-secret"
            secret_file.write_text(PUBLISH_SECRET_HEX, encoding="ascii")
            secret_file.chmod(0o644)
            with patch.dict(
                "os.environ",
                {"HOMEKIT_INSPECTOR_PUBLISH_SECRET_FILE": str(secret_file)},
                clear=False,
            ):
                with self.assertRaisesRegex(SystemExit, "accessible by other users"):
                    load_publish_secret()
            secret_file.chmod(0o600)
            with patch.dict(
                "os.environ",
                {"HOMEKIT_INSPECTOR_PUBLISH_SECRET_FILE": str(secret_file)},
                clear=False,
            ):
                self.assertEqual(load_publish_secret(), PUBLISH_SECRET)

    def test_root_path_resolves_to_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self.assertEqual(safe_resolve(root, "/"), root / DEFAULT_INDEX)

    def test_file_path_stays_inside_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self.assertEqual(safe_resolve(root, "/report.html"), root / "report.html")

    def test_parent_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self.assertIsNone(safe_resolve(root, "/../secret.txt"))

    def test_server_publisher_replaces_report_and_verifies_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            report = root / "generated.html"
            report.write_bytes(VALID_REPORT)
            secret_file = root / "publish-secret"
            secret_file.write_text(PUBLISH_SECRET_HEX + "\n", encoding="ascii")
            secret_file.chmod(0o600)
            capture = {}
            with running_server(root, capture=capture) as base_url:
                digest = publish_report(
                    PublishConfig(
                        kind="server",
                        url=f"{base_url}/api/v1/report",
                        secret_file=secret_file,
                    ),
                    report,
                )
                with request.urlopen(f"{base_url}/api/v1/status") as response:
                    status = json.load(response)
                with request.urlopen(f"{base_url}/") as response:
                    self.assertIn(
                        "connect-src 'none'",
                        response.headers["Content-Security-Policy"],
                    )
                    self.assertEqual(
                        response.headers["Cross-Origin-Resource-Policy"],
                        "same-origin",
                    )
            self.assertEqual(digest, hashlib.sha256(VALID_REPORT).hexdigest())
            self.assertEqual((root / DEFAULT_INDEX).read_bytes(), VALID_REPORT)
            self.assertEqual(
                stat.S_IMODE((root / DEFAULT_INDEX).stat().st_mode), 0o640
            )
            self.assertEqual(status["sha256"], digest)
            self.assertIsNone(capture["authorization"])

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL is required")
    def test_server_publisher_uses_verified_tls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            credentials = root / "credentials"
            subprocess.run(
                [
                    str(ROOT / "scripts/create-server-credentials.sh"),
                    "--hostname",
                    "localhost",
                    "--output-dir",
                    str(credentials),
                    "--days",
                    "1",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            report = root / "generated.html"
            report.write_bytes(VALID_REPORT)
            secret_file = credentials / "publish-secret"
            secret = bytes.fromhex(secret_file.read_text(encoding="ascii").strip())
            with running_server(
                root,
                secret=secret,
                tls_cert=credentials / "server-cert.pem",
                tls_key=credentials / "server-key.pem",
            ) as base_url:
                with self.assertRaisesRegex(RefreshError, "Server publication failed"):
                    publish_report(
                        PublishConfig(
                            kind="server",
                            url=f"{base_url}/api/v1/report",
                            secret_file=secret_file,
                        ),
                        report,
                    )
                digest = publish_report(
                    PublishConfig(
                        kind="server",
                        url=f"{base_url}/api/v1/report",
                        secret_file=secret_file,
                        ca_file=credentials / "ca.pem",
                    ),
                    report,
                )
            self.assertEqual(digest, hashlib.sha256(VALID_REPORT).hexdigest())
            self.assertEqual((root / DEFAULT_INDEX).read_bytes(), VALID_REPORT)

    def test_upload_endpoint_is_disabled_without_server_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with running_server(root, secret=b"") as base_url:
                upload = signed_upload(base_url, VALID_REPORT)
                with self.assertRaises(error.HTTPError) as raised:
                    request.urlopen(upload)
            self.assertEqual(raised.exception.code, 503)
            self.assertFalse((root / DEFAULT_INDEX).exists())

    def test_upload_rejects_wrong_signature_without_replacing_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            destination = root / DEFAULT_INDEX
            destination.write_bytes(b"previous report")
            with running_server(root) as base_url:
                upload = signed_upload(
                    base_url, VALID_REPORT, secret=bytes.fromhex("cd" * 32)
                )
                with self.assertRaises(error.HTTPError) as raised:
                    request.urlopen(upload)
            self.assertEqual(raised.exception.code, 401)
            self.assertEqual(destination.read_bytes(), b"previous report")

    def test_upload_rejects_hash_mismatch_without_replacing_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            destination = root / DEFAULT_INDEX
            destination.write_bytes(b"previous report")
            with running_server(root) as base_url:
                upload = signed_upload(base_url, VALID_REPORT, digest="0" * 64)
                with self.assertRaises(error.HTTPError) as raised:
                    request.urlopen(upload)
            self.assertEqual(raised.exception.code, 422)
            self.assertEqual(destination.read_bytes(), b"previous report")

    def test_upload_rejects_non_inspector_html_without_replacing_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            destination = root / DEFAULT_INDEX
            destination.write_bytes(b"previous report")
            invalid_report = b"<!doctype html><title>Unrelated page</title>"
            with running_server(root) as base_url:
                upload = signed_upload(base_url, invalid_report)
                with self.assertRaises(error.HTTPError) as raised:
                    request.urlopen(upload)
            self.assertEqual(raised.exception.code, 422)
            self.assertEqual(destination.read_bytes(), b"previous report")

    def test_upload_rejects_oversized_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with running_server(root, max_bytes=16) as base_url:
                upload = signed_upload(base_url, VALID_REPORT)
                with self.assertRaises(error.HTTPError) as raised:
                    request.urlopen(upload)
            self.assertEqual(raised.exception.code, 413)
            self.assertFalse((root / DEFAULT_INDEX).exists())

    def test_replayed_signed_request_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with running_server(root) as base_url:
                upload = signed_upload(base_url, VALID_REPORT)
                with request.urlopen(upload) as response:
                    self.assertEqual(response.status, 201)
                with self.assertRaises(error.HTTPError) as raised:
                    request.urlopen(upload)
            self.assertEqual(raised.exception.code, 409)

    def test_stale_signed_request_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            stale = str(int(time.time()) - 301)
            with running_server(root) as base_url:
                upload = signed_upload(base_url, VALID_REPORT, timestamp=stale)
                with self.assertRaises(error.HTTPError) as raised:
                    request.urlopen(upload)
            self.assertEqual(raised.exception.code, 401)
            self.assertFalse((root / DEFAULT_INDEX).exists())


if __name__ == "__main__":
    unittest.main()
