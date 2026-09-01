# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
import ipaddress
import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "PULLBACKUP_"
LEGACY_ENV_PREFIX = "PULLBACK_"

# Smallest auth-store cap the loader will accept.
#
# ADVISORY 3 on PR #20: below roughly two entries the throttle stops being a
# throttle. `_ExpiringStore._protected_cap()` reserves `max(1, cap // 2)` for
# completed lockouts, so at cap=1 one locked-out client occupies the entire
# store and every newcomer's entry is evicted at insert — measured as eight
# consecutive failed logins with retry_after 0 each time. Zero and negatives
# reach the same place through the store's own `max(1, ...)` floors, which are
# load-bearing (removing them raises KeyError from `_place`).
#
# 100 rather than 2. The store's documented weakness is that an attacker
# sustaining ~cap/2 distinct single-failure clients between a victim's attempts
# can keep that victim from ever locking out; measured, the victim survives at
# exactly `cap` interleaved flooders per attempt. A floor of 2 would be
# technically functional and practically defeated by a handful of addresses.
# 100 costs a few KiB, is 1/1000th of the default, and makes the cheapest
# throttle bypass cost 100 distinct source addresses sustained inside the
# 60-second window, past the trusted-proxy resolver.
MIN_AUTH_STORE_MAX_ENTRIES = 100


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
    # Optional. When empty the signing key is derived from the password, so an
    # upgrade needs no new configuration and changing the password invalidates
    # every previously issued session.
    session_secret: str = ""
    session_max_age_seconds: int = 7 * 24 * 60 * 60
    # Comma-separated IPs or CIDRs of reverse proxies whose X-Forwarded-For
    # header may be believed. Empty (the default) means no forwarding header is
    # trusted and the login throttle keys on the immediate peer, exactly as it
    # did before the setting existed.
    trusted_proxies: str = ""
    # Hard ceiling on each in-memory auth store (revoked sessions, failed-login
    # history). Both are keyed on values an unauthenticated caller can
    # influence, so a ceiling is what makes memory use predictable in a
    # memory-limited container.
    #
    # 100,000 entries is roughly 10-20 MiB per store, which is negligible next
    # to the container, while being far more concurrent sessions or distinct
    # attacking clients than a single-operator backup tool will ever see. It is
    # exposed rather than hard-coded because the safe value depends on the
    # deployment: an instance behind a proxy serving a large user base wants it
    # higher, and an operator who hits the cap should be able to raise it
    # rather than read the source to discover the number exists.
    #
    # Refused below MIN_AUTH_STORE_MAX_ENTRIES at startup: a store too small to
    # hold a lockout alongside a newcomer switches the throttle off rather than
    # merely constraining it. See _validate_auth_store_max_entries.
    auth_store_max_entries: int = 100_000

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
    def trusted_proxy_networks(self) -> list:
        return parse_trusted_proxies(self.trusted_proxies)

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


def parse_trusted_proxies(raw: str) -> list:
    """Parse the trusted-proxy allowlist, returning [] for anything malformed.

    Fails CLOSED. An empty list means no forwarding header is believed and the
    login throttle keys on the immediate peer — the behaviour that shipped
    before this setting existed. The opposite failure mode, reading a typo as
    "trust everyone", would convert a misconfiguration into a throttle bypass,
    so a single bad entry discards the whole list rather than the entry.

    A bare address is accepted and treated as a single-host network, because
    that is what an operator writes for a single proxy.
    """
    networks = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            return []
    return networks


def invalid_trusted_proxies(raw: str) -> list[str]:
    """Entries of the allowlist that are not an IP address or CIDR."""
    invalid = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            invalid.append(entry)
    return invalid


def _validate_trusted_proxies(raw: str) -> None:
    """Refuse to start on a malformed allowlist.

    The runtime resolver already fails closed, so a typo cannot widen the trust
    boundary. But silently ignoring a setting the operator believes is switched
    on is its own trap: the throttle would quietly stay shared across every
    client behind the proxy. Naming the bad entry at startup makes that loud.
    """
    invalid = invalid_trusted_proxies(raw)
    if invalid:
        raise ConfigurationError(
            f"{ENV_PREFIX}TRUSTED_PROXIES must be a comma-separated list of IP "
            f"addresses or CIDR ranges; could not parse: {', '.join(invalid)}"
        )


def _validate_auth_store_max_entries(cap: int) -> None:
    """Refuse a cap too small for the throttle to work.

    Rejected rather than clamped, matching this file's existing stance
    (`_validate_db_filename`, `_validate_trusted_proxies`) and
    `http_auth.require_valid_configuration`, which refuses a short HTTP Basic
    password rather than padding it. Clamping would run under a number the
    operator never chose while their configured value silently had no effect —
    the same silence this guard exists to remove, relocated.

    The error names both the variable and the minimum, so the fix does not
    require reading this source.
    """
    if cap < MIN_AUTH_STORE_MAX_ENTRIES:
        raise ConfigurationError(
            f"{ENV_PREFIX}AUTH_STORE_MAX_ENTRIES must be at least "
            f"{MIN_AUTH_STORE_MAX_ENTRIES} (got {cap}). Below that the store "
            f"cannot hold a locked-out client and a new one at the same time, "
            f"so the login throttle is switched off rather than merely "
            f"constrained. The default is 100000."
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
    _validate_trusted_proxies(settings.trusted_proxies)
    _validate_auth_store_max_entries(settings.auth_store_max_entries)

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
