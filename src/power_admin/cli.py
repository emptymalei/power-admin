"""power-admin command line interface."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Optional

import typer

from . import __version__
from .auth import APP_ONLY_MODES, AuthError, TokenProvider, logout as clear_cache
from .collectors import COLLECTORS, PullContext
from .config import AUTH_MODES, CONFIG_FILE, GRAPH_SCOPE, PP_SCOPE, load_settings, save_settings
from .http import ApiClient, ApiError
from .storage import configure_s3, write_dataset, write_manifest

app = typer.Typer(
    help="Pull Power Platform admin data (inventory, capacity, Copilot credits, users) to a folder or S3.",
    no_args_is_help=True,
    add_completion=False,
)

# Shared auth options -------------------------------------------------------
TenantOpt = Annotated[Optional[str], typer.Option("--tenant-id", "-t", help="Entra tenant ID or domain.")]
ClientOpt = Annotated[Optional[str], typer.Option("--client-id", "-c", help="App registration (client) ID. Optional for interactive/device-code.")]
AuthOpt = Annotated[
    Optional[str],
    typer.Option("--auth", "-a", help=f"Auth mode: {', '.join(AUTH_MODES)}. Default: interactive."),
]
SecretOpt = Annotated[
    Optional[str], typer.Option("--client-secret", help="Client secret (client-secret mode). Prefer POWER_ADMIN_CLIENT_SECRET.")
]
CertOpt = Annotated[Optional[str], typer.Option("--certificate-path", help="PEM private key (certificate mode).")]
ThumbOpt = Annotated[Optional[str], typer.Option("--certificate-thumbprint", help="Certificate thumbprint.")]


def _settings(tenant_id, client_id, auth, client_secret=None, certificate_path=None, certificate_thumbprint=None, output=None):
    try:
        return load_settings(
            tenant_id=tenant_id,
            client_id=client_id,
            auth_mode=auth,
            client_secret=client_secret,
            certificate_path=certificate_path,
            certificate_thumbprint=certificate_thumbprint,
            output=output,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _parse_date(value: Optional[str], default: date) -> date:
    return date.fromisoformat(value) if value else default


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command()
def configure(
    tenant_id: TenantOpt = None,
    client_id: ClientOpt = None,
    auth: AuthOpt = None,
    certificate_path: CertOpt = None,
    certificate_thumbprint: ThumbOpt = None,
    output: Annotated[Optional[str], typer.Option("--output", "-o", help="Default output folder or s3://bucket/prefix.")] = None,
) -> None:
    """Save defaults to ~/.power-admin/config.json so you don't pass them every time (secrets are never saved)."""
    s = _settings(tenant_id, client_id, auth, None, certificate_path, certificate_thumbprint, output)
    path = save_settings(s)
    typer.echo(f"Saved settings to {path}")


@app.command()
def login(
    tenant_id: TenantOpt = None,
    client_id: ClientOpt = None,
    auth: AuthOpt = None,
    client_secret: SecretOpt = None,
    certificate_path: CertOpt = None,
    certificate_thumbprint: ThumbOpt = None,
) -> None:
    """Sign in and cache tokens for the Power Platform API and Microsoft Graph."""
    s = _settings(tenant_id, client_id, auth, client_secret, certificate_path, certificate_thumbprint)
    tokens = TokenProvider(s, echo=typer.echo)
    try:
        tokens.get_token(PP_SCOPE)
        typer.echo("Power Platform API: OK")
        tokens.get_token(GRAPH_SCOPE)
        typer.echo("Microsoft Graph: OK")
    except AuthError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    user = tokens.signed_in_user()
    typer.echo(f"Signed in{' as ' + user if user else ''} using {s.auth_mode} auth.")


@app.command()
def logout() -> None:
    """Delete the cached sign-in."""
    typer.echo("Signed out." if clear_cache() else "No cached sign-in found.")


@app.command()
def datasets() -> None:
    """List what `pull` can collect."""
    for c in COLLECTORS.values():
        note = "  (needs a signed-in user)" if c.delegated_only else ""
        typer.echo(f"{c.name:<14}{c.description}{note}")


