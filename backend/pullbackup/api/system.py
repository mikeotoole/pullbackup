from fastapi import APIRouter
from ..config import settings
from ..services import fs, ssh
from .. import __version__

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/health")
def health():
    return {"ok": True, "version": __version__}


@router.get("/info")
def info():
    return {
        "version": __version__,
        "dest_roots": [str(p) for p in settings.dest_roots_list],
        "tz": settings.tz_name,
        "matrix_enabled": settings.matrix_enabled,
        "kuma_enabled": settings.kuma_enabled,
        "kuma_url": settings.uptime_kuma_url,
    }


@router.get("/browse")
def browse(path: str | None = None):
    try:
        return fs.browse(path)
    except fs.PathNotAllowed as e:
        return {"error": str(e), "entries": []}


@router.get("/ssh-pubkey")
def ssh_pubkey():
    priv = ssh.ensure_default_key()
    return {"private_path": str(priv), "public_key": ssh.read_pubkey(priv)}
