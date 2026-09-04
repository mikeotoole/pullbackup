<!--
Pullbackup is developed on a private Gitea instance and mirrored to GitHub, so
GitHub pull requests are not merged from here. Please open an issue describing
the change and it will be picked up upstream. See CONTRIBUTING.md.
-->

## What this changes

<!-- One or two sentences. What behaviour is different afterwards? -->

## Why

<!-- The problem being solved. A bug report or issue number is ideal. -->

## Verification

<!--
How do you know it works? Any behaviour change should ship with a test that
fails without the fix — see CONTRIBUTING.md. Paste the test failing against the
unfixed code and passing afterwards; that is the difference between a test and
a test that would have caught the bug.
-->

- [ ] Backend gates pass (`uv run --project backend --locked --with pytest --with pytest-asyncio pytest -q`)
- [ ] Frontend gates pass (`npm test --prefix frontend`) if the UI changed
- [ ] A test covers the change and fails without it
- [ ] `CHANGELOG.md` updated under `## [Unreleased]` — never inside a released section
