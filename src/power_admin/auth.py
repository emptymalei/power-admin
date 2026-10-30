"""Token acquisition for the Power Platform API and Microsoft Graph.

Supported modes:
  interactive    browser sign-in as you (delegated). Default.
  device-code    sign in on any device with a code (delegated). Good for SSH or locked-down shells.
  client-secret  app-only service principal with a client secret.
  certificate    app-only service principal with a certificate (PEM private key + thumbprint).
  token          bring your own access tokens via POWER_ADMIN_PP_TOKEN / POWER_ADMIN_GRAPH_TOKEN.

Delegated modes cache tokens in ~/.power-admin, so you sign in once and later runs are silent
until the refresh token expires. Note the Power Platform inventory API only accepts
delegated (user) tokens; app-only modes get 403 there.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import msal

from .config import GRAPH_SCOPE, PP_SCOPE, TOKEN_CACHE_FILE, Settings

DELEGATED_MODES = {"interactive", "device-code"}

# Microsoft's public client ID for Azure PowerShell (the one Connect-AzAccount uses). It is
# pre-authorized for the Power Platform API and Graph, so delegated sign-in works without
# registering your own Entra app. Override with --client-id if your tenant blocks it.
DEFAULT_PUBLIC_CLIENT_ID = "1950a258-227b-4e31-a9cf-717495945fc2"
APP_ONLY_MODES = {"client-secret", "certificate"}

_TOKEN_ENV = {PP_SCOPE: "POWER_ADMIN_PP_TOKEN", GRAPH_SCOPE: "POWER_ADMIN_GRAPH_TOKEN"}


class AuthError(RuntimeError):
    pass


def _load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if TOKEN_CACHE_FILE.exists():
        cache.deserialize(TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
    return cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        TOKEN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_CACHE_FILE.write_text(cache.serialize(), encoding="utf-8")
        try:
            os.chmod(TOKEN_CACHE_FILE, 0o600)
        except OSError:
            pass


class TokenProvider:
    """Returns bearer tokens per scope, signing in on first use."""

    def __init__(self, settings: Settings, echo: Callable[[str], None] = print):
        self.settings = settings
        self.echo = echo
        self._cache = _load_cache()
        self._app: msal.ClientApplication | None = None
        self._memo: dict[str, str] = {}

    @property
    def mode(self) -> str:
        return self.settings.auth_mode

    @property
    def is_delegated(self) -> bool:
        return self.mode in DELEGATED_MODES or self.mode == "token"

    def _require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self.settings, n)]
        if missing:
            flags = ", ".join("--" + n.replace("_", "-") for n in missing)
            raise AuthError(f"Auth mode {self.mode!r} needs {flags} (or the matching POWER_ADMIN_* env vars)")

    def _client(self) -> msal.ClientApplication:
        if self._app is not None:
            return self._app
        s = self.settings
        if self.mode in DELEGATED_MODES:
            self._app = msal.PublicClientApplication(
                s.client_id or DEFAULT_PUBLIC_CLIENT_ID, authority=s.authority, token_cache=self._cache
            )
        elif self.mode == "client-secret":
            self._require("tenant_id", "client_id", "client_secret")
            self._app = msal.ConfidentialClientApplication(
                s.client_id, authority=s.authority, client_credential=s.client_secret, token_cache=self._cache
            )
        elif self.mode == "certificate":
            self._require("tenant_id", "client_id", "certificate_path", "certificate_thumbprint")
            key = Path(s.certificate_path).read_text(encoding="utf-8")
            self._app = msal.ConfidentialClientApplication(
                s.client_id,
                authority=s.authority,
                client_credential={"private_key": key, "thumbprint": s.certificate_thumbprint},
                token_cache=self._cache,
            )
        else:
            raise AuthError(f"Auth mode {self.mode!r} does not use MSAL")
        return self._app

    def get_token(self, scope: str) -> str:
        if scope in self._memo:
            return self._memo[scope]
        token = self._acquire(scope)
        self._memo[scope] = token
        return token

    def _acquire(self, scope: str) -> str:
        if self.mode == "token":
            env = _TOKEN_ENV.get(scope)
            value = os.environ.get(env or "")
            if not value:
                raise AuthError(f"Auth mode 'token' needs the {env} environment variable for {scope}")
            return value

        app = self._client()
        scopes = [scope]
        result = None
        if self.mode in APP_ONLY_MODES:
            result = app.acquire_token_for_client(scopes=scopes)
        else:
            accounts = app.get_accounts()
            if accounts:
                result = app.acquire_token_silent(scopes, account=accounts[0])
            if not result:
                result = self._sign_in(app, scopes)
        _save_cache(self._cache)

        if not result or "access_token" not in result:
            detail = (result or {}).get("error_description") or (result or {}).get("error") or "no token returned"
            raise AuthError(f"Could not get a token for {scope}: {detail}")
        return result["access_token"]

    def _sign_in(self, app: msal.PublicClientApplication, scopes: list[str]) -> dict:
        if self.mode == "device-code":
            flow = app.initiate_device_flow(scopes=scopes)
            if "user_code" not in flow:
                raise AuthError(f"Device code flow failed: {flow.get('error_description', flow)}")
            self.echo(flow["message"])
            return app.acquire_token_by_device_flow(flow)
        self.echo("Opening a browser window to sign in...")
        return app.acquire_token_interactive(scopes=scopes, prompt="select_account")

    def signed_in_user(self) -> str | None:
        if self.mode not in DELEGATED_MODES:
            return None
        accounts = self._client().get_accounts()
        return accounts[0].get("username") if accounts else None


def logout() -> bool:
    if TOKEN_CACHE_FILE.exists():
        TOKEN_CACHE_FILE.unlink()
        return True
    return False
