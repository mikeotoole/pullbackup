# Contributing

Thanks for looking. Pullbackup is a small, opinionated tool: a pull-only
backup task manager where the puller holds every credential and source hosts
need nothing installed. Changes that preserve that shape are much easier to
land than ones that broaden it.

## Before you start

For anything beyond a bug fix, open an issue first. It is not a formality —
it avoids you building something that does not fit the pull-only model, and
there are only so many features a tool like this can absorb before it becomes
a worse version of something else.

## Development setup

```bash
# Backend (Python 3.13, uv)
cd backend
uv sync
export PULLBACKUP_HTTP_BASIC_USERNAME=dev
export PULLBACKUP_HTTP_BASIC_PASSWORD=$(openssl rand -base64 32)
uv run uvicorn pullbackup.main:app --reload --port 8000

# Frontend (separate terminal)
cd frontend
npm ci
npm run dev   # Vite proxies /api -> :8000
```

## Gates

Everything below must pass before a change is reviewable:

```bash
# Backend tests
uv run --project backend --with pytest --with pytest-asyncio pytest -q

# Frontend tests and build
npm test --prefix frontend
npm run build --prefix frontend
```

## Tests

Write the test first and **watch it fail** before you write the fix. A test
that has never failed has not been shown to test anything — this project has
shipped more than one guard that passed against broken code because nobody
checked the red.

Two specific habits, both learned the hard way here:

- **Test behaviour at the boundary that matters.** Config guards that only
  inspected `os.environ` missed the same problem arriving via `.env`, and a
  hand-rolled `.env` parser missed `export KEY=value`, which the real loader
  accepts. Prefer deferring to the library over reimplementing its parsing.
- **Never weaken a guard to make it pass.** If a test is in the way, either
  the code is wrong or the test encodes the wrong rule — fix whichever it is,
  and say so. Broadening an allowlist until a check goes quiet is how a suite
  becomes decoration.

## Conventions

- **UI casing**: general chrome is lower case ("add task", "remote path");
  run/status tokens are UPPER CASE. Acronyms and proper nouns keep their
  casing (SSH, ZFS, UTC, Matrix, Uptime Kuma).
- **Mobile**: lists render as cards below the `sm` breakpoint and as tables
  above it. Do not reintroduce a horizontal scroller as the phone experience.
- **Destinations are allowlisted.** Any code path that writes to a
  destination must resolve it against the configured roots. If you are
  touching `services/fs.py`, assume a reviewer will look hard at TOCTOU.
- Commits explain *why*, not just what.

## What will get pushed back

- Widening the destination allowlist, or adding a way to bypass it.
- Anything that makes source hosts run or install Pullbackup components. The
  whole point is that they do not.
- Logging credentials, tokens, or full environment dumps.
- Rendering user-controlled strings as HTML.
