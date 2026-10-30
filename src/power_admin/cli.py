"""power-admin command line interface."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Optional

import typer
import yaml

from . import __version__
from .auth import APP_ONLY_MODES, AuthError, TokenProvider, logout as clear_cache
from .collectors import COLLECTORS, PullContext, select_collectors
from .config import (
    AUTH_MODES,
    GRAPH_SCOPE,
    PP_SCOPE,
    SECRET_FIELDS,
    ConfigError,
    Settings,
    config_path,
    load_settings,
    save_settings,
)
from .http import ApiClient, ApiError
from .storage import configure_s3, write_dataset, write_manifest

app = typer.Typer(
    help="Pull Power Platform admin data (inventory, usage, capacity, Copilot credits, users) to a folder or S3.",
    no_args_is_help=True,
    add_completion=False,
)

# Shared options ----------------------------------------------------------------------
ConfigOpt = Annotated[
    Optional[str],
    typer.Option("--config", "-C", help="YAML config file. Default: POWER_ADMIN_CONFIG or ~/.power-admin/config.yaml."),
]
ProfileOpt = Annotated[Optional[str], typer.Option("--profile", "-p", help="Profile from the config file.")]
TenantOpt = Annotated[Optional[str], typer.Option("--tenant-id", "-t", help="Entra tenant ID or domain.")]
ClientOpt = Annotated[
    Optional[str], typer.Option("--client-id", "-c", help="App registration (client) ID. Optional for interactive/device-code.")
]
AuthOpt = Annotated[Optional[str], typer.Option("--auth", "-a", help=f"Auth mode: {', '.join(AUTH_MODES)}.")]
SecretOpt = Annotated[
    Optional[str], typer.Option("--client-secret", help="Client secret. Prefer POWER_ADMIN_CLIENT_SECRET.")
]
CertOpt = Annotated[Optional[str], typer.Option("--certificate-path", help="PEM private key (certificate mode).")]
ThumbOpt = Annotated[Optional[str], typer.Option("--certificate-thumbprint", help="Certificate thumbprint.")]
OutputOpt = Annotated[Optional[str], typer.Option("--output", "-o", help="Folder or s3://bucket/prefix.")]


def _settings(**kwargs) -> Settings:
    try:
        return load_settings(**kwargs)
    except ConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _fail(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command()
def configure(
    config: ConfigOpt = None,
    profile: Annotated[Optional[str], typer.Option("--profile", "-p", help="Save into this profile instead of the top level.")] = None,
    tenant_id: TenantOpt = None,
    client_id: ClientOpt = None,
    auth: AuthOpt = None,
    certificate_path: CertOpt = None,
    certificate_thumbprint: ThumbOpt = None,
    output: OutputOpt = None,
    aws_profile: Annotated[Optional[str], typer.Option(help="AWS profile for s3:// output.")] = None,
) -> None:
    """Save settings to the YAML config (secrets are never saved). Edit the file by hand for everything else."""
    s = _settings(
        tenant_id=tenant_id,
        client_id=client_id,
        auth=auth,
        certificate_path=certificate_path,
        certificate_thumbprint=certificate_thumbprint,
        output=output,
        aws_profile=aws_profile,
    )
    path = save_settings(s, config_path(config), profile)
    typer.echo(f"Saved to {path}{' (profile ' + profile + ')' if profile else ''}")


@app.command("init-config")
def init_config(
    path: Annotated[Path, typer.Argument(help="Where to write the template.")] = Path("power-admin.yaml"),
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Write a commented YAML config template to edit and reuse."""
    if path.exists() and not force:
        _fail(f"{path} already exists (use --force to overwrite)")
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    typer.echo(f"Wrote {path}. Use it with: power-admin pull -C {path} [-p PROFILE]")


