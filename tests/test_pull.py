import json
from datetime import date

import pytest
from typer.testing import CliRunner

from power_admin import cli, collectors
from power_admin.auth import TokenProvider
from power_admin.config import Settings
from power_admin.http import ApiClient, ApiError
from power_admin.storage import flatten, to_csv


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.content = b"x" if body is not None else b""
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeSession:
    """Routes requests by (method, url-substring) to queued responses."""

    def __init__(self, routes):
        self.routes = routes  # list of (method, substring, [responses])
        self.calls = []

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json, "headers": headers})
        for m, sub, queue in self.routes:
            if m == method and sub in url:
                return queue.pop(0) if len(queue) > 1 else queue[0]
        raise AssertionError(f"unexpected {method} {url}")


@pytest.fixture
def token_env(monkeypatch, tmp_path):
    monkeypatch.setenv("POWER_ADMIN_PP_TOKEN", "pp-token")
    monkeypatch.setenv("POWER_ADMIN_GRAPH_TOKEN", "graph-token")
    monkeypatch.setattr("power_admin.config.CONFIG_FILE", tmp_path / "none.json")
    return tmp_path


def make_client(routes):
    session = FakeSession(routes)
    tokens = TokenProvider(Settings(auth_mode="token"))
    return ApiClient(tokens, session=session, sleep=lambda s: None), session


AGENT = {"name": "AGENT-1", "type": "microsoft.copilotstudio/agents", "properties": {"displayName": "HR Bot"}}
APP = {"name": "app-1", "type": "microsoft.powerapps/canvasapps", "properties": {"displayName": "Expenses"}}
FLOW = {"name": "flow-1", "type": "microsoft.powerautomate/cloudflows", "properties": {"displayName": "Approvals"}}


def test_inventory_pages_and_splits(token_env):
    client, session = make_client(
        [("POST", "/resourcequery/resources/query", [
            FakeResponse(body={"data": [AGENT, APP], "skipToken": "next"}),
            FakeResponse(body={"data": [FLOW]}),
        ])]
    )
    ctx = collectors.PullContext(client, date(2026, 9, 1), date(2026, 10, 30))
    out = collectors.collect_inventory(ctx, {})
    assert [len(out[k]) for k in ("inventory", "agents", "apps", "flows")] == [3, 1, 1, 1]
    assert session.calls[1]["json"]["Options"]["SkipToken"] == "next"
    assert session.calls[0]["headers"]["Authorization"] == "Bearer pp-token"


def test_credits_follow_continuation_and_attach_names(token_env):
    client, session = make_client(
        [("GET", "/licensing/entitlements/MCSMessages/resources", [
            FakeResponse(body={"value": [{"resourceId": "agent-1", "environmentId": "ENV-1", "consumed": 12.5}],
                               "continuationtoken": "c2"}),
            FakeResponse(body={"value": [{"resourceId": "other", "environmentId": "env-1", "consumed": 1}]}),
        ])]
    )
    ctx = collectors.PullContext(client, date(2026, 9, 1), date(2026, 10, 30))
    so_far = {"agents": [AGENT], "environments": [{"id": "env-1", "displayName": "Prod"}]}
    rows = collectors.collect_credits(ctx, so_far)["agent_credits"]
    assert rows[0]["resourceName"] == "HR Bot" and rows[0]["environmentName"] == "Prod"
    assert rows[1]["resourceName"] is None
    assert session.calls[0]["params"]["fromDate"] == "2026-09-01"
    assert session.calls[1]["params"]["continuationToken"] == "c2"


def test_users_join_license_names(token_env):
    client, _ = make_client(
        [
            ("GET", "/v1.0/users", [FakeResponse(body={"value": [
                {"id": "u1", "userPrincipalName": "a@x.com", "assignedLicenses": [{"skuId": "s1"}]}]})]),
            ("GET", "/v1.0/subscribedSkus", [FakeResponse(body={"value": [{"skuId": "s1", "skuPartNumber": "POWERAPPS_PER_USER"}]})]),
        ]
    )
    ctx = collectors.PullContext(client, date(2026, 9, 1), date(2026, 10, 30))
    out = collectors.collect_users(ctx, {})
    assert out["user_licenses"] == [
        {"userId": "u1", "userPrincipalName": "a@x.com", "skuId": "s1", "skuPartNumber": "POWERAPPS_PER_USER"}
    ]


