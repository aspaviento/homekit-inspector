# AGENTS.md

Guidance for coding agents working on HomeKit Inspector.

## Project Scope

HomeKit Inspector is a local, read-only inspection tool for Apple's HomeKit
`homed` SQLite store. It extracts HomeKit structure and automation data, then
generates a self-contained HTML inspector for private review.

The project does not control HomeKit devices, modify HomeKit state, or write to
the HomeKit database.

## Safety And Privacy

- Open HomeKit SQLite databases read-only with `mode=ro`.
- Keep generated exports, generated HTML reports, real HomeKit database copies,
  real Homebridge configuration, screenshots from real homes, theme assignments,
  and private overrides out of version control.
- Treat generated inspector output as sensitive household data.
- Use synthetic fixtures and redacted examples for committed tests and docs.
- Do not add real hostnames, local machine paths, personal identifiers, room
  names, device names, serial numbers, webhook URLs, tokens, or deployment
  details to public files.

## Development Workflow

- Prefer small, focused changes that preserve the read-only extraction model.
- Keep extraction logic, HTML generation, docs, and examples separated by their
  existing responsibilities.
- When changing generated HTML behavior, update `scripts/generate_inspector.py`
  rather than editing generated output directly.
- Regenerate example or local HTML output only as a validation step unless the
  generated artifact is intentionally tracked.
- Use structured parsers for SQLite, plist, JSON, and HTML/JavaScript checks
  where practical.

## Validation

Run the unit tests before finishing code changes:

```bash
python3 -m unittest discover -s tests
```

For changes that touch inline JavaScript or generated HTML structure, also parse
the generated HTML script blocks with Node or an equivalent JavaScript parser.

For extractor changes, validate with synthetic fixtures first. Real HomeKit
captures may be used locally, but their outputs remain private.

## Documentation Style

- Keep public documentation descriptive rather than written as internal agent
  instructions.
- Describe project behavior and constraints in neutral terms.
- Keep operational examples generic and portable.
- Avoid references to private infrastructure, local repository names, personal
  filesystem paths, or household-specific deployment details.

## Release Notes

Update `CHANGELOG.md` when preparing a user-visible release. Public release notes
should summarize product behavior and developer-facing changes without exposing
private installation details.