@app.command("show-config")
def show_config(config: ConfigOpt = None, profile: ProfileOpt = None) -> None:
    """Print the effective settings after merging the config file, profile and env vars."""
    s = _settings(config=config, profile=profile)
    data = {k: v for k, v in asdict(s).items() if k not in SECRET_FIELDS}
    typer.echo(f"# config file: {config_path(config)}")
    typer.echo(yaml.safe_dump(data, sort_keys=False).rstrip())


@app.command()
def login(
    config: ConfigOpt = None,
    profile: ProfileOpt = None,
    tenant_id: TenantOpt = None,
    client_id: ClientOpt = None,
    auth: AuthOpt = None,
    client_secret: SecretOpt = None,
    certificate_path: CertOpt = None,
    certificate_thumbprint: ThumbOpt = None,
) -> None:
    """Sign in and cache tokens for the Power Platform API and Microsoft Graph."""
    s = _settings(
        config=config,
        profile=profile,
        tenant_id=tenant_id,
        client_id=client_id,
        auth=auth,
        client_secret=client_secret,
        certificate_path=certificate_path,
        certificate_thumbprint=certificate_thumbprint,
    )
    tokens = TokenProvider(s, echo=typer.echo)
    try:
        tokens.get_token(PP_SCOPE)
        typer.echo("Power Platform API: OK")
        tokens.get_token(GRAPH_SCOPE)
        typer.echo("Microsoft Graph: OK")
    except AuthError as exc:
        _fail(str(exc))
    user = tokens.signed_in_user()
    typer.echo(f"Signed in{' as ' + user if user else ''} using {s.auth} auth.")


@app.command()
def logout() -> None:
    """Delete the cached sign-in."""
    typer.echo("Signed out." if clear_cache() else "No cached sign-in found.")


@app.command()
def datasets() -> None:
    """List what `pull` can collect. Opt-in ones run only with --include or --only."""
    for c in COLLECTORS.values():
        tags = []
        if not c.default:
            tags.append("opt-in")
        if c.delegated_only:
            tags.append("signed-in user only")
        if c.needs:
            tags.append("needs " + c.needs)
        typer.echo(f"{c.name:<14}{c.description}{'  [' + '; '.join(tags) + ']' if tags else ''}")


@app.command()
def pull(
    config: ConfigOpt = None,
    profile: ProfileOpt = None,
    output: OutputOpt = None,
    only: Annotated[Optional[list[str]], typer.Option("--only", help="Collect only these (repeatable).")] = None,
    include: Annotated[
        Optional[list[str]], typer.Option("--include", "-i", help="Add opt-in datasets, e.g. dataverse, audit.")
    ] = None,
    skip: Annotated[Optional[list[str]], typer.Option("--skip", help="Skip these (repeatable).")] = None,
    date_from: Annotated[
        Optional[str], typer.Option("--from", help="Start date YYYY-MM-DD for usage/credits. Default: 1st of last month.")
    ] = None,
    date_to: Annotated[Optional[str], typer.Option("--to", help="End date YYYY-MM-DD. Default: today.")] = None,
    environment: Annotated[
        Optional[list[str]], typer.Option("--environment", "-e", help="Limit per-environment collectors (repeatable).")
    ] = None,
    fmt: Annotated[Optional[list[str]], typer.Option("--format", "-f", help="jsonl and/or csv (repeatable).")] = None,
    aws_profile: Annotated[Optional[str], typer.Option(help="AWS profile for s3:// output.")] = None,
    tenant_id: TenantOpt = None,
    client_id: ClientOpt = None,
    auth: AuthOpt = None,
    client_secret: SecretOpt = None,
    certificate_path: CertOpt = None,
    certificate_thumbprint: ThumbOpt = None,
) -> None:
    """Collect admin data and save a dated snapshot."""
    s = _settings(
        config=config,
        profile=profile,
        output=output,
        only=only,
        include=include,
        skip=skip,
        date_from=date_from,
        date_to=date_to,
        environments=environment,
        formats=fmt,
        aws_profile=aws_profile,
        tenant_id=tenant_id,
        client_id=client_id,
        auth=auth,
        client_secret=client_secret,
        certificate_path=certificate_path,
        certificate_thumbprint=certificate_thumbprint,
    )
    try:
        selected = select_collectors(s)
        today = date.today()
        first_of_last_month = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        ctx_from = date.fromisoformat(s.date_from) if s.date_from else first_of_last_month
        ctx_to = date.fromisoformat(s.date_to) if s.date_to else today
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    if s.output.startswith("s3://"):
        configure_s3(s.aws_profile, s.s3_endpoint_url)

    tokens = TokenProvider(s, echo=typer.echo)
    ctx = PullContext(ApiClient(tokens), ctx_from, ctx_to, settings=s, log=typer.echo)

    now = datetime.now(timezone.utc)
    snapshot = now.date().isoformat()
    manifest: dict = {
        "run_id": now.strftime("%Y%m%dT%H%M%SZ"),
        "version": __version__,
        "started": now.isoformat(),
        "auth": s.auth,
        "profile": profile,
        "period": {"from": ctx_from.isoformat(), "to": ctx_to.isoformat()},
        "collectors": [c.name for c in selected],
        "datasets": {},
        "errors": {},
    }
    results: dict[str, list[dict]] = {}
    for c in selected:
        if c.delegated_only and s.auth in APP_ONLY_MODES:
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
            write_dataset(s.output, name, snapshot, rows, formats=tuple(s.formats))
            manifest["datasets"][name] = len(rows)
            typer.echo(f"  {name}: {len(rows)} rows")

    manifest["finished"] = datetime.now(timezone.utc).isoformat()
    path = write_manifest(s.output, manifest["run_id"], manifest)
    typer.echo(f"Saved to {s.output} (manifest: {path})")
    if manifest["errors"]:
        raise typer.Exit(1)


