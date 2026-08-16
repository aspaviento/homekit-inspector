#!/usr/bin/env python3
"""Refresh and publish a HomeKit Inspector report from one configuration file."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = Path.home() / "Library/Application Support/HomeKit Inspector/config.json"
STATUS_FILENAME = "refresh_status.json"
LOCK_FILENAME = ".refresh.lock"
REPORT_FILENAME = "homekit_inspector.html"
DATA_FILENAME = "homekit_inspector_data.json"
EXPORT_FILENAME = "homekit_homed_export.json"
CONFIG_VERSION = 1
SAFE_SSH_HOST = re.compile(r"^[A-Za-z0-9_.@-]+$")
SAFE_REMOTE_PATH = re.compile(r"^/[A-Za-z0-9._/-]+$")


class ConfigError(ValueError):
    """Raised when the refresh configuration is invalid."""


class RefreshError(RuntimeError):
    """Raised when extraction, generation, publication, or verification fails."""


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class InputConfig:
    theme_config: Path | None = None
    private_overrides: Path | None = None
    homebridge_config: Path | None = None


@dataclass(frozen=True)
class PublishConfig:
    kind: str
    path: Path | str | None = None
    host: str | None = None
    url: str | None = None
    token_file: Path | None = None
    request_timeout_seconds: float = 30.0
    health_url: str | None = None
    health_timeout_seconds: float = 5.0


@dataclass(frozen=True)
class RefreshConfig:
    database: Path
    working_directory: Path
    inputs: InputConfig
    publish: PublishConfig
    verbose: bool = False


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_path(value: str, base: Path) -> Path:
    expanded = Path(os.path.expandvars(os.path.expanduser(value)))
    if not expanded.is_absolute():
        expanded = base / expanded
    return expanded.resolve()


def require_mapping(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be a JSON object")
    return value


def reject_unknown_keys(value: dict, allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError(f"Unknown {label} keys: {', '.join(unknown)}")


def optional_path(value, label: str, base: Path) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a non-empty path string")
    return resolve_path(value, base)


def validated_http_url(value, label: str) -> str:
    parsed = urlparse(value) if isinstance(value, str) else None
    if (
        parsed is None
        or parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ConfigError(
            f"{label} must be an http or https URL without credentials or fragments"
        )
    return value


def load_config(path: Path) -> RefreshConfig:
    config_path = path.expanduser().resolve()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {config_path}: {exc}") from exc

    raw = require_mapping(raw, "configuration")
    reject_unknown_keys(
        raw,
        {"version", "database", "workingDirectory", "inputs", "publish", "verbose"},
        "configuration",
    )
    if type(raw.get("version")) is not int or raw["version"] != CONFIG_VERSION:
        raise ConfigError(f"version must be {CONFIG_VERSION}")

    base = config_path.parent
    database_raw = raw.get("database", "~/Library/HomeKit/core.sqlite")
    working_raw = raw.get(
        "workingDirectory", "~/Library/Application Support/HomeKit Inspector/output"
    )
    if not isinstance(database_raw, str) or not database_raw.strip():
        raise ConfigError("database must be a non-empty path string")
    if not isinstance(working_raw, str) or not working_raw.strip():
        raise ConfigError("workingDirectory must be a non-empty path string")

    inputs_raw = require_mapping(raw.get("inputs", {}), "inputs")
    reject_unknown_keys(
        inputs_raw, {"themeConfig", "privateOverrides", "homebridgeConfig"}, "inputs"
    )
    inputs = InputConfig(
        theme_config=optional_path(inputs_raw.get("themeConfig"), "inputs.themeConfig", base),
        private_overrides=optional_path(
            inputs_raw.get("privateOverrides"), "inputs.privateOverrides", base
        ),
        homebridge_config=optional_path(
            inputs_raw.get("homebridgeConfig"), "inputs.homebridgeConfig", base
        ),
    )

    publish_raw = require_mapping(raw.get("publish"), "publish")
    reject_unknown_keys(
        publish_raw,
        {
            "type",
            "host",
            "path",
            "url",
            "tokenFile",
            "requestTimeoutSeconds",
            "healthUrl",
            "healthTimeoutSeconds",
        },
        "publish",
    )
    kind = publish_raw.get("type")
    if kind not in {"local", "ssh", "http"}:
        raise ConfigError("publish.type must be 'local', 'ssh', or 'http'")
    publish_path = publish_raw.get("path")
    health_url = publish_raw.get("healthUrl")
    if health_url is not None:
        health_url = validated_http_url(health_url, "publish.healthUrl")
    timeout = publish_raw.get("healthTimeoutSeconds", 5)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 0 < timeout <= 60
    ):
        raise ConfigError("publish.healthTimeoutSeconds must be between 0 and 60")
    request_timeout = publish_raw.get("requestTimeoutSeconds", 30)
    if (
        isinstance(request_timeout, bool)
        or not isinstance(request_timeout, (int, float))
        or not 0 < request_timeout <= 300
    ):
        raise ConfigError("publish.requestTimeoutSeconds must be between 0 and 300")

    host = publish_raw.get("host")
    publish_url = publish_raw.get("url")
    token_file_raw = publish_raw.get("tokenFile")
    resolved_publish_path: Path | str | None = None
    token_file: Path | None = None
    if kind == "local":
        if host is not None or publish_url is not None or token_file_raw is not None:
            raise ConfigError("local publication does not support host, url, or tokenFile")
        if not isinstance(publish_path, str) or not publish_path.strip():
            raise ConfigError("publish.path must be a non-empty path string")
        resolved_publish_path = resolve_path(publish_path, base)
    elif kind == "ssh":
        if publish_url is not None or token_file_raw is not None:
            raise ConfigError("SSH publication does not support publish.url or tokenFile")
        if not isinstance(publish_path, str) or not publish_path.strip():
            raise ConfigError("publish.path must be a non-empty path string")
        if (
            not isinstance(host, str)
            or host.startswith("-")
            or not SAFE_SSH_HOST.fullmatch(host)
        ):
            raise ConfigError("publish.host contains unsupported characters")
        if not SAFE_REMOTE_PATH.fullmatch(publish_path):
            raise ConfigError(
                "SSH publish.path must be absolute and contain only letters, numbers, '.', '_', '-', and '/'"
            )
        resolved_publish_path = publish_path
    else:
        if host is not None or publish_path is not None:
            raise ConfigError("HTTP publication does not support host or path")
        publish_url = validated_http_url(publish_url, "publish.url")
        token_file = optional_path(token_file_raw, "publish.tokenFile", base)
        if token_file is None:
            raise ConfigError("publish.tokenFile is required for HTTP publication")

    verbose = raw.get("verbose", False)
    if not isinstance(verbose, bool):
        raise ConfigError("verbose must be true or false")

    return RefreshConfig(
        database=resolve_path(database_raw, base),
        working_directory=resolve_path(working_raw, base),
        inputs=inputs,
        publish=PublishConfig(
            kind=kind,
            path=resolved_publish_path,
            host=host,
            url=publish_url,
            token_file=token_file,
            request_timeout_seconds=float(request_timeout),
            health_url=health_url,
            health_timeout_seconds=float(timeout),
        ),
        verbose=verbose,
    )


def validate_config(config: RefreshConfig, require_database: bool = True) -> None:
    if require_database and not config.database.is_file():
        raise ConfigError(f"HomeKit database is not readable: {config.database}")
    for label, path in (
        ("themeConfig", config.inputs.theme_config),
        ("privateOverrides", config.inputs.private_overrides),
        ("homebridgeConfig", config.inputs.homebridge_config),
    ):
        if path is not None and not path.is_file():
            raise ConfigError(f"Configured {label} is not readable: {path}")
    if config.publish.kind == "ssh":
        for executable in ("rsync", "ssh"):
            if shutil.which(executable) is None:
                raise ConfigError(f"Required executable not found: {executable}")
    if config.publish.kind == "http":
        if config.publish.token_file is None or not config.publish.token_file.is_file():
            raise ConfigError(
                f"HTTP publication token is not readable: {config.publish.token_file}"
            )
        read_publish_token(config.publish.token_file)
        if config.publish.token_file.stat().st_mode & 0o077:
            raise ConfigError("HTTP publication token must use owner-only permissions")


def read_publish_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"HTTP publication token is not readable: {path}") from exc
    if len(token) < 32:
        raise ConfigError("HTTP publication token must contain at least 32 characters")
    if any(character.isspace() for character in token):
        raise ConfigError("HTTP publication token must not contain whitespace")
    return token


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_copy(source: Path, destination: Path, mode: int = 0o600) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        os.chmod(temporary, mode)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def refresh_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RefreshError("Another refresh is already running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_command(command: list[str], verbose: bool = False) -> subprocess.CompletedProcess:
    if verbose:
        print("+ " + " ".join(shlex.quote(part) for part in command), file=sys.stderr)
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "command failed").strip()
        raise RefreshError(f"Command failed ({completed.returncode}): {detail[-4000:]}")
    if verbose and completed.stderr:
        print(completed.stderr.rstrip(), file=sys.stderr)
    return completed


def extractor_command(config: RefreshConfig, export_path: Path) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "scripts/homed_extract.py"),
        "--db",
        str(config.database),
        "--output",
        str(export_path),
    ]
    if config.verbose:
        command.append("--verbose")
    return command


def generator_command(config: RefreshConfig, export_path: Path, output_dir: Path) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "scripts/generate_inspector.py"),
        str(export_path),
        "--db",
        str(config.database),
        "--output-dir",
        str(output_dir),
    ]
    for flag, path in (
        ("--theme-config", config.inputs.theme_config),
        ("--private-overrides", config.inputs.private_overrides),
        ("--homebridge-config", config.inputs.homebridge_config),
    ):
        if path is not None:
            command.extend((flag, str(path)))
    return command


def validate_generated_output(directory: Path) -> dict[str, str]:
    export_path = directory / EXPORT_FILENAME
    data_path = directory / DATA_FILENAME
    report_path = directory / REPORT_FILENAME
    for path in (export_path, data_path, report_path):
        if not path.is_file() or path.stat().st_size == 0:
            raise RefreshError(f"Generated file is missing or empty: {path.name}")
    try:
        json.loads(export_path.read_text(encoding="utf-8"))
        payload = json.loads(data_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RefreshError(f"Generated JSON validation failed: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("rules"), list):
        raise RefreshError("Generated inspector payload does not contain a rules list")
    html = report_path.read_text(encoding="utf-8")
    if "HomeKit Inspector" not in html or "const data =" not in html:
        raise RefreshError("Generated inspector HTML is incomplete")
    return {
        EXPORT_FILENAME: sha256_file(export_path),
        DATA_FILENAME: sha256_file(data_path),
        REPORT_FILENAME: sha256_file(report_path),
    }


def remote_sha256(host: str, path: str, verbose: bool = False) -> str:
    code = "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())"
    remote_command = f"python3 -c {shlex.quote(code)} {shlex.quote(path)}"
    completed = run_command(["ssh", host, remote_command], verbose=verbose)
    digest = completed.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RefreshError("Remote server returned an invalid report hash")
    return digest


def publish_report(config: PublishConfig, report_path: Path, verbose: bool = False) -> str:
    expected_hash = sha256_file(report_path)
    if config.kind == "local":
        if config.path is None:
            raise RefreshError("Local publication path is not configured")
        destination = Path(config.path)
        atomic_copy(report_path, destination, mode=0o644)
        actual_hash = sha256_file(destination)
    elif config.kind == "ssh":
        host = config.host or ""
        destination = str(config.path or "")
        run_command(
            ["rsync", "-a", "--checksum", "--", str(report_path), f"{host}:{destination}"],
            verbose=verbose,
        )
        actual_hash = remote_sha256(host, destination, verbose=verbose)
    else:
        if not config.url or config.token_file is None:
            raise RefreshError("HTTP publication is not fully configured")
        try:
            token = read_publish_token(config.token_file)
            content = report_path.read_bytes()
        except OSError as exc:
            raise RefreshError(f"Unable to read HTTP publication input: {exc}") from exc
        request = urllib.request.Request(
            config.url,
            data=content,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "text/html; charset=utf-8",
                "X-Report-SHA256": expected_hash,
                "Accept": "application/json",
            },
        )
        try:
            opener = urllib.request.build_opener(NoRedirectHandler())
            with opener.open(request, timeout=config.request_timeout_seconds) as response:
                payload = json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise RefreshError(f"HTTP publication failed: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RefreshError("HTTP publication did not return ok=true")
        actual_hash = payload.get("sha256")
        if not isinstance(actual_hash, str):
            raise RefreshError("HTTP publication did not return a report hash")
    if actual_hash != expected_hash:
        raise RefreshError("Published report hash does not match generated report")
    return actual_hash


def check_health(config: PublishConfig) -> dict | None:
    if not config.health_url:
        return None
    request = urllib.request.Request(config.health_url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=config.health_timeout_seconds) as response:
            payload = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RefreshError(f"Server health check failed: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RefreshError("Server health check did not return ok=true")
    if payload.get("indexExists") is False:
        raise RefreshError("Server health check reports a missing inspector file")
    return {"ok": True, "indexExists": payload.get("indexExists")}


def safe_error_message(exc: Exception) -> str:
    return str(exc).replace(str(Path.home()), "~")[-4000:]


def run_refresh(config: RefreshConfig) -> dict:
    validate_config(config)
    working = config.working_directory
    working.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(working, 0o700)
    status_path = working / STATUS_FILENAME
    started_at = utc_now()
    started = time.monotonic()

    with refresh_lock(working / LOCK_FILENAME):
        atomic_write_json(status_path, {"version": 1, "state": "running", "startedAt": started_at})
        try:
            with tempfile.TemporaryDirectory(prefix=".refresh-", dir=working) as temporary_name:
                temporary = Path(temporary_name)
                export_path = temporary / EXPORT_FILENAME
                print("Extracting HomeKit data...", file=sys.stderr)
                run_command(extractor_command(config, export_path), verbose=config.verbose)
                print("Generating inspector...", file=sys.stderr)
                run_command(
                    generator_command(config, export_path, temporary), verbose=config.verbose
                )
                hashes = validate_generated_output(temporary)
                print("Publishing inspector...", file=sys.stderr)
                published_hash = publish_report(
                    config.publish, temporary / REPORT_FILENAME, verbose=config.verbose
                )
                health = check_health(config.publish)
                for filename in (EXPORT_FILENAME, DATA_FILENAME, REPORT_FILENAME):
                    destination = working / filename
                    publish_is_same_file = (
                        config.publish.kind == "local"
                        and destination.resolve() == Path(config.publish.path).resolve()
                    )
                    if not publish_is_same_file:
                        atomic_copy(temporary / filename, destination)

            completed_at = utc_now()
            result = {
                "version": 1,
                "state": "success",
                "startedAt": started_at,
                "completedAt": completed_at,
                "durationSeconds": round(time.monotonic() - started, 3),
                "publishType": config.publish.kind,
                "reportSha256": published_hash,
                "artifacts": hashes,
                "health": health,
            }
            atomic_write_json(status_path, result)
            return result
        except Exception as exc:
            failure = {
                "version": 1,
                "state": "failed",
                "startedAt": started_at,
                "completedAt": utc_now(),
                "durationSeconds": round(time.monotonic() - started, 3),
                "error": safe_error_message(exc),
            }
            atomic_write_json(status_path, failure)
            if isinstance(exc, (ConfigError, RefreshError)):
                raise
            raise RefreshError(str(exc)) from exc


def print_status(config: RefreshConfig) -> int:
    status_path = config.working_directory / STATUS_FILENAME
    if not status_path.is_file():
        print(json.dumps({"version": 1, "state": "never"}, indent=2))
        return 0
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RefreshError(f"Invalid status file: {exc}") from exc
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def print_config(config_path: Path, config: RefreshConfig) -> int:
    payload = {
        "version": CONFIG_VERSION,
        "configPath": str(config_path.expanduser().resolve()),
        "database": str(config.database),
        "workingDirectory": str(config.working_directory),
        "inputs": {
            "themeConfig": (
                str(config.inputs.theme_config) if config.inputs.theme_config else None
            ),
            "privateOverrides": (
                str(config.inputs.private_overrides)
                if config.inputs.private_overrides
                else None
            ),
            "homebridgeConfig": (
                str(config.inputs.homebridge_config)
                if config.inputs.homebridge_config
                else None
            ),
        },
        "publish": {
            "type": config.publish.kind,
            "host": config.publish.host,
            "path": str(config.publish.path) if config.publish.path is not None else None,
            "url": config.publish.url,
            "tokenFile": (
                str(config.publish.token_file) if config.publish.token_file else None
            ),
            "requestTimeoutSeconds": config.publish.request_timeout_seconds,
            "healthUrl": config.publish.health_url,
            "healthTimeoutSeconds": config.publish.health_timeout_seconds,
        },
        "verbose": config.verbose,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Configuration file (default: {DEFAULT_CONFIG})",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("refresh", "Extract, generate, validate, and publish the inspector"),
        ("validate-config", "Validate configuration and required local inputs"),
        ("show-config", "Print the effective configuration and resolved paths as JSON"),
        ("status", "Print the last refresh status as JSON"),
    ):
        command_parser = subparsers.add_parser(name, help=help_text)
        add_config_argument(command_parser)
    args = parser.parse_args()

    try:
        config = load_config(args.config)
        if args.command == "validate-config":
            validate_config(config)
            print("Configuration is valid")
            return 0
        if args.command == "show-config":
            return print_config(args.config, config)
        if args.command == "status":
            return print_status(config)
        result = run_refresh(config)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ConfigError as exc:
        print(f"Configuration error: {safe_error_message(exc)}", file=sys.stderr)
        return 2
    except RefreshError as exc:
        print(f"Refresh failed: {safe_error_message(exc)}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
