import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "PULLBACKUP_"
LEGACY_ENV_PREFIX = "PULLBACK_"


class ConfigurationError(RuntimeError):
    """Raised when the environment cannot be trusted to configure the app.

    This exists because pydantic-settings falls back to a field's default for
    any variable it cannot find. If the process reads ``PULLBACKUP_*`` while the
    host still exports ``PULLBACK_*``, nothing raises: ``dest_roots`` silently
    reverts to the built-in default and the HTTP Basic credentials blank out.
    That is a fail-OPEN reset of a security boundary, so startup must abort
    instead.
    """


def _dotenv_values(env_file: str | os.PathLike | None) -> dict:
    """Read a .env file using the SAME parser pydantic-settings uses.

    Review 323 (high): legacy detection originally inspected only ``os.environ``
    while ``Settings`` also loads ``.env``, so a legacy value living in the file
    bypassed the guard entirely.

    Review 324 (high): the first fix hand-rolled a ``name=value`` split, which
    pydantic-settings' own parser does not match. ``export PULLBACK_DEST_ROOTS=x``
    is valid dotenv — python-dotenv strips the ``export`` keyword — but the naive
    split recorded the name as ``"export PULLBACK_DEST_ROOTS"`` and never flagged
    it. Verified against the real library: pydantic-settings read the value while
    the hand-rolled detector returned nothing.

    Delegating to ``dotenv_values`` removes the whole class of divergence rather
    than patching one syntax at a time: whatever the settings loader honours, the
    guard sees.
    """
    if not env_file:
        return {}
    path = Path(env_file)
    if not path.is_file():
        return {}
    return {name: value for name, value in dotenv_values(path).items() if name}


def orphaned_legacy_variables(environ, env_file=None) -> list[str]:
    """Legacy names with no ``PULLBACKUP_`` counterpart.

    Checked per variable, not globally. The container image sets
    ``PULLBACKUP_DATA_DIR`` itself, so *some* new-prefix variable always exists
    at runtime; a global "any new var present" test would therefore never fire
    inside the container, and a stack exporting only ``PULLBACK_DEST_ROOTS``
    would silently revert the rsync allowlist to its default. Each legacy name
    must be matched individually.

    Both the process environment and ``.env`` are considered, because settings
    are loaded from both. A legacy name in either place needs a new-prefix
    counterpart in either place.
    """
    combined = dict(_dotenv_values(env_file))
    combined.update(environ)
    return sorted(
        name
        for name in combined
        if name.startswith(LEGACY_ENV_PREFIX)
        and not name.startswith(ENV_PREFIX)
        and ENV_PREFIX + name[len(LEGACY_ENV_PREFIX):] not in combined
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix=ENV_PREFIX, env_file=".env", extra="ignore")

    data_dir: Path = Path("/data")
    # The deployed volume holds pullback.db. Renaming the file is a separate,
    # deliberate migration: changing this default would make SQLModel create a
    # new empty database and silently orphan every task, source, run, and log.
    db_filename: str = "pullback.db"
    dest_roots: str = "/mnt/dest/backups"
    zfs_dest_roots: str = ""
    max_concurrent_runs: int = 1
    log_retention_runs: int = 200
    http_basic_username: str = ""
    http_basic_password: str = ""

    matrix_homeserver: str = ""
    matrix_token: str = ""
    matrix_room_id: str = ""

    uptime_kuma_url: str = ""
    uptime_kuma_username: str = ""
    uptime_kuma_password: str = ""
    kuma_group_id: int | None = None  # parent group for created push monitors

    @property
    def dest_roots_list(self) -> list[Path]:
        return [Path(p.strip()) for p in self.dest_roots.split(",") if p.strip()]

    @property
    def zfs_dest_roots_list(self) -> list[str]:
        return [
            normalized
            for value in self.zfs_dest_roots.split(",")
            if (normalized := value.strip().strip("/"))
        ]

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
        return self.data_dir / self.db_filename

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


# Matrix/Kuma variables are intentionally NOT prefixed, matching common usage,
# so the rename must not sweep them up.
class _MatrixKumaSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    MATRIX_HOMESERVER: str = ""
    MATRIX_TOKEN: str = ""
    MATRIX_ROOM_ID: str = ""
    UPTIME_KUMA_URL: str = ""
    UPTIME_KUMA_USERNAME: str = ""
    UPTIME_KUMA_PASSWORD: str = ""