@app.command()
def status(config: ConfigOpt = None, profile: ProfileOpt = None) -> None:
    """Show the active settings and cached sign-in."""
    s = _settings(config=config, profile=profile)
    path = config_path(config)
    typer.echo(f"Config file: {path}{'' if path.exists() else ' (not created yet)'}")
    for k in ("tenant_id", "client_id", "auth", "output"):
        typer.echo(f"{k:<12}{getattr(s, k) or '-'}")
    if s.auth in ("interactive", "device-code"):
        typer.echo(f"{'signed in':<12}{TokenProvider(s).signed_in_user() or 'no'}")


CONFIG_TEMPLATE = """\
# power-admin config. Top-level keys are defaults; profiles override them.
# Use: power-admin pull -C power-admin.yaml -p monthly
# Every key can also be set as an env var (POWER_ADMIN_<KEY>) or a CLI flag.

tenant_id: contoso.onmicrosoft.com
# client_id: 00000000-0000-0000-0000-000000000000   # optional for interactive/device-code
auth: interactive            # interactive | device-code | client-secret | certificate | token
output: ./power-admin-output # local folder or s3://bucket/prefix
# aws_profile: admin
formats: [jsonl, csv]

# What to collect. Run `power-admin datasets` for the list.
# only: [inventory, credits]          # run exactly these
include: []                           # add opt-in datasets: dataverse, flow_runs, audit, sign_ins
skip: []
# from: 2026-10-01                    # default: 1st of last month
# to: 2026-10-31                      # default: today

# Collector options
credit_entitlement: MCSMessages
environments: []                      # limit dataverse/flow_runs to these environment IDs
max_flows_per_environment: 200
audit_timeout_minutes: 30
# audit_record_types: [powerAppsApp, microsoftFlow, powerPlatformAdminEnvironment]

profiles:
  monthly:
    output: s3://my-bucket/power-platform
    aws_profile: admin
    include: [dataverse, audit]

  quick:
    only: [inventory, credits]
    output: ./quick

  automation:
    extends: monthly                  # reuse everything from monthly
    auth: client-secret               # secret from POWER_ADMIN_CLIENT_SECRET
    client_id: 00000000-0000-0000-0000-000000000000
"""


if __name__ == "__main__":
    app()
