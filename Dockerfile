# --- frontend build ---
# Using mirror.gcr.io to bypass docker.io anonymous pull rate limits.
FROM mirror.gcr.io/library/node:20-alpine AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- backend runtime ---
FROM mirror.gcr.io/library/python:3.13-slim AS runtime
# zfsutils-linux (the `zfs`/`zpool` CLI syncoid needs) lives in Debian `contrib`; enable it.
# syncoid ships in the `sanoid` package; mbuffer/pv/lzop are its transport helpers.
# procps gives `ps`, which syncoid shells out to — without it every run logs
# "Can't exec ps: No such file or directory at /usr/sbin/syncoid".
COPY scripts/patch_syncoid_control_master.py /tmp/patch_syncoid_control_master.py
RUN sed -i 's/ main/ main contrib/' /etc/apt/sources.list /etc/apt/sources.list.d/*.sources 2>/dev/null; \
    apt-get update && apt-get install -y --no-install-recommends \
        rsync openssh-client ca-certificates tini tzdata procps \
        sanoid zfsutils-linux mbuffer pv lzop \
    && python3 /tmp/patch_syncoid_control_master.py /usr/sbin/syncoid \
    && perl -c /usr/sbin/syncoid \
    && rm /tmp/patch_syncoid_control_master.py \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Install from the packaging metadata rather than a hand-maintained list.
# The duplicated list previously drifted: config.py gained a python-dotenv
# import that pyproject.toml declared but this file did not install, so the
# image raised ModuleNotFoundError at startup. Installing the project itself
# makes that class of drift impossible.
COPY backend/pyproject.toml ./pyproject.toml
# AGPL-3.0 sections 4 and 6 require that whoever conveys the work — including
# object code — give recipients a copy of the licence. The image declares
# License-Expression: AGPL-3.0-or-later, so it must carry the text too.
COPY LICENSE ./LICENSE
COPY backend/pullbackup ./pullbackup
RUN pip install --no-cache-dir .

COPY --from=frontend /fe/dist ./static

ENV PULLBACKUP_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8000

ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "pullbackup.main:app", "--host", "0.0.0.0", "--port", "8000"]