@app.command()
def pull(
    output: Annotated[
        Optional[str], typer.Option("--output", "-o", help="Folder or s3://bucket/prefix. Default from config.")
    ] = None,
    only: Annotated[Optional[list[str]], typer.Option("--only", help="Collect only these (repeatable).")] = None,
    skip: Annotated[Optional[list[str]], typer.Option("--skip", help="Skip these (repeatable).")] = None,
    date_from: Annotated[
        Optional[str], typer.Option("--from", help="Start date for credit usage, YYYY-MM-DD. Default: 1st of last month.")
    ] = None,
    date_to: Annotated[Optional[str], typer.Option("--to", help="End date, YYYY-MM-DD. Default: today.")] = None,
    credit_entitlement: Annotated[
        str, typer.Option(help="Licensing entitlement ID used for Copilot credit consumption.")
    ] = "MCSMessages",
    fmt: Annotated[list[str], typer.Option("--format", "-f", help="jsonl and/or csv (repeatable).")] = ["jsonl", "csv"],
    aws_profile: Annotated[Optional[str], typer.Option(help="AWS profile for s3:// output.")] = None,
    s3_endpoint_url: Annotated[Optional[str], typer.Option(help="Custom S3 endpoint (e.g. MinIO).", hidden=True)] = None,
    tenant_id: TenantOpt = None,
    client_id: ClientOpt = None,
    auth: AuthOpt = None,
    client_secret: SecretOpt = None,
    certificate_path: CertOpt = None,
    certificate_thumbprint: ThumbOpt = None,
) -> None:
    """Collect admin data and save a dated snapshot."""
    s = _settings(tenant_id, client_id, auth, client_secret, certificate_path, certificate_thumbprint, output)
    unknown = [n for n in (only or []) + (skip or []) if n not in COLLECTORS]
    if unknown:
        raise typer.BadParameter(f"Unknown dataset(s): {', '.join(unknown)}. See `power-admin datasets`.")
    bad_fmt = [f for f in fmt if f not in ("jsonl", "csv")]
    if bad_fmt:
        raise typer.BadParameter(f"Unknown format(s): {', '.join(bad_fmt)}")

    today = date.today()
    first_of_last_month = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    ctx_from, ctx_to = _parse_date(date_from, first_of_last_month), _parse_date(date_to, today)

    if s.output.startswith("s3://"):
        configure_s3(aws_profile, s3_endpoint_url)

    tokens = TokenProvider(s, echo=typer.echo)
    ctx = PullContext(ApiClient(tokens), ctx_from, ctx_to, credit_entitlement)
    selected = [c for name, c in COLLECTORS.items() if (not only or name in only) and name not in (skip or [])]

    now = datetime.now(timezone.utc)
    snapshot = now.date().isoformat()
    manifest: dict = {
        "run_id": now.strftime("%Y%m%dT%H%M%SZ"),
        "started": now.isoformat(),
        "auth_mode": s.auth_mode,
        "period": {"from": ctx_from.isoformat(), "to": ctx_to.isoformat()},
        "datasets": {},
        "errors": {},
    }
    results: dict[str, list[dict]] = {}
    for c in selected:
        if c.delegated_only and s.auth_mode in APP_ONLY_MODES:
            msg = "skipped: needs a signed-in user (use interactive or device-code auth)"
            manifest["errors"][c.name] = msg
            typer.secho(f"- {c.name}: {msg}", fg=typer.colors.YELLOW)
            continue
        typer.echo(f"- {c.name}: collecting...")
        try:
            out = c.run(ctx, results)
        except (ApiError, AuthError) as exc:
            manifest["errors"][c.name] = str(exc)
            typer.secho(f"  failed: {exc}", fg=typer.colors.RED, err=True)
            continue
        for name, rows in out.items():
            results[name] = rows
            write_dataset(s.output, name, snapshot, rows, formats=tuple(fmt))
            manifest["datasets"][name] = len(rows)
            typer.echo(f"  {name}: {len(rows)} rows")

    manifest["finished"] = datetime.now(timezone.utc).isoformat()
    path = write_manifest(s.output, manifest["run_id"], manifest)
    typer.echo(f"Saved to {s.output} (manifest: {path})")
    if manifest["errors"]:
        raise typer.Exit(1)


@app.command()
def status() -> None:
    """Show the active settings and cached sign-in."""
    s = load_settings()
    typer.echo(f"Config file: {CONFIG_FILE}{'' if CONFIG_FILE.exists() else ' (not created yet)'}")
    for k in ("tenant_id", "client_id", "auth_mode", "output"):
        typer.echo(f"{k:<12}{getattr(s, k) or '-'}")
    if s.auth_mode in ("interactive", "device-code"):
        typer.echo(f"{'signed in':<12}{TokenProvider(s).signed_in_user() or 'no'}")


if __name__ == "__main__":
    app()
