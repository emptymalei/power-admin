"""Settings from a YAML config file (with reusable profiles), environment variables and CLI flags.

Precedence, lowest to highest:
    built-in defaults < config file (top level, then the chosen profile) < POWER_ADMIN_* env vars < CLI flags

Example config (power-admin.yaml):

    tenant_id: contoso.onmicrosoft.com
    output: C:/power-admin-data
    profiles:
      monthly:
        output: s3://my-bucket/power-platform
        aws_profile: admin
        include: [dataverse, audit]
      ci:
        extends: monthly
        auth: client-secret
        client_id: 00000000-0000-0000-0000-000000000000
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, fields
from datetime import date
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(os.environ.get("POWER_ADMIN_HOME", Path.home() / ".power-admin"))
DEFAULT_CONFIG_FILE = CONFIG_DIR / "config.yaml"
TOKEN_CACHE_FILE = CONFIG_DIR / "msal_token_cache.json"

POWER_PLATFORM_API = "https://api.powerplatform.com"
GRAPH_API = "https://graph.microsoft.com"
PP_SCOPE = f"{POWER_PLATFORM_API}/.default"
GRAPH_SCOPE = f"{GRAPH_API}/.default"

AUTH_MODES = ("interactive", "device-code", "client-secret", "certificate", "token")

DEFAULT_AUDIT_RECORD_TYPES = [
    "powerAppsApp",
    "powerAppsPlan",
    "powerAppsResource",
    "microsoftFlow",
    "powerPlatformAdminDlp",
    "powerPlatformAdminEnvironment",
    "powerPlatformServiceActivity",
    "powerPagesSite",
    "hostedRpa",
]


@dataclass
class Settings:
    # Sign-in
    tenant_id: str | None = None
    client_id: str | None = None
    auth: str = "interactive"
    client_secret: str | None = None  # never written to the config file
    certificate_path: str | None = None
    certificate_thumbprint: str | None = None
    # Output
    output: str = "./power-admin-output"
    aws_profile: str | None = None
    s3_endpoint_url: str | None = None
    formats: list[str] = field(default_factory=lambda: ["jsonl", "csv"])
    # What to collect
    only: list[str] = field(default_factory=list)  # run exactly these datasets
    include: list[str] = field(default_factory=list)  # add opt-in datasets to the defaults
    skip: list[str] = field(default_factory=list)
    date_from: str | None = None  # YYYY-MM-DD, default: 1st of last month
    date_to: str | None = None  # YYYY-MM-DD, default: today
    # Collector options
    credit_entitlements: list[str] = field(default_factory=lambda: ["MCSMessages"])
    credit_granularity: str = "month"  # month | day | none (one total for the whole range)
    transcript_content: bool = False  # also download the conversation text of each agent session
    environments: list[str] = field(default_factory=list)  # limit per-environment collectors to these IDs
    max_flows_per_environment: int = 200
    audit_record_types: list[str] = field(default_factory=lambda: list(DEFAULT_AUDIT_RECORD_TYPES))
    audit_timeout_minutes: int = 30

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id or 'organizations'}"


FIELD_NAMES = {f.name for f in fields(Settings)}
LIST_FIELDS = {f.name for f in fields(Settings) if f.type.startswith("list")}
INT_FIELDS = {f.name for f in fields(Settings) if f.type == "int"}
BOOL_FIELDS = {f.name for f in fields(Settings) if f.type == "bool"}
SECRET_FIELDS = {"client_secret"}
# YAML-friendly aliases
ALIASES = {
    "from": "date_from",
    "to": "date_to",
    "auth_mode": "auth",
    "format": "formats",
    "credit_entitlement": "credit_entitlements",
}

ENV_PREFIX = "POWER_ADMIN_"


class ConfigError(ValueError):
    pass


def _normalize(data: dict[str, Any], source: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for raw_key, value in (data or {}).items():
        key = ALIASES.get(raw_key, raw_key).replace("-", "_")
        if key in ("profiles", "extends"):
            continue
        if key not in FIELD_NAMES:
            raise ConfigError(f"Unknown setting {raw_key!r} in {source}")
        if isinstance(value, date):  # YAML turns 2026-09-01 into a date
            value = value.isoformat()
        if key in LIST_FIELDS and isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]
        if key in INT_FIELDS and value is not None:
            value = int(value)
        if key in BOOL_FIELDS and isinstance(value, str):
            value = value.strip().lower() in ("1", "true", "yes", "on")
        out[key] = value
    return out


def config_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    if os.environ.get(ENV_PREFIX + "CONFIG"):
        return Path(os.environ[ENV_PREFIX + "CONFIG"]).expanduser()
    return DEFAULT_CONFIG_FILE


def read_config_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return data


def _resolve_profile(raw: dict[str, Any], name: str, path: Path, seen: tuple[str, ...] = ()) -> dict[str, Any]:
    profiles = raw.get("profiles") or {}
    if name not in profiles:
        known = ", ".join(profiles) or "none defined"
        raise ConfigError(f"Profile {name!r} not found in {path} (profiles: {known})")
    if name in seen:
        raise ConfigError(f"Profile inheritance loop: {' -> '.join(seen + (name,))}")
    profile = profiles[name] or {}
    base: dict[str, Any] = {}
    if profile.get("extends"):
        base = _resolve_profile(raw, profile["extends"], path, seen + (name,))
    return {**base, **_normalize(profile, f"{path} profile {name!r}")}


def _from_env() -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name in FIELD_NAMES:
        env = ENV_PREFIX + name.upper()
        if os.environ.get(env):
            values[name] = os.environ[env]
    return _normalize(values, "environment variables")


def load_settings(config: str | None = None, profile: str | None = None, **overrides: Any) -> Settings:
    path = config_path(config)
    if config and not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    raw = read_config_file(path)
    values = _normalize(raw, str(path))
    profile = profile or os.environ.get(ENV_PREFIX + "PROFILE")
    if profile:
        values.update(_resolve_profile(raw, profile, path))
    values.update(_from_env())
    values.update(_normalize({k: v for k, v in overrides.items() if v not in (None, [], ())}, "command line"))
    settings = Settings(**values)
    if settings.auth not in AUTH_MODES:
        raise ConfigError(f"Unknown auth mode {settings.auth!r}; choose one of {', '.join(AUTH_MODES)}")
    if settings.credit_granularity not in ("month", "day", "none"):
        raise ConfigError("credit_granularity must be month, day or none")
    bad = [f for f in settings.formats if f not in ("jsonl", "csv")]
    if bad:
        raise ConfigError(f"Unknown format(s): {', '.join(bad)} (use jsonl and/or csv)")
    return settings


def save_settings(settings: Settings, path: Path | None = None, profile: str | None = None) -> Path:
    """Write non-default, non-secret settings to the config file (top level or a profile)."""
    path = path or DEFAULT_CONFIG_FILE
    defaults = asdict(Settings())
    changed = {
        k: v for k, v in asdict(settings).items() if k not in SECRET_FIELDS and v is not None and v != defaults.get(k)
    }
    raw = read_config_file(path)
    if profile:
        raw.setdefault("profiles", {}).setdefault(profile, {}).update(changed)
    else:
        raw.update(changed)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path
