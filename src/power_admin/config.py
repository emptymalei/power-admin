"""Settings loaded from flags, environment variables and ~/.power-admin/config.json."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("POWER_ADMIN_HOME", Path.home() / ".power-admin"))
CONFIG_FILE = CONFIG_DIR / "config.json"
TOKEN_CACHE_FILE = CONFIG_DIR / "msal_token_cache.json"

POWER_PLATFORM_API = "https://api.powerplatform.com"
GRAPH_API = "https://graph.microsoft.com"
PP_SCOPE = f"{POWER_PLATFORM_API}/.default"
GRAPH_SCOPE = f"{GRAPH_API}/.default"

AUTH_MODES = ("interactive", "device-code", "client-secret", "certificate", "token")


@dataclass
class Settings:
    tenant_id: str | None = None
    client_id: str | None = None
    auth_mode: str = "interactive"
    # Service principal options. The secret is never written to the config file.
    client_secret: str | None = None
    certificate_path: str | None = None
    certificate_thumbprint: str | None = None
    # Default output location: a local folder or s3://bucket/prefix
    output: str = "./power-admin-output"

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id or 'organizations'}"


_ENV = {
    "tenant_id": "POWER_ADMIN_TENANT_ID",
    "client_id": "POWER_ADMIN_CLIENT_ID",
    "auth_mode": "POWER_ADMIN_AUTH_MODE",
    "client_secret": "POWER_ADMIN_CLIENT_SECRET",
    "certificate_path": "POWER_ADMIN_CERTIFICATE_PATH",
    "certificate_thumbprint": "POWER_ADMIN_CERTIFICATE_THUMBPRINT",
    "output": "POWER_ADMIN_OUTPUT",
}

_SECRET_FIELDS = {"client_secret"}


def load_settings(**overrides: str | None) -> Settings:
    """Config file < environment variables < explicit overrides (CLI flags)."""
    values: dict[str, str] = {}
    if CONFIG_FILE.exists():
        values.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    for name, env in _ENV.items():
        if os.environ.get(env):
            values[name] = os.environ[env]
    values.update({k: v for k, v in overrides.items() if v is not None})
    known = {f.name for f in fields(Settings)}
    settings = Settings(**{k: v for k, v in values.items() if k in known})
    if settings.auth_mode not in AUTH_MODES:
        raise ValueError(f"Unknown auth mode {settings.auth_mode!r}; choose one of {', '.join(AUTH_MODES)}")
    return settings


def save_settings(settings: Settings) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = {k: v for k, v in asdict(settings).items() if v is not None and k not in _SECRET_FIELDS}
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return CONFIG_FILE
