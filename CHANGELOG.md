# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Entries for new releases are generated with `cz bump`.

## [Unreleased]

## 0.1.0 (2026-10-23)

### Added

- `ACGSGuard` with `guard.middleware`: a function middleware that checks each function call's name and arguments
  against an ACGS constitution before the function runs, and a chat middleware that refuses runs offering tools
  the function middleware cannot check.
- Review through Agent Framework's tool approval flow, with the call checked again when it is replayed.
- `on_review="deny"`, `on_deny="raise"`, `allow_ungoverned=True` and `tool_call_formatter=` options.
- `mcp` extra for local MCP tools.
