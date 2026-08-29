# Pullbackup

Pull-only rsync task manager. Web UI like TrueNAS's "Rsync Tasks" but inverted: the puller holds all credentials, sources never need anything from us.

**Stack:** FastAPI (Python 3.13) · SQLite · APScheduler · React 18 + Vite + Tailwind + shadcn/ui

## Concepts

- **Source** — a remote host you pull from (name, `user@host:port`, SSH key). Defined once, reused across tasks.
- **Task** — a scheduled `rsync` pull from a source path → local path, with the usual rsync knobs (archive, compress, delete, bwlimit, excludes …).
- **Run** — one execution of a task. Records exit code, byte/file counts, link to streaming log.

## Notifications (per task)

- **Matrix** — checkbox; failures (and optionally successes) post to a configured room.
- **Uptime Kuma** — checkbox; Pullbackup creates a Kuma push monitor for the task and updates the heartbeat interval whenever the cron changes. Disabled tasks → paused monitor. Deleted tasks → deleted monitor.

## Deployment

Built as a single container image and deployed as a Docker Compose stack. Bind-mount the destination roots you want exposed; Pullbackup's filesystem browser is allowlisted to those roots. All UI and API routes except `/api/system/health` require HTTP Basic authentication.

```
volumes:
  - ${DOCKER_VOLUMES}/pullback/data:/data        # sqlite, logs, ssh keys
  - /mnt/user/backups:/mnt/dest/backups          # destination root
  - /mnt/user/media:/mnt/dest/media              # add more roots as needed
```

## Local dev

```bash
# Backend
cd backend
uv sync
export PULLBACKUP_HTTP_BASIC_USERNAME=pullbackup-dev
export PULLBACKUP_HTTP_BASIC_PASSWORD=$(openssl rand -base64 32)
uv run uvicorn pullbackup.main:app --reload --port 8000

# Frontend (separate terminal)
cd frontend
npm install
npm run dev   # Vite proxies /api → :8000
```

> **Note on naming.** The product is Pullbackup, and so are the Python package
> and the `PULLBACKUP_` environment prefix. Two identifiers still carry the
> shorter former name and are renamed separately: the host data directory
> (`${DOCKER_VOLUMES}/pullback/data`) and the SQLite file inside it
> (`pullback.db`). Do not rename the host data directory or the database on
> your own — they hold the database, run logs, and SSH keys, and pointing the
> app at a new path or filename starts it against an empty directory.
> `PULLBACKUP_DB_FILENAME` exists so that rename can be done deliberately,
> with a backup, when you choose.

## Env

See `.env.example`. Deployment requires both `PULLBACKUP_HTTP_BASIC_USERNAME` and `PULLBACKUP_HTTP_BASIC_PASSWORD`; the password must contain at least 32 characters. Keep the password in the deployment secret store, not in source control.

This build reads the `PULLBACKUP_` prefix. If it finds a retired `PULLBACK_`
name with no `PULLBACKUP_` counterpart it **adopts that value and logs a
warning**, rather than falling back to a built-in default — which would widen
the rsync destination allowlist and blank the HTTP Basic credentials. Both
prefixes may be set at once during a deployment cutover; the new names win.
The adoption shim is temporary: rename your variables and it goes quiet.

`PULLBACKUP_ZFS_DEST_ROOTS` is a comma-separated allowlist for Syncoid destinations and ZFS pruning. Each Syncoid destination must be a descendant of one configured dataset root (for example, `cache/docker_remote/eel` beneath `cache/docker_remote`). An empty allowlist disables Syncoid and pruning operations. `PULLBACKUP_DEST_ROOTS` remains the separate filesystem allowlist for rsync destinations and the browser.

Matrix and Uptime Kuma credentials are optional; omit their values to disable those integrations.
