# pullback

Pull-only rsync task manager. Web UI like TrueNAS's "Rsync Tasks" but inverted: the puller (seal) holds all credentials, sources never need anything from us.

**Stack:** FastAPI (Python 3.13) · SQLite · APScheduler · React 18 + Vite + Tailwind + shadcn/ui

## Concepts

- **Source** — a remote host you pull from (name, `user@host:port`, SSH key). Defined once, reused across tasks.
- **Task** — a scheduled `rsync` pull from a source path → local path, with the usual rsync knobs (archive, compress, delete, bwlimit, excludes …).
- **Run** — one execution of a task. Records exit code, byte/file counts, link to streaming log.

## Notifications (per task)

- **Matrix** — checkbox; failures (and optionally successes) post to the homelab room.
- **Uptime Kuma** — checkbox; pullback creates a Kuma push monitor for the task and updates the heartbeat interval whenever the cron changes. Disabled tasks → paused monitor. Deleted tasks → deleted monitor.

## Deployment

Built as a single container image (`pullback:<tag>`) and deployed as a Komodo stack pinned to **seal**. Traefik-routed at `https://pullback.seal.lagoon.cloud`. Bind-mount the destination roots you want exposed; pullback's filesystem browser is allowlisted to those roots.

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
uv run uvicorn pullback.main:app --reload --port 8000

# Frontend (separate terminal)
cd frontend
npm install
npm run dev   # Vite proxies /api → :8000
```

## Env

See `.env.example`. Only the optional Matrix/Kuma blocks are sensitive; the rest is paths and tuning.
