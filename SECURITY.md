# Security policy

## Supported versions

Security fixes target the latest published release of HomeKit Inspector.
At publication of this policy, that release is **v0.4.1**. Older releases do
not receive separate security backports. Reports affecting development code
are welcome; reproduce against the latest release where practical.

This is a community-maintained project. Response and remediation times depend
on maintainer availability; no response deadline or support SLA is promised.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting form:

[Report a vulnerability privately](https://github.com/aspaviento/homekit-inspector/security/advisories/new)

Include the affected version or commit, installation details, reproduction
steps, expected and observed behavior, and potential impact. Use synthetic
examples and redact credentials, personal information, and private network
details. Real HomeKit databases, reports, Homebridge configuration, and
screenshots can reveal household layout, occupancy, and security routines;
they are not suitable public reproduction attachments.

Coordinate disclosure with the maintainer while the report is assessed.
Ordinary bugs and feature requests belong in public issues; exploit details
and sensitive household data belong in a private report.

## Security boundaries

The macOS extractor opens the HomeKit SQLite store read-only. The report
server cannot initiate extraction or control HomeKit devices. Generated
reports remain sensitive even though the inspection workflow is read-only.

Server publication authenticates possession of a shared secret using HMAC,
timestamps, nonces, and report digests. It does not identify a particular
client executable. Any process able to read the publication secret can sign
an upload. Viewer access uses separate HTTP Basic credentials.

HTTPS is the default network transport. Plain HTTP requires explicit opt-in
on the server and client and is intended only for a trusted LAN or private
VPN. HMAC does not encrypt reports or viewer credentials. The initial
admin/admin viewer credentials are intended for setup on a trusted network.

Keep real exports, generated reports, private annotations, publication
secrets, server configuration, and private keys outside version control.
See [Privacy](docs/PRIVACY.md) and [Report server](docs/SERVER.md) for storage,
deployment, and credential-handling details.
