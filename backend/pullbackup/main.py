# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import auth, runs, sources, system, tasks
from .db import init_db
from .http_auth import HttpBasicAuthMiddleware, require_valid_configuration
from .services import runner, scheduler, ssh

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    require_valid_configuration()
    init_db()
    runner.reconcile_stale_runs()
    ssh.ensure_default_key()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown()
        await runner.shutdown_execution_tasks()


app = FastAPI(title="pullback", lifespan=lifespan)
app.add_middleware(HttpBasicAuthMiddleware)
app.include_router(system.router)
app.include_router(auth.router)
app.include_router(sources.router)
app.include_router(tasks.router)
app.include_router(runs.router)

# serve built React frontend if present
_static_dir = Path(__file__).resolve().parent.parent / "static"
if _static_dir.exists():
    app.mount("/assets", StaticFiles(directory=_static_dir / "assets"), name="assets")

    _static_root = _static_dir.resolve()

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        # Serve a real file at the web root (favicon.svg, robots.txt, manifest…)
        # before falling back to the SPA index. Without this the catch-all returns
        # index.html for /favicon.svg, so the icon never loads.
        if full_path:
            try:
                candidate = (_static_dir / full_path).resolve()
                if candidate.is_file() and _static_root in candidate.parents:
                    return FileResponse(candidate)
            except (OSError, ValueError):
                pass
        index = _static_dir / "index.html"
        if index.exists():
            return FileResponse(index)
        return {"error": "frontend not built"}
