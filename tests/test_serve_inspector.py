import contextlib
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib import error, request

from scripts.homekit_inspector_cli import PublishConfig, publish_report
from scripts.serve_inspector import (
    DEFAULT_INDEX,
    InspectorHandler,
    InspectorServer,
    load_upload_token,
    safe_resolve,
)


UPLOAD_TOKEN = "synthetic-upload-token-with-at-least-32-characters"
VALID_REPORT = b"<!doctype html><title>HomeKit Inspector</title><script>const data = {};</script>"


class SilentInspectorHandler(InspectorHandler):
    def log_message(self, format, *args):  # noqa: A002 - inherited API
        pass


@contextlib.contextmanager
def running_server(root: Path, token: str = UPLOAD_TOKEN, max_bytes: int = 1024 * 1024):
    server = InspectorServer(("127.0.0.1", 0), SilentInspectorHandler)
    server.root = root
    server.index_name = DEFAULT_INDEX
    server.auth_header = ""
    server.upload_token = token
    server.max_upload_bytes = max_bytes
    server.upload_lock = threading.Lock()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class ServeInspectorTests(unittest.TestCase):
    def test_load_upload_token_requires_owner_only_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "upload-token"
            token_file.write_text(UPLOAD_TOKEN, encoding="utf-8")
            token_file.chmod(0o644)
            with patch.dict(
                "os.environ",
                {"HOMEKIT_INSPECTOR_UPLOAD_TOKEN_FILE": str(token_file)},
                clear=False,
            ):
                with self.assertRaisesRegex(SystemExit, "owner-only permissions"):
                    load_upload_token()
            token_file.chmod(0o600)
            with patch.dict(
                "os.environ",
                {"HOMEKIT_INSPECTOR_UPLOAD_TOKEN_FILE": str(token_file)},
                clear=False,
            ):
                self.assertEqual(load_upload_token(), UPLOAD_TOKEN)

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

    def test_http_publisher_replaces_report_and_verifies_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            report = root / "generated.html"
            report.write_bytes(VALID_REPORT)
            token_file = root / "upload-token"
            token_file.write_text(UPLOAD_TOKEN + "\n", encoding="utf-8")
            with running_server(root) as base_url:
                digest = publish_report(
                    PublishConfig(
                        kind="http",
                        url=f"{base_url}/api/v1/report",
                        token_file=token_file,
                    ),
                    report,
                )
                with request.urlopen(f"{base_url}/api/v1/status") as response:
                    status = json.load(response)
            self.assertEqual(digest, hashlib.sha256(VALID_REPORT).hexdigest())
            self.assertEqual((root / DEFAULT_INDEX).read_bytes(), VALID_REPORT)
            self.assertEqual(status["sha256"], digest)

    def test_upload_endpoint_is_disabled_without_server_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with running_server(root, token="") as base_url:
                upload = request.Request(
                    f"{base_url}/api/v1/report",
                    data=VALID_REPORT,
                    method="POST",
                    headers={
                        "Authorization": f"Bearer {UPLOAD_TOKEN}",
                        "Content-Type": "text/html",
                        "X-Report-SHA256": hashlib.sha256(VALID_REPORT).hexdigest(),
                    },
                )
                with self.assertRaises(error.HTTPError) as raised:
                    request.urlopen(upload)
            self.assertEqual(raised.exception.code, 503)
            self.assertFalse((root / DEFAULT_INDEX).exists())

    def test_upload_rejects_wrong_token_without_replacing_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            destination = root / DEFAULT_INDEX
            destination.write_bytes(b"previous report")
            with running_server(root) as base_url:
                upload = request.Request(
                    f"{base_url}/api/v1/report",
                    data=VALID_REPORT,
                    method="POST",
                    headers={
                        "Authorization": "Bearer incorrect-token",
                        "Content-Type": "text/html",
                        "X-Report-SHA256": hashlib.sha256(VALID_REPORT).hexdigest(),
                    },
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
                upload = request.Request(
                    f"{base_url}/api/v1/report",
                    data=VALID_REPORT,
                    method="POST",
                    headers={
                        "Authorization": f"Bearer {UPLOAD_TOKEN}",
                        "Content-Type": "text/html",
                        "X-Report-SHA256": "0" * 64,
                    },
                )
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
                upload = request.Request(
                    f"{base_url}/api/v1/report",
                    data=invalid_report,
                    method="POST",
                    headers={
                        "Authorization": f"Bearer {UPLOAD_TOKEN}",
                        "Content-Type": "text/html",
                        "X-Report-SHA256": hashlib.sha256(invalid_report).hexdigest(),
                    },
                )
                with self.assertRaises(error.HTTPError) as raised:
                    request.urlopen(upload)
            self.assertEqual(raised.exception.code, 422)
            self.assertEqual(destination.read_bytes(), b"previous report")

    def test_upload_rejects_oversized_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with running_server(root, max_bytes=16) as base_url:
                upload = request.Request(
                    f"{base_url}/api/v1/report",
                    data=VALID_REPORT,
                    method="POST",
                    headers={
                        "Authorization": f"Bearer {UPLOAD_TOKEN}",
                        "Content-Type": "text/html",
                        "X-Report-SHA256": hashlib.sha256(VALID_REPORT).hexdigest(),
                    },
                )
                with self.assertRaises(error.HTTPError) as raised:
                    request.urlopen(upload)
            self.assertEqual(raised.exception.code, 413)
            self.assertFalse((root / DEFAULT_INDEX).exists())


if __name__ == "__main__":
    unittest.main()
