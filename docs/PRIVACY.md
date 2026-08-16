# Privacy and Publishing Checklist

HomeKit exports describe a real home. They can reveal occupancy patterns,
security logic, room layout, device inventory, bridge topology, and automation
schedules. Generated files are sensitive by default.

## Private Files

- `local-output/`
- `output/`
- `homekit_homed_export.json`
- `homekit_inspector_data.json`
- `homekit_inspector.html`
- raw `core.sqlite`, `core.sqlite-wal`, or `core.sqlite-shm`
- Controller for HomeKit backup files
- Homebridge `config.json`
- theme assignments from a real home
- private overrides from a real home
- screenshots of the inspector from a real home
- report upload tokens and token files

These files are kept out of public commits.

## Public Files

- extractor code;
- HTML generator code;
- condition decoder code;
- synthetic fixtures;
- redacted examples;
- documentation that does not include household-specific names or topology.

## Local Output

Generated files live under `local-output/`, which is ignored by git.

```bash
mkdir -p local-output
python3 scripts/homed_extract.py \
  --db ~/Library/HomeKit/core.sqlite \
  -o local-output/homekit_homed_export.json
```

## LAN Serving

Serving `homekit_inspector.html` from a Raspberry Pi or another LAN host does
not make the report less sensitive. The served directory remains private, and
the report is intended only for a trusted LAN or VPN rather than public DNS,
port forwarding, or an unauthenticated internet-facing reverse proxy.

Authenticated API publication keeps its bearer token in a separate `0600`
file. The token must not be committed, embedded in generated HTML, or placed in
the server's document root. Plain HTTP does not encrypt either the token or the
report; use HTTPS when the transport network is not trusted.

## Homebridge Context

Homebridge configuration often contains credentials, webhook URLs, hostnames,
serial numbers, plugin topology, and room/device naming conventions. Real
Homebridge configs are private files.

Documentation and tests can use a small synthetic file such as
`examples/homebridge-context.example.json`.

## Theme Configuration

Themes are editorial metadata. They may expose how a household thinks about
security, presence simulation, sleep modes, access, or family routines.

`examples/theme-config.example.json` is the public schema reference. Real
assignments live in `local-output/homekit_theme_config.json`.

## Private Overrides

Private overrides cover local facts that cannot be derived from HomeKit or a
context source.

`examples/private-overrides.example.json` is the public schema reference. Real
overrides live in `local-output/homekit_private_overrides.json`.

## Pre-Publish Audit

Before pushing or opening a public pull request:

```bash
git status --short --ignored
rg -n "YOUR_REAL_NAME|REAL_ROOM|REAL_DEVICE|REAL_HOST|REAL_TOKEN" \
  -g '!local-output/**' -g '!output/**' .
rg -n "/Users/|REAL_HOST|REAL_TOKEN|REAL_SERIAL|REAL_WEBHOOK" \
  -g '!local-output/**' -g '!output/**' .
```

Also check tracked generated files:

```bash
git ls-files local-output output
```

The command is expected to return no real generated HomeKit outputs.

## Runtime Safety

The extractor is read-only:

- open SQLite with `mode=ro`;
- do not write to `core.sqlite`, `core.sqlite-wal`, or `core.sqlite-shm`;
- do not restart, kill, or manipulate `homed`;
- do not run database migrations or vacuum operations against the live HomeKit
  database.