def _validate_db_filename(name: str) -> None:
    """The override names a file *inside* the data directory.

    Accepting a path would let the database escape the mounted volume and be
    written somewhere that is not persisted or not backed up.
    """
    if not name:
        raise ConfigurationError(f"{ENV_PREFIX}DB_FILENAME must not be empty")
    if name != Path(name).name or os.sep in name or (os.altsep and os.altsep in name):
        raise ConfigurationError(
            f"{ENV_PREFIX}DB_FILENAME must be a bare filename inside the data "
            f"directory, not a path (got {name!r})"
        )


ENV_FILE = ".env"


def load_settings(environ=None, env_file=ENV_FILE) -> Settings:
    """Build Settings, refusing an environment with orphaned legacy names.

    Passing both prefixes is allowed and is what makes the deployment cutover
    possible: the stack exports old and new names together, the new image reads
    the new ones, and a rollback to the previous image still finds the old ones.
    """
    environ = os.environ if environ is None else environ

    # Adopt orphaned legacy names instead of refusing.
    #
    # The hazard this tranche exists to prevent is a SILENT fall back to
    # built-in defaults — a widened rsync allowlist and blank HTTP Basic
    # credentials — when the process reads PULLBACKUP_* but the deployment
    # still exports PULLBACK_*. Adoption removes that hazard completely: the
    # operator's configured value is used, and nothing reverts to a default.
    #
    # Refusing outright also proved to be a deployment deadlock. The CI
    # workflow runs under `pull_request_target`, so Gitea executes the BASE
    # branch's tests.yml, which still exports the legacy names. A build that
    # refuses them can never pass its own gate to get merged, and the same
    # ordering trap applies to the live stack: the new image would have to be
    # deployed before its environment could be updated.
    #
    # New-prefix values always win, so a migrated environment is unaffected and
    # the documented cutover is unchanged.
    adopted = {}
    for name in orphaned_legacy_variables(environ, env_file):
        source = environ if name in environ else _dotenv_values(env_file)
        adopted[ENV_PREFIX + name[len(LEGACY_ENV_PREFIX):]] = source[name]
    if adopted:
        logging.getLogger(__name__).warning(
            "Adopting retired %s* variables with no %s counterpart: %s. "
            "Rename them to %s* — this compatibility shim is removed once the "
            "deployment no longer exports the legacy names.",
            LEGACY_ENV_PREFIX, ENV_PREFIX,
            ", ".join(sorted(orphaned_legacy_variables(environ, env_file))),
            ENV_PREFIX,
        )

    merged = {**adopted, **{k: v for k, v in environ.items() if k.startswith(ENV_PREFIX)}}

    # Review 324 (medium/correctness): the supplied env_file was used for legacy
    # validation but never passed to Settings, so model_config's default ".env"
    # was loaded instead and a caller's custom file was silently ignored. Pass it
    # through to both models so validation and loading read the same source.
    settings = Settings(_env_file=env_file, **_explicit(merged))
    _validate_db_filename(settings.db_filename)

    mk = _MatrixKumaSettings(_env_file=env_file, **_matrix_kuma(environ))
    settings.matrix_homeserver = settings.matrix_homeserver or mk.MATRIX_HOMESERVER
    settings.matrix_token = settings.matrix_token or mk.MATRIX_TOKEN
    settings.matrix_room_id = settings.matrix_room_id or mk.MATRIX_ROOM_ID
    settings.uptime_kuma_url = settings.uptime_kuma_url or mk.UPTIME_KUMA_URL
    settings.uptime_kuma_username = settings.uptime_kuma_username or mk.UPTIME_KUMA_USERNAME
    settings.uptime_kuma_password = settings.uptime_kuma_password or mk.UPTIME_KUMA_PASSWORD
    return settings


def _explicit(environ) -> dict:
    """Map PULLBACKUP_* names onto Settings fields.

    Done explicitly rather than relying on process env so the loader can be
    called with a dict in tests and behave identically.
    """
    values = {}
    for name, value in environ.items():
        if name.startswith(ENV_PREFIX):
            field = name[len(ENV_PREFIX):].lower()
            if field in Settings.model_fields:
                values[field] = value
    return values


def _matrix_kuma(environ) -> dict:
    fields = _MatrixKumaSettings.model_fields
    return {name: value for name, value in environ.items() if name in fields}


settings = load_settings()
