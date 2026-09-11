# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Stop a run that is already in flight. Previously the only way to end a
  transfer was to restart the whole application, which terminates every run and
  throws away the scheduler's state. `POST /api/runs/{id}/cancel` terminates the
  run's isolated process group through the existing graceful TERM → bounded wait
  → KILL path (so the ssh or `zfs send` the transfer spawned goes with it), keeps
  the run's log, and records the previously-unused `cancelled` state. The task
  list shows a Stop control on desktop and phone while a run is RUNNING, behind a
  confirmation, and Run now stays a separate control.

  Cancellation is keyed on a run-id ownership map rather than on a pid: pids are
  recycled, and a run's pid is not knowable until after the exec. A request can
  therefore only ever signal the execution this process started for that exact
  run. A pending run that has not started, a run left behind by a previous
  process, and an unknown id each get an honest refusal with the row untouched,
  and a run that completes while the stop request is in flight reports
  `already finished (success)` rather than claiming it was cancelled.

- Tasks can now be edited while a run is active, for the fields that cannot
  reach a transfer already executing: name, description, cron, enabled, and the
  Matrix/Uptime Kuma notification flags. Everything that shapes the command or
  decides whose data is copied — source, paths, task type, rsync/syncoid flags
  and arguments, bandwidth, pruning — stays locked with a 409 that names each
  unsafe field. The lock is on a CHANGED value rather than on a field's presence,
  so the form's full-task PATCH is accepted. Disabling a task mid-run now
  succeeds too: it removes the future scheduler job and sends no signal, and the
  running transfer finishes normally.

- `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), GitHub issue templates and a
  pull-request template. The bug template asks for a version and redacted logs
  up front, because a backup tool's logs are full of real hostnames and paths
  and the first reply to a report should not have to be "please remove your
  network layout from this".

- The README now shows the UI. Six screenshots — the task list, a finished run
  with its full `rsync` output, per-task run history, the task form, sources,
  and the task list on a phone — all taken from the throwaway instance
  `scripts/seed-demo.py` produces, so every host, path, schedule and byte count
  in them is invented. `tests/test_readme_screenshots.py` fails if a referenced
  image is missing, if a committed screenshot is never shown, or if the README
  stops saying where the data came from; the last of those is the only thing
  keeping a hurried maintainer from publishing a picture of their own instance.

- Build instructions. No image is published yet, so the README's Deployment
  section now says to build one from a checkout, and
  `docker/compose.example.yaml` builds from the repository root instead of
  pulling a placeholder `ghcr.io/OWNER` image that never existed.

### Changed

- Run logs open with `# pullbackup run N (type)` rather than the pre-rename
  `# pullback run`; the seeded demo instance writes the same header.
- The frontend package is `pullbackup-frontend` in `package.json` and the lock
  file, matching the product name everywhere else.

### Removed

- `docs/research/product-name.md` and its guard test. The note weighed a rename
  away from "Pullback" because of a namespace collision with an existing
  rsync-based project of nearly the same name; that decision was made and the
  deliberation belongs in private notes rather than shipped alongside the
  product it argues about.

- `frontend/tsconfig.tsbuildinfo` is no longer tracked. It is TypeScript's
  incremental-build cache and changed on every build; `*.tsbuildinfo` is now
  ignored.

## [0.14.0] - 2026-09-03

### Added

- `scripts/seed-demo.py` stands up a throwaway local instance seeded with
  entirely fabricated data, for README and marketing screenshots. It builds the
  image from the checkout, runs it on `127.0.0.1:18080` against a scratch
  directory, creates two invented sources and eleven tasks through the API,
  writes ~40 runs and their logs directly to SQLite, and prints the URL and
  credentials. Safe to
  re-run: it tears the previous instance down first and seeds from a fixed RNG
  seed, so two runs produce the same data. `docs/demo.md` explains it.

  Because the credentials are printed on stdout, the publish spec names the
  loopback address explicitly rather than using Docker's `-p 18080:8000`
  shorthand, which binds `0.0.0.0` *and* the IPv6 wildcard and would expose
  those credentials to the LAN. `--verify` re-proves the binding against the
  live container with `docker port` and `docker inspect`, and
  `tests/test_demo_publish_binding.py` fails if the host IP is ever dropped.

  The seeded tasks carry real cron schedules, so the scheduler does try to fire
  them; four independent layers stop that becoming an outbound connection —
  blackholed DNS, hosts that resolve nowhere, a non-terminal run per source that
  makes every admission fail closed, and an `ssh_key_path` that does not exist.
  `--verify` checks all four against the running container, plus that a
  browser-shaped `401` still carries no `WWW-Authenticate`.

### Changed

- Every emoji used as a UI affordance is now a custom SVG icon drawn for this
  project (`frontend/src/components/icons/`): edit, delete, run history, clone,
  run now, Uptime Kuma monitor, back, external link and warning. No icon-library
  dependency was added.

  This is more than cosmetic. Emoji render as full-colour platform glyphs — a
  different picture on macOS, Windows, Android and Linux, usually misaligned
  with the text beside them — and they ignore `currentColor`. The delete button
  has carried `hover:text-danger` for some time and it did nothing, because
  there is no stroke on a 🗑 for the cascade to recolour. The replacements are
  stroke-only and inherit their colour, so hover and danger states work.

  The controls' accessible names are unchanged and now applied consistently
  across both layouts; each icon is `aria-hidden`/`focusable="false"` so screen
  readers announce the control once, and the 44px phone tap targets are intact.

- The app icon is now a three-bar "layers" mark instead of an arrow dropping
  into a storage tray. The old glyph read as *download*, which is the wrong
  verb for a backup tool; the three bars — brightest to dimmest — read as
  accumulated backup generations. The palette is the one shared across the
  wider project family (background `#26262b` → `#161619` → `#0c0c0f`, signal
  `#d3b6ff` → `#8b5cf6`) rather than the previous standalone flat violet. A
  test pins the shipped `frontend/public/favicon.svg` by digest and asserts the
  three distinct tones, so the mark cannot silently drift or be flattened.
- The logo drawn inside the app — in the header and on the sign-in page — is
  now the same three-bar mark as the icon. Both places previously inlined their
  own copy of the retired glyph, so replacing the served asset alone would have
  left the brand split in two. There is now a single `BrandMark` component and
  a test that fails if either placement inlines an SVG again, or if the retired
  artwork reappears anywhere under `frontend/src`. The `theme-color` used by
  mobile browser chrome moves from the old flat violet to the app's own page
  background, which is what actually sits behind it.

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

## [0.13.1] - 2026-09-03

### Fixed

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