def test_retries_on_429_then_raises_on_403(token_env):
    client, session = make_client(
        [
            ("GET", "/a", [FakeResponse(429, {"error": "slow"}, {"Retry-After": "1"}), FakeResponse(body={"ok": 1})]),
            ("GET", "/b", [FakeResponse(403, {"error": "forbidden"})]),
        ]
    )
    assert client.get_json("https://x/a", "https://api.powerplatform.com/.default") == {"ok": 1}
    with pytest.raises(ApiError) as err:
        client.get_json("https://x/b", "https://api.powerplatform.com/.default")
    assert err.value.status == 403


def test_flatten_and_csv():
    row = {"a": 1, "p": {"x": "y", "n": {"z": 2}}, "l": [1, 2]}
    assert flatten(row) == {"a": 1, "p.x": "y", "p.n.z": 2, "l": "[1, 2]"}
    assert to_csv([row, {"b": 3}]).splitlines()[0] == "a,p.x,p.n.z,l,b"


def test_cli_pull_writes_partitioned_files(token_env, monkeypatch):
    out_dir = token_env / "out"
    session = FakeSession(
        [
            ("GET", "/environmentmanagement/environments", [FakeResponse(body={"value": [{"id": "env-1", "displayName": "Prod"}]})]),
            ("POST", "/resourcequery/resources/query", [FakeResponse(body={"data": [AGENT]})]),
            ("GET", "/licensing/tenantCapacity/currencyReports", [FakeResponse(body=[{"currencyType": "MCSMessages", "purchased": 25000}])]),
            ("GET", "/licensing/tenantCapacity", [FakeResponse(body={"tenantCapacities": [
                {"capacityType": "Database", "totalCapacity": 100, "consumption": {"actual": 40}}]})]),
            ("GET", "/licensing/entitlements/", [FakeResponse(body={"value": [{"resourceId": "agent-1", "consumed": 3}]})]),
            ("GET", "/v1.0/users", [FakeResponse(body={"value": []})]),
            ("GET", "/v1.0/subscribedSkus", [FakeResponse(body={"value": []})]),
        ]
    )
    monkeypatch.setattr("power_admin.http.requests.Session", lambda: session)
    result = CliRunner().invoke(cli.app, ["pull", "-o", str(out_dir), "--auth", "token", "--from", "2026-10-01"])
    assert result.exit_code == 0, result.output

    credits = list(out_dir.glob("agent_credits/snapshot_date=*/agent_credits.jsonl"))
    assert len(credits) == 1
    row = json.loads(credits[0].read_text().splitlines()[0])
    assert row["resourceName"] == "HR Bot" and row["periodFrom"] == "2026-10-01"
    assert list(out_dir.glob("capacity/snapshot_date=*/capacity.csv"))
    manifest = json.loads(next(out_dir.glob("_runs/*.json")).read_text())
    assert manifest["datasets"]["agents"] == 1 and manifest["errors"] == {}


def test_cli_app_only_skips_inventory(token_env, monkeypatch):
    monkeypatch.setattr("power_admin.auth.TokenProvider.get_token", lambda self, scope: "t")
    session = FakeSession([("GET", "/environmentmanagement/environments", [FakeResponse(body={"value": []})])])
    monkeypatch.setattr("power_admin.http.requests.Session", lambda: session)
    result = CliRunner().invoke(
        cli.app,
        ["pull", "-o", str(token_env / "o"), "--auth", "client-secret", "--only", "inventory", "--only", "environments",
         "-t", "t", "-c", "c", "--client-secret", "s"],
    )
    assert result.exit_code == 1
    assert "needs a signed-in user" in result.output
    assert not any("/resourcequery" in c["url"] for c in session.calls)


def test_delegated_mode_defaults_to_powershell_client(monkeypatch, tmp_path):
    monkeypatch.setattr("power_admin.auth.TOKEN_CACHE_FILE", tmp_path / "cache.json")
    from power_admin.auth import DEFAULT_PUBLIC_CLIENT_ID
    seen = {}

    class FakePCA:
        def __init__(self, client_id, authority, token_cache):
            seen["client_id"], seen["authority"] = client_id, authority

    monkeypatch.setattr("power_admin.auth.msal.PublicClientApplication", FakePCA)
    TokenProvider(Settings(auth_mode="interactive", tenant_id="contoso.com"))._client()
    assert seen == {"client_id": DEFAULT_PUBLIC_CLIENT_ID, "authority": "https://login.microsoftonline.com/contoso.com"}
