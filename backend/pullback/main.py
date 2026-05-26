import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from .db import init_db
from .services import scheduler, ssh
from .api import sources, tasks, runs, system

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ssh.ensure_default_key()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown()


app = FastAPI(title="pullback", lifespan=lifespan)
app.include_router(system.router)
app.include_router(sources.router)
app.include_router(tasks.router)
app.include_router(runs.router)

# serve built React frontend if present
_static_dir = Path(__file__).resolve().parent.parent / "static"
if _static_dir.exists():
    app.mount("/assets", StaticFiles(directory=_static_dir / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        index = _static_dir / "index.html"
        if index.exists():
            return FileResponse(index)
        return {"error": "frontend not built"}
