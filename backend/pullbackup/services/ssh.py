# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
import socket
import subprocess
from pathlib import Path
from ..config import settings

DEFAULT_KEY_NAME = "id_ed25519"


def key_comment() -> str:
    """Comment baked into the generated public key.

    Derived from the running host rather than hardcoded: the public key is
    pasted into authorized_keys on every source machine, so a literal
    deployment hostname would be published to anyone running this.
    """
    host = socket.gethostname().strip() or "host"
    return f"pullbackup@{host}"


def ensure_default_key() -> Path:
    """Generate the default ed25519 keypair on first boot if missing. Returns private key path."""
    settings.ssh_dir.mkdir(parents=True, exist_ok=True)
    priv = settings.ssh_dir / DEFAULT_KEY_NAME
    if priv.exists():
        return priv
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", key_comment(), "-f", str(priv)],
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
