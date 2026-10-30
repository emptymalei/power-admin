import pytest
import yaml
from typer.testing import CliRunner

from power_admin import cli
from power_admin.config import ConfigError, Settings, load_settings, save_settings

CONFIG = """
tenant_id: contoso.onmicrosoft.com
output: ./data
include: [audit]
profiles:
  monthly:
    output: s3://bucket/pp
    aws_profile: admin
    from: 2026-09-01
  ci:
    extends: monthly
    auth: client-secret
    client_id: abc
  loop-a:
    extends: loop-b
  loop-b:
    extends: loop-a
"""


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    for var in ("POWER_ADMIN_OUTPUT", "POWER_ADMIN_AUTH", "POWER_ADMIN_PROFILE", "POWER_ADMIN_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    path = tmp_path / "power-admin.yaml"
    path.write_text(CONFIG)
    return path


def test_top_level_then_profile_then_extends(cfg):
    base = load_settings(config=str(cfg))
    assert base.output == "./data" and base.include == ["audit"] and base.auth == "interactive"
    ci = load_settings(config=str(cfg), profile="ci")
    assert ci.output == "s3://bucket/pp" and ci.aws_profile == "admin" and ci.date_from == "2026-09-01"
    assert ci.auth == "client-secret" and ci.client_id == "abc" and ci.tenant_id == "contoso.onmicrosoft.com"


def test_env_and_flags_override_config(cfg, monkeypatch):
    monkeypatch.setenv("POWER_ADMIN_OUTPUT", "./from-env")
    monkeypatch.setenv("POWER_ADMIN_SKIP", "users,rbac")
    s = load_settings(config=str(cfg), profile="monthly")
    assert s.output == "./from-env" and s.skip == ["users", "rbac"]
    assert load_settings(config=str(cfg), output="./flag").output == "./flag"


def test_errors(cfg, tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_settings(config=str(cfg), profile="nope")
    with pytest.raises(ConfigError, match="loop"):
        load_settings(config=str(cfg), profile="loop-a")
    bad = tmp_path / "bad.yaml"
    bad.write_text("typo_key: 1\n")
    with pytest.raises(ConfigError, match="Unknown setting"):
        load_settings(config=str(bad))
    with pytest.raises(ConfigError, match="Config file not found"):
        load_settings(config=str(tmp_path / "missing.yaml"))


def test_save_settings_into_profile_skips_secrets(tmp_path):
    path = tmp_path / "c.yaml"
    save_settings(Settings(tenant_id="t", client_secret="hunter2"), path)
    save_settings(Settings(output="s3://b/p"), path, profile="monthly")
    data = yaml.safe_load(path.read_text())
    assert data["tenant_id"] == "t" and "client_secret" not in data
    assert data["profiles"]["monthly"] == {"output": "s3://b/p"}


def test_init_config_template_is_loadable(tmp_path):
    path = tmp_path / "power-admin.yaml"
    result = CliRunner().invoke(cli.app, ["init-config", str(path)])
    assert result.exit_code == 0, result.output
    assert load_settings(config=str(path), profile="automation").auth == "client-secret"
    shown = CliRunner().invoke(cli.app, ["show-config", "-C", str(path), "-p", "quick"])
    assert "only:\n- inventory\n- credits" in shown.output
