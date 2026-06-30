# --- frontend build ---
# Using mirror.gcr.io to bypass docker.io anonymous pull rate limits.
FROM mirror.gcr.io/library/node:20-alpine AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# --- backend runtime ---
FROM mirror.gcr.io/library/python:3.13-slim AS runtime
# zfsutils-linux (the `zfs`/`zpool` CLI syncoid needs) lives in Debian `contrib`; enable it.
# syncoid ships in the `sanoid` package; mbuffer/pv/lzop are its transport helpers.
# procps gives `ps`, which syncoid shells out to — without it every run logs
# "Can't exec ps: No such file or directory at /usr/sbin/syncoid".
RUN sed -i 's/ main/ main contrib/' /etc/apt/sources.list /etc/apt/sources.list.d/*.sources 2>/dev/null; \
    apt-get update && apt-get install -y --no-install-recommends \
        rsync openssh-client ca-certificates tini tzdata procps \
        sanoid zfsutils-linux mbuffer pv lzop \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/pyproject.toml ./pyproject.toml
RUN pip install --no-cache-dir \
        "fastapi>=0.115" "uvicorn[standard]>=0.32" "sqlmodel>=0.0.22" \
        "apscheduler>=3.10" "croniter>=3.0" "pydantic-settings>=2.6" \
        "httpx>=0.27" "python-socketio[asyncio_client]>=5.11" \
        "sse-starlette>=2.1" "python-multipart>=0.0.12"

COPY backend/pullback ./pullback
COPY --from=frontend /fe/dist ./static

ENV PULLBACK_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8000

ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "pullback.main:app", "--host", "0.0.0.0", "--port", "8000"]
