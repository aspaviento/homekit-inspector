# Report Server

HomeKit Inspector supports two publication modes:

- `local`: generate and atomically replace an HTML file on the Mac. This is
  the default and does not require a web server.
- `server`: send the completed report to the HomeKit Inspector report server
  through its authenticated API.

The server never accesses HomeKit and cannot initiate extraction on the Mac.
Browser-initiated refresh is outside this version.

## Security Model

Server publication combines two independent controls:

1. HTTPS encrypts the report and authenticates the server.
2. HMAC-SHA256 authenticates each publication using a private 256-bit shared
   secret that is never transmitted.

The signature covers the protocol version, HTTP method, endpoint, timestamp,
one-time nonce, report SHA-256, and content length. The server rejects stale
timestamps, reused nonces, invalid signatures, oversized bodies, digest
mismatches, and non-Inspector HTML before atomically replacing the report.
Served HTML also receives a restrictive Content Security Policy: the
self-contained inspector may run its inline JavaScript and CSS, but it cannot
connect to remote endpoints, submit forms, load remote resources, or be framed.

This authenticates possession of the private publication secret. No portable
server protocol can prove that the caller is a particular Python executable.
Any process that can read the client's secret can impersonate the client, so
the secret file and the macOS account remain part of the trust boundary.

The server refuses authenticated publication over plain network HTTP. Plain
HTTP is accepted only on loopback for a TLS reverse-proxy deployment.

This design follows OWASP guidance that secure REST endpoints use HTTPS and
that TLS 1.0/1.1 remain disabled. The built-in server uses Python's server-side
`SSLContext` with TLS 1.2 as its minimum version:

