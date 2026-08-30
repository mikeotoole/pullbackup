# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Preparing the first public release. Nothing below has shipped in a tagged
version yet.

### Added

- **Licensed under AGPL-3.0-or-later.** Pullbackup is server software you
  self-host; section 13 obliges anyone who modifies it and offers it to others
  over a network to publish their source. Running an unmodified copy carries no
  such obligation.
- A login page and signed `HttpOnly` cookie session over the existing HTTP
  Basic credential. Basic auth still works for scripted access and the compose
  healthcheck, and `PULLBACKUP_SESSION_SECRET` optionally pins the signing key.
- Card layouts for the task, run-history and source lists below the `sm`
  breakpoint, replacing horizontal scrolling on phones.
- `PULLBACKUP_DB_FILENAME` to override the SQLite filename inside the data
  directory, so the database can be renamed deliberately rather than as a
  side effect of an upgrade.
- `SECURITY.md`, `CONTRIBUTING.md` and this changelog.
- `docker/compose.example.yaml` as a neutral deployment example.

### Changed

- Renamed the product, Python package and environment prefix to Pullbackup /
  `pullbackup` / `PULLBACKUP_`. A retired `PULLBACK_` variable with no
  `PULLBACKUP_` counterpart is adopted with a warning rather than ignored, so
  an unmigrated deployment keeps its configured values instead of silently
  falling back to defaults.
- UI chrome is lower case; run states remain upper case.
- Generated SSH key comments use the running hostname instead of a hardcoded
  one.

### Fixed

- rsync destinations are pinned to a directory descriptor, closing a TOCTOU
  window between validating a destination and writing to it.
- Destination validation no longer treats an unreadable path component as
  "does not exist", which previously let an unvalidated destination through.
- Task creation rejects a destination that cannot be resolved instead of
  returning a 500.

### Security

- The destination allowlist can no longer be widened by a path that is
  renamed or symlinked between validation and use.
