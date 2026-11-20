# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Entries for new releases are generated with `cz bump`.

## [Unreleased]

## 0.2.0 (2026-11-20)

### Added

- Offline `examples/deny_and_audit.py`: one allowed and one denied Agent Framework tool call, followed by an
  application decision trail.
- Compatibility regression coverage for nested JSON-native arguments and bytes/bytearray formatting.

### Changed

- Document the compatibility matrix: acgs-lite 4.0.0 in the locked environment and the 3.0.1 floor in CI's
  `test-min` job. Runtime support remains `acgs-lite>=3.0.1,<5`.
- Clarify that the adapter does not issue signed receipts.

## 0.1.0 (2026-10-23)

### Added

- `ACGSGuard` with `guard.middleware`: a function middleware that checks each function call's name and arguments
  against an ACGS constitution before the function runs, and a chat middleware that refuses runs offering tools
  the function middleware cannot check.
- Review through Agent Framework's tool approval flow, with the call checked again when it is replayed.
- `on_review="deny"`, `on_deny="raise"`, `allow_ungoverned=True` and `tool_call_formatter=` options.
- `mcp` extra for local MCP tools.