- [OWASP REST Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html)
- [OWASP Transport Layer Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Transport_Layer_Security_Cheat_Sheet.html)
- [Python `ssl` documentation](https://docs.python.org/3/library/ssl.html)

## Create Private Credentials

For a private LAN or VPN deployment, create a private CA, server certificate,
server key, and publication secret on a trusted administration machine:

```bash
scripts/create-server-credentials.sh \
  --hostname inspector-server.example.net \
  --output-dir /path/to/private/server-credentials
```

The hostname must match the address used by the client. The generated files
are:

```text
ca-key.pem       Private CA key; keep offline and never deploy to the server
ca.pem           CA certificate used by the macOS client
server-cert.pem  TLS certificate deployed to the server
server-key.pem   TLS private key deployed to the server
publish-secret   Shared HMAC secret stored on the server and macOS client
server.json      Private HTTP Basic viewer configuration
```

Store this directory outside the repository. Transfer only
`server-cert.pem`, `server-key.pem`, `publish-secret`, and `server.json` to
the server through a secure administrative channel. Keep a `0600` copy of
`ca.pem` and `publish-secret` for the macOS client.

The generated `server.json` contains the initial credentials:

```json
{
  "viewer": {
    "username": "admin",
    "password": "admin"
  }
}
```

Change these values before exposing the server outside a trusted network.
The file contains a readable password and must remain private; it is not a
public example or a client configuration.

For an internet-facing hostname, a certificate from a publicly trusted CA or a
well-maintained HTTPS reverse proxy is preferable to the private-CA example.

## Install The Server

Run the included installer from a cloned checkout on the Linux host:

```bash
sudo ./install-server.sh \
  --install-dir /opt/homekit-inspector \
  --data-dir /var/lib/homekit-inspector \
  --host 0.0.0.0 \
  --port 8099 \
  --publish-secret-file /path/to/publish-secret \
  --tls-cert-file /path/to/server-cert.pem \
  --tls-key-file /path/to/server-key.pem \
  --server-config-file /path/to/server.json
```

The installer:

- validates the certificate/key pair and 256-bit secret;
- installs server code under `/opt/homekit-inspector` by default;
- stores the served report under `/var/lib/homekit-inspector` by default;
- installs code, publication secrets, and TLS keys as root-owned files, with
  private files readable but not writable by the service group;
- installs the supplied server configuration as
  `/var/lib/homekit-inspector/server.json`, owned by the service account with
  mode `0600`;
- installs and enables `homekit-inspector.service`;
- preserves an already installed `server.json` during code-only updates.

When `--server-config-file` is omitted on a new installation, the installer
creates `/var/lib/homekit-inspector/server.json` with `admin/admin`. The service
account can edit that file directly; restart the service afterward:

```bash
nano /var/lib/homekit-inspector/server.json
sudo systemctl restart homekit-inspector.service
```

Supplying `--server-config-file` copies that file into the data directory.
Omitting the option during later code updates preserves the installed
configuration. Supplying it again intentionally replaces the installed
credentials.

## Viewer Authentication

Browser access uses the native HTTP Basic authentication dialog. The server
accepts this configuration schema:

```json
{
  "viewer": {
    "username": "admin",
    "password": "admin"
  }
}
```

The username must be non-empty and cannot contain whitespace or a colon. The
password must contain between 1 and 256 characters without control characters.
Credentials are loaded when the service starts, so editing `server.json`
requires a service restart.

Although `server.json` shares the data directory with the generated report,
it is not web content. The server explicitly returns `404` for both `GET` and
`HEAD` requests targeting the configuration file. Filesystem permissions add a
separate control: the configuration is owned by the service account with mode
`0600`.

After a successful challenge, browsers normally reuse HTTP Basic credentials
for the current browser session. Persistence across browser restarts is a
browser behavior rather than a server setting.

By default, the service runs as the non-root user that invoked `sudo`. An
existing unprivileged account can be selected with `--user`; root is rejected.
Network listeners require viewer authentication by default. The explicit
`--allow-unauthenticated-view` option is intended only for deployments where a
VPN or reverse proxy already enforces access control.

Start the service after reviewing the generated systemd unit:

```bash
sudo systemctl restart homekit-inspector.service
sudo systemctl status homekit-inspector.service
```

Validate TLS and server health from a machine holding `ca.pem`:

```bash
curl --fail --cacert /path/to/ca.pem \
  https://inspector-server.example.net:8099/health
```

Browsers must also trust the issuing CA. For a private CA, verify the
certificate fingerprint through the administrative channel before importing
`ca.pem` into the browser or operating-system trust store. Never import
`ca-key.pem` and do not bypass certificate warnings.

## Configure The macOS Client

Use `server` publication and reference private files rather than embedding
credentials in JSON:

```json
{
  "version": 1,
  "publish": {
    "type": "server",
    "url": "https://inspector-server.example.net:8099/api/v1/report",
    "secretFile": "/path/to/publish-secret",
    "caFile": "/path/to/ca.pem",
    "requestTimeoutSeconds": 30,
    "healthUrl": "https://inspector-server.example.net:8099/health",
    "healthTimeoutSeconds": 5
  }
}
```

`caFile` may be omitted when the server certificate chains to a CA already
trusted by macOS. Install or replace the CLI configuration with:

```bash
./install-cli.sh --config /path/to/config.json --replace-config
homekit-inspector validate-config
homekit-inspector refresh
```

The CLI does not follow redirects during publication and validates the server
certificate, hostname, response, and returned report digest.

## Reverse Proxy Deployment

When TLS terminates at Caddy, nginx, or another reverse proxy, bind the Python
server to loopback and omit its direct TLS options:

```bash
sudo ./install-server.sh \
  --host 127.0.0.1 \
  --port 8099 \
  --publish-secret-file /path/to/publish-secret
```

The reverse proxy must:

- expose only HTTPS;
- enforce viewer authentication before serving the report;
- validate and renew its certificate;
- preserve the `X-Inspector-*`, `X-Report-SHA256`, `Content-Type`, and
  `Content-Length` headers;
- proxy to loopback only;
- enforce an upload-size limit no larger than the server limit;
- avoid request-body logging.

The client URL remains the external HTTPS URL. Do not configure the client to
use the loopback upstream address.

## Operational Security

- Do not expose the report server through plain HTTP.
- Restrict ingress with a firewall or VPN wherever possible.
- Keep the operating system, Python, TLS library, and reverse proxy patched.
- Back up neither `publish-secret` nor private keys into public repositories.
- Do not publish, commit, or place copies of `server.json` under another served
  filename.
- Rotate the publication secret after suspected disclosure.
- Replace the initial `admin/admin` viewer credentials on any network that is
  not fully trusted.
- Rotate certificates before expiry and retain the CA key offline.
- Review `/api/v1/status` and the CLI's returned SHA-256 after publication.
- Keep server and Mac clocks synchronized; signed requests allow a five-minute
  clock window.
