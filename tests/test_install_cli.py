import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.install_refresh_config import install_config


ROOT = Path(__file__).resolve().parents[1]


class InstallCliTests(unittest.TestCase):
    def create_source_config(self, home: Path) -> Path:
        source = home / "source"
        source.mkdir(parents=True)
        database = home / "Library/HomeKit/core.sqlite"
        database.parent.mkdir(parents=True)
        database.write_bytes(b"synthetic sqlite placeholder")
        for filename in ("themes.json", "overrides.json", "homebridge.json"):
            (source / filename).write_text("{}\n", encoding="utf-8")
        config = {
            "version": 1,
            "database": str(database),
            "workingDirectory": "old-output",
            "inputs": {
                "themeConfig": "themes.json",
                "privateOverrides": "overrides.json",
                "homebridgeConfig": "homebridge.json",
            },
            "publish": {
                "type": "local",
                "path": str(home / "served/homekit_inspector.html"),
            },
        }
        path = source / "refresh.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def test_migrates_config_and_private_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            source = self.create_source_config(home)
            destination = home / "Library/Application Support/HomeKit Inspector/config.json"
            with patch.dict(os.environ, {"HOME": str(home)}):
                result = install_config(source, destination)
            self.assertEqual(result, "installed")
            installed = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(installed["workingDirectory"], "output")
            self.assertEqual(installed["database"], "~/Library/HomeKit/core.sqlite")
            self.assertEqual(installed["inputs"]["themeConfig"], "config/theme-config.json")
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            for filename in ("theme-config.json", "private-overrides.json", "homebridge-config.json"):
                copied = destination.parent / "config" / filename
                self.assertTrue(copied.is_file())
                self.assertEqual(stat.S_IMODE(copied.stat().st_mode), 0o600)

    def test_preserves_existing_config_without_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            source = self.create_source_config(home)
            destination = home / "installed/config.json"
            destination.parent.mkdir(parents=True)
            destination.write_text('{"existing": true}\n', encoding="utf-8")
            result = install_config(source, destination)
            self.assertEqual(result, "preserved")
            self.assertTrue(json.loads(destination.read_text(encoding="utf-8"))["existing"])

    def test_migrates_http_publication_token_to_private_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            source_dir = home / "source"
            source_dir.mkdir()
            token = source_dir / "upload-token"
            token.write_text(
                "synthetic-upload-token-with-at-least-32-characters\n",
                encoding="utf-8",
            )
            source = source_dir / "refresh.json"
            source.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "publish": {
                            "type": "http",
                            "url": "https://inspector.example/api/v1/report",
                            "tokenFile": "upload-token",
                        },
                    }
                ),
                encoding="utf-8",
            )
            destination = home / "installed/config.json"
            result = install_config(source, destination)
            installed = json.loads(destination.read_text(encoding="utf-8"))
            installed_token = destination.parent / installed["publish"]["tokenFile"]
            self.assertEqual(result, "installed")
            self.assertEqual(installed["publish"]["type"], "http")
            self.assertEqual(
                installed["publish"]["url"],
                "https://inspector.example/api/v1/report",
            )
            self.assertNotIn("synthetic-upload-token", destination.read_text(encoding="utf-8"))
            self.assertEqual(
                installed_token.read_text(encoding="utf-8"),
                token.read_text(encoding="utf-8"),
            )
            self.assertEqual(stat.S_IMODE(installed_token.stat().st_mode), 0o600)

    def test_installer_and_uninstaller_in_isolated_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            source = self.create_source_config(home)
            app_home = home / "Library/Application Support/HomeKit Inspector"
            bin_dir = home / ".local/bin"
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            environment["HOMEKIT_INSPECTOR_PYTHON"] = sys.executable
            subprocess.run(
                [
                    str(ROOT / "install-cli.sh"),
                    "--app-home",
                    str(app_home),
                    "--bin-dir",
                    str(bin_dir),
                    "--config",
                    str(source),
                ],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
            command = bin_dir / "homekit-inspector"
            self.assertTrue(command.is_symlink())
            status = subprocess.run(
                [str(command), "status"],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(json.loads(status.stdout)["state"], "never")
            subprocess.run(
                [
                    str(ROOT / "uninstall-cli.sh"),
                    "--app-home",
                    str(app_home),
                    "--bin-dir",
                    str(bin_dir),
                ],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertFalse(command.exists())
            self.assertFalse((app_home / "app").exists())
            self.assertTrue((app_home / "config.json").is_file())

    def test_installer_rejects_home_directory_as_application_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            marker = home / "preserve-me"
            marker.write_text("safe", encoding="utf-8")
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            environment["HOMEKIT_INSPECTOR_PYTHON"] = sys.executable
            completed = subprocess.run(
                [str(ROOT / "install-cli.sh"), "--app-home", str(home)],
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn("Refusing unsafe application directory", completed.stderr)
            self.assertEqual(marker.read_text(encoding="utf-8"), "safe")


if __name__ == "__main__":
    unittest.main()
