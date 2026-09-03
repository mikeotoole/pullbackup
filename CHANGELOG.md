# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.13.1] - 2026-09-03

### Fixed

- The browser no longer shows its own native Basic-auth credential dialog in
  front of the sign-in page. Loading the UI signed out fired the first `/api/*`
  call with no session cookie; the `401` answering it carried
  `WWW-Authenticate: Basic`, and a browser pops its dialog for that header even
  on a same-origin `fetch()`. Only after cancelling did the app route to
  `/login`. The challenge is now omitted when the caller is recognisably a
  browser — `Sec-Fetch-Mode` other than `navigate`, `X-Pullbackup-Client: web`
  (which the UI now sends), or `Accept: text/event-stream` for the run-log
  stream, which `EventSource` cannot mark any other way. Scripted callers, the
  container healthcheck and a browser navigating directly at `/openapi.json`
  still receive `WWW-Authenticate: Basic`, and an unauthenticated `/api/*`
  request is still refused with `401` — never a 200, never a redirect.

- The task-list type filter chips now read `all`, `rsync` and `zfs` instead of
  `All`, `Rsync` and `Zfs`. The underlying values were always lower case; a
  Tailwind `capitalize` utility on the chip was re-casing them at paint time,
  which is why the labels disagreed with every other piece of UI chrome.

## [0.13.0] - 2026-09-02

### Changed

- Session cookies are now signed with `itsdangerous` (Pallets, and what
  Starlette's own `SessionMiddleware` uses) instead of ~100 lines of
  hand-rolled HMAC, base64url and issued-at handling. Expiry, the 60-second
  clock-skew tolerance, revocation and the login throttle are unchanged and
  stay in this codebase. **Upgrading signs every existing session out once**:
  the token format changed and a cookie from the old signer is deliberately
  rejected rather than reinterpreted.

## [0.12.1] - 2026-08-31

### Security

- Refuse to start when `PULLBACKUP_AUTH_STORE_MAX_ENTRIES` is below 100. The
  loader previously accepted any integer, including 0 and negatives. Because
  the store reserves `max(1, cap // 2)` for completed lockouts, a cap of 1 left
  one locked-out client occupying the whole store and every newcomer evicted at
  insert: a fresh client survived eight consecutive failed logins with
  `retry_after` 0 each time. The throttle was absent rather than broken, and
  nothing warned.

### Fixed

- An rsync task may again write to a destination that is exactly one of the
  configured destination roots. 0.12.0's destination hardening rejected it, so
  tasks using a dedicated, least-privilege per-task bind mount failed before
  rsync with `PathNotAllowed: <path> is a configured destination root`. The rule
  worked against its own intent: it forced operators to mount a broader parent
  purely to manufacture a descendant path. Containment is unchanged — a
  destination must still canonicalize to a configured root or a path beneath
  one, with every other control (control-character rejection, symlink and
  traversal handling, no-follow descriptor-pinned execution) intact.
- When configured roots overlap, a destination now binds to the most specific
  containing root instead of the first one declared. Previously
  `/backups,/backups/critical` and `/backups/critical,/backups` resolved the
  same destination against different roots, which is the root the pinned
  traversal opens.

## [0.12.0] - 2026-08-31

### Security

- Bound both in-memory auth stores (revoked sessions, failed-login history) with
  one shared expiring structure. Both previously grew without limit on a key
  space an unauthenticated caller can influence, and the sweep added for the
  second scanned the whole store on every login request. Per-request login
  latency no longer degrades with store size (was 7.4x worse at 50,000 live
  entries).
- Key the login throttle on the real client behind a trusted proxy, using every
  `X-Forwarded-For` header line rather than only the first. HAProxy emits a
  second line rather than appending, so the throttle could be bypassed entirely
  by a client setting its own header.
- Give the lockout-protected tier a bounded share of the store. Without it a
  saturated tier meant a new client's entry was always the one evicted, so that
  client could never accumulate failures and never locked out.
- Warn when a revocation is dropped while the session could still be accepted.
  The warning existed but was unreachable.
- The destination allowlist can no longer be widened by a path that is
  renamed or symlinked between validation and use.

### Added

- `PULLBACKUP_AUTH_STORE_MAX_ENTRIES` (default 100,000) caps both auth stores.
- **Copyright attribution.** `NOTICE` names the holder (Mike O'Toole) and every
  first-party source file carries a one-line
  `SPDX-License-Identifier: AGPL-3.0-or-later` header with a copyright line.
  `LICENSE` stays byte-for-byte the FSF text; a test pins its sha256.
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

- Ship the licence text in the distributed image and declare it in the frontend
  package metadata (AGPL sections 4 and 6).
- Derive the reported version from packaging metadata instead of a hardcoded
  literal, ending a three-way disagreement between `__init__.py`,
  `pyproject.toml` and the image tag.
- **The reported version is no longer wrong.** `backend/pyproject.toml` is the
  single source of truth and `pullbackup.__version__` is derived from installed
  distribution metadata, so `/api/system/health` and `/api/system/info` report
  what was actually packaged. Three sources previously disagreed (0.10.0 in the
  package, 0.5.1 in the packaging metadata, 0.11.0 deployed), which made a good
  rollout look like a failed one.
- rsync destinations are pinned to a directory descriptor, closing a TOCTOU
  window between validating a destination and writing to it.
- Destination validation no longer treats an unreadable path component as
  "does not exist", which previously let an unvalidated destination through.
- Task creation rejects a destination that cannot be resolved instead of
  returning a 500.
