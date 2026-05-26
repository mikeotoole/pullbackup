import subprocess
from pathlib import Path
from ..config import settings

DEFAULT_KEY_NAME = "id_ed25519"


def ensure_default_key() -> Path:
    """Generate the default ed25519 keypair on first boot if missing. Returns private key path."""
    settings.ssh_dir.mkdir(parents=True, exist_ok=True)
    priv = settings.ssh_dir / DEFAULT_KEY_NAME
    if priv.exists():
        return priv
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "pullback@seal", "-f", str(priv)],
        check=True,
        capture_output=True,
    )
    priv.chmod(0o600)
    pub = priv.with_suffix(".pub")
    pub.chmod(0o644)
    return priv


def read_pubkey(key_path: Path) -> str:
    pub = Path(str(key_path) + ".pub")
    return pub.read_text().strip() if pub.exists() else ""
