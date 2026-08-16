# Refresh CLI

The refresh CLI turns HomeKit Inspector's extraction and HTML generation steps
into one repeatable operation. It remains read-only with respect to HomeKit and
publishes only validated generated artifacts.

## User Installation

Install the CLI without `sudo`:

```bash
./install-cli.sh --config /path/to/private-refresh-config.json
```

The installer copies executable code to
`~/Library/Application Support/HomeKit Inspector/app`, migrates the selected
configuration to `~/Library/Application Support/HomeKit Inspector/config.json`,
copies optional theme, override, and Homebridge inputs into a private `config/`
directory, and creates `~/.local/bin/homekit-inspector`.

The installed layout separates replaceable code from stable private data:

```text
~/Library/Application Support/HomeKit Inspector/
├── app/
├── config.json
├── config/
└── output/
```

Code updates are installed by rerunning the same command. Existing
configuration and outputs are preserved. Use `--replace-config` together with
`--config FILE` to replace and remigrate the installed configuration.

Custom locations can be selected with `--app-home` and `--bin-dir`, or with
`HOMEKIT_INSPECTOR_HOME` and `HOMEKIT_INSPECTOR_BIN_DIR`. The Python executable
can be selected with `HOMEKIT_INSPECTOR_PYTHON`.

Terminal or the configured Python executable must retain Full Disk Access for
the extractor to read the protected HomeKit database.

Uninstall code and the command while preserving private data:

```bash
./uninstall-cli.sh
```

`./uninstall-cli.sh --remove-private-data` also deletes the installed
configuration, copied context inputs, output reports, and refresh status. The
destructive option is explicit, and the uninstaller rejects critical directory
targets such as `/`, the home directory, or `~/Library`.

## Commands

```bash
bin/homekit-inspector validate-config --config CONFIG.json
bin/homekit-inspector show-config --config CONFIG.json
bin/homekit-inspector refresh --config CONFIG.json
bin/homekit-inspector status --config CONFIG.json
```

The action is a subcommand; options keep the `--` prefix. For example,
`refresh` is the action and `--config` selects its configuration file.

The launcher resolves the project-relative Python entry point and uses `python3`
by default. A specific Python 3.9 or later executable can be selected without
editing the scripts:

```bash
HOMEKIT_INSPECTOR_PYTHON=/path/to/python3 bin/homekit-inspector status \
  --config CONFIG.json
```

Direct invocation of `scripts/homekit_inspector_cli.py` remains supported for
development and embedding.

Without `--config`, the CLI looks for:

```text
~/Library/Application Support/HomeKit Inspector/config.json
```

This is the path populated by `install-cli.sh`, so installed commands normally
do not need `--config`.

If the database contains more than one HomeKit home, `refresh` selects the home
marked as primary by HomeKit. Its log prints the selected name, selection
method, and number of available homes; the same information is stored in the
successful refresh status. Explicit selection of another home is not supported
in this version.

`validate-config` checks the schema, database and optional input files, and the
local executables needed by the selected publisher. It does not extract data.

`show-config` prints the effective configuration file, resolved filesystem
paths, optional input locations, and publication settings as JSON. It does not
read the HomeKit database, inspect the optional input contents, or contact the
publication target.

`refresh` performs these steps:

1. Acquire a non-blocking refresh lock.
2. Extract `core.sqlite` through the existing read-only extractor.
3. Generate JSON and self-contained HTML in a private temporary directory.
4. Parse and validate the generated artifacts.
5. Publish the HTML and verify its SHA-256 hash.
6. Optionally check the server's `/health` endpoint.
7. Atomically update the private working copies and status file.

`status` prints machine-readable JSON describing the most recent run. It does
not access the HomeKit database or contact the publication target.

## Configuration

The configuration format is versioned JSON. Real configurations contain
private filesystem and network information and should not be committed.
Store them outside the repository or under an ignored private directory, and
use owner-only permissions such as `chmod 600 CONFIG.json`.

```json
{
  "version": 1,
  "database": "~/Library/HomeKit/core.sqlite",
  "workingDirectory": "~/Library/Application Support/HomeKit Inspector/output",
  "inputs": {
    "themeConfig": null,
    "privateOverrides": null,
    "homebridgeConfig": null
  },
  "publish": {
    "type": "local",
    "path": "~/Library/Application Support/HomeKit Inspector/published/homekit_inspector.html"
  },
  "verbose": false
}
```

Relative paths are resolved from the configuration file's directory. The
working directory is private and contains the current export, inspector data,
HTML report, lock, and refresh status. Generated private data files use owner-
only permissions.

Optional inputs preserve the existing separation between HomeKit-derived data
and local enrichment:

- `themeConfig`: explicit automation theme assignments.
- `privateOverrides`: sparse private corrections or annotations.
- `homebridgeConfig`: optional structural Homebridge context.

## Local Publication

The local publisher atomically replaces one report file:

```json
{
  "publish": {
    "type": "local",
    "path": "~/Documents/HomeKit Inspector/homekit_inspector.html"
  }
}
```

This is the default mode and does not need a report server. The destination
directory is created when possible. The published HTML and private working
artifacts use mode `0600`.

When the `publish` object is omitted entirely, the default destination is:

```text
~/Library/Application Support/HomeKit Inspector/published/homekit_inspector.html
```

## Server Publication

Server publication sends the validated self-contained report to the optional
HomeKit Inspector report server:

```json
{
  "publish": {
    "type": "server",
    "url": "https://inspector-server.example.net:8099/api/v1/report",
    "secretFile": "config/publish-secret",
    "caFile": "config/server-ca.pem",
    "requestTimeoutSeconds": 30,
    "healthUrl": "https://inspector-server.example.net:8099/health",
    "healthTimeoutSeconds": 5
  }
}
```

`secretFile` contains exactly 64 lowercase hexadecimal characters representing
a random 256-bit key. `caFile` is required for a private CA and omitted when the
server certificate is already trusted by macOS. Both are copied into private
installed storage by `install-cli.sh --config FILE`.

The secret is not stored in JSON, displayed by `show-config`, included in the
report, passed as a process argument, or transmitted. The client signs each
request with HMAC-SHA256 over the endpoint, timestamp, one-time nonce, report
digest, and content length. Redirects are rejected.

The server rejects unsigned, stale, replayed, oversized, incomplete,
hash-mismatched, or structurally invalid reports. A valid report atomically
replaces the previous HTML file, and the response returns the stored digest for
client verification.

The server API provides:

- `POST /api/v1/report`: authenticated report publication.
- `GET /api/v1/status`: current report size, modification time, and SHA-256.
- `GET /health`: server and index availability.

Remote server URLs must use HTTPS. Plain HTTP is accepted only for loopback
development or a loopback-only upstream behind an HTTPS reverse proxy. See
[SERVER.md](SERVER.md) for server installation, TLS setup, reverse-proxy
requirements, credential rotation, and operational security.

## Failure Behavior

- Concurrent refresh attempts fail without disturbing the running operation.
- Generation occurs in a temporary directory under the private working path.
- Invalid or incomplete output is never published.
- Publication is hash-verified before success is recorded.
- A failed run records a redacted error and leaves the previously published
  report available.
- The CLI never restarts `homed`, writes to HomeKit SQLite files, or changes
  HomeKit accessories and automations.

Browser-initiated refresh remains outside this version. The API accepts a
completed report from the macOS agent; it cannot access or extract the Mac's
HomeKit database.
