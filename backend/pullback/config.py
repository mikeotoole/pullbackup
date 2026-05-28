import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PULLBACK_", env_file=".env", extra="ignore")

    data_dir: Path = Path("/data")
    dest_roots: str = "/mnt/dest/backups"
    max_concurrent_runs: int = 4
    log_retention_runs: int = 200

    matrix_homeserver: str = ""
    matrix_token: str = ""
    matrix_room_id: str = ""

    uptime_kuma_url: str = ""
    uptime_kuma_username: str = ""
    uptime_kuma_password: str = ""

    @property
    def dest_roots_list(self) -> list[Path]:
        return [Path(p.strip()) for p in self.dest_roots.split(",") if p.strip()]

    @property
    def tz_name(self) -> str:
        return os.environ.get("TZ", "UTC")

    @property
    def tzinfo(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.tz_name)
        except ZoneInfoNotFoundError:
            return ZoneInfo("UTC")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "pullback.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def ssh_dir(self) -> Path:
        return self.data_dir / "ssh"

    @property
    def matrix_enabled(self) -> bool:
        return bool(self.matrix_homeserver and self.matrix_token and self.matrix_room_id)

    @property
    def kuma_enabled(self) -> bool:
        return bool(self.uptime_kuma_url and self.uptime_kuma_username and self.uptime_kuma_password)


# Settings are read from env. PULLBACK_-prefixed keys override Matrix/Kuma
# blocks too — note those are intentionally NOT prefixed to match common usage.
class _MatrixKumaSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    MATRIX_HOMESERVER: str = ""
    MATRIX_TOKEN: str = ""
    MATRIX_ROOM_ID: str = ""
    UPTIME_KUMA_URL: str = ""
    UPTIME_KUMA_USERNAME: str = ""
    UPTIME_KUMA_PASSWORD: str = ""


def _load() -> Settings:
    s = Settings()
    mk = _MatrixKumaSettings()
    s.matrix_homeserver = s.matrix_homeserver or mk.MATRIX_HOMESERVER
    s.matrix_token = s.matrix_token or mk.MATRIX_TOKEN
    s.matrix_room_id = s.matrix_room_id or mk.MATRIX_ROOM_ID
    s.uptime_kuma_url = s.uptime_kuma_url or mk.UPTIME_KUMA_URL
    s.uptime_kuma_username = s.uptime_kuma_username or mk.UPTIME_KUMA_USERNAME
    s.uptime_kuma_password = s.uptime_kuma_password or mk.UPTIME_KUMA_PASSWORD
    return s


settings = _load()
