# Changelog

## Unreleased

- Added a service-owned private JSON configuration for readable HTTP Basic
  viewer credentials, installed with initial `admin/admin` values in the
  server data directory.
- Scoped extraction, layout, infrastructure, scenes, and condition lookups to
  the HomeKit primary home when multiple homes share the local database.
- Added extraction metadata and logging for the selected home and available
  home count.
- Added signed HTTPS report publication with HMAC-SHA256, timestamp and nonce
  replay protection, SHA-256 verification, upload limits, and atomic replacement.
- Reduced publication to two explicit modes: default local HTML generation and
  secure publication to the optional report server.
- Added private credential migration, direct TLS support, credential generation,
  and a documented server installer entry point.
- Added a user-scoped CLI installer with private configuration migration,
  repeatable code updates, a stable command path, and a safe uninstaller that
  preserves private data by default.
- Added a `show-config` CLI subcommand for displaying the effective
  configuration file and resolved paths without accessing HomeKit data.
- Added a configuration-driven refresh CLI with locking, temporary generation,
  validation, status reporting, and atomic publication.
- Added a stable command launcher with refresh, validation, and status
  subcommands and a configurable Python executable.
- Added independent contextual filters to Home Layout, Bridges, Manufacturers,
  Capabilities, Scenes, and Theme Editor while retaining the existing
  Automations filters.
- Added decoded HomeKit service capability badges and a Capabilities tab for grouping accessories by service type.
- Expanded decoded HomeKit service capabilities to include fans, lightbulbs, and switches.
- Refined the summary ribbon into clickable high-level inventory and navigation metrics.

## 0.3.0 - 2026-08-08

- Added the optional static LAN server and systemd install assets for serving a generated inspector report from a private host.
- Improved mobile ergonomics with a compact header and inline automation and scene details.
- Added HomeKit capture timestamp display to the inspector header.
- Added automation participation tracing in Home Layout and Manufacturers views.
- Added bridge-origin and Matter integration markers on accessory cards.
- Improved navigation with dedicated infrastructure tabs and fixed bridge search filtering.
- Expanded scene/action decoding, including provided names, decoded values, and media actions.
- Added automatic dark mode using the browser color-scheme preference.
- Added Standard/Advanced automation classification, badges, and filtering.
- Added the HomeKit Inspector house-and-magnifier logo.

## 0.2.0

- Previous tagged public version.
