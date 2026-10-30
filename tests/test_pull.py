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
    """Routes requests by (method, url-substring) to queued responses; first match wins."""

    def __init__(self, routes, fallback=None):
        self.routes = routes  # list of (method, substring, [responses])
        self.fallback = fallback
        self.calls = []

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json, "headers": headers})
        for m, sub, queue in self.routes:
            if m == method and sub in url:
                return queue.pop(0) if len(queue) > 1 else queue[0]
        if self.fallback is not None:
            return self.fallback
        raise AssertionError(f"unexpected {method} {url}")

    def urls(self, sub):
        return [c for c in self.calls if sub in c["url"]]


@pytest.fixture
def token_env(monkeypatch, tmp_path):
    monkeypatch.setenv("POWER_ADMIN_PP_TOKEN", "pp-token")
    monkeypatch.setenv("POWER_ADMIN_GRAPH_TOKEN", "graph-token")
    monkeypatch.setattr("power_admin.config.DEFAULT_CONFIG_FILE", tmp_path / "none.yaml")
    monkeypatch.setattr("power_admin.auth.TOKEN_CACHE_FILE", tmp_path / "cache.json")
    return tmp_path


def make_ctx(routes, settings=None, fallback=None):
    session = FakeSession(routes, fallback)
    tokens = TokenProvider(Settings(auth="token"))
    client = ApiClient(tokens, session=session, sleep=lambda s: None)
    ctx = collectors.PullContext(
        client, date(2026, 9, 1), date(2026, 10, 30), settings=settings or Settings(auth="token"), sleep=lambda s: None
    )
    return ctx, session


AGENT = {"name": "AGENT-1", "type": "microsoft.copilotstudio/agents", "properties": {"displayName": "HR Bot"}}
APP = {"name": "app-1", "type": "microsoft.powerapps/canvasapps", "properties": {"displayName": "Expenses"}}
FLOW = {"name": "flow-1", "type": "microsoft.powerautomate/cloudflows", "properties": {"displayName": "Approvals"}}
CONNECTOR = {"name": "shared_sql", "type": "microsoft.powerplatformconnector/connectors"}
EMPTY = FakeResponse(body={"value": []})


def test_inventory_pages_and_splits(token_env):
    ctx, session = make_ctx(
        [("POST", "/resourcequery/resources/query", [
            FakeResponse(body={"data": [AGENT, APP], "skipToken": "next"}),
            FakeResponse(body={"data": [FLOW, CONNECTOR]}),
        ])]
    )
    out = collectors.collect_inventory(ctx, {})
    assert [len(out[k]) for k in ("inventory", "agents", "apps", "flows", "connectors")] == [4, 1, 1, 1, 1]
    assert session.calls[1]["json"]["Options"]["SkipToken"] == "next"
    assert session.calls[0]["headers"]["Authorization"] == "Bearer pp-token"


def test_date_windows():
    w = collectors.date_windows(date(2026, 8, 15), date(2026, 10, 3), "month")
    assert w == [
        (date(2026, 8, 15), date(2026, 8, 31)),
        (date(2026, 9, 1), date(2026, 9, 30)),
        (date(2026, 10, 1), date(2026, 10, 3)),
    ]
    assert len(collectors.date_windows(date(2026, 10, 1), date(2026, 10, 3), "day")) == 3
    assert collectors.date_windows(date(2026, 10, 1), date(2026, 10, 3), "none") == [(date(2026, 10, 1), date(2026, 10, 3))]


def test_credits_history_per_month_with_users_and_summary(token_env):
    ctx, session = make_ctx(
        [
            ("GET", "/MCSMessages/resources", [
                FakeResponse(body={"value": [{"resourceId": "agent-1", "environmentId": "ENV-1", "consumed": 10}],
                                   "continuationtoken": "c2"}),
                FakeResponse(body={"value": [{"resourceId": "agent-1", "environmentId": "env-1", "consumed": 2.5}]}),
            ]),
            ("GET", "/MCSMessages/users", [FakeResponse(body={"value": [{"users": [{"userId": "u1", "consumed": 4}]}]})]),
            ("GET", "/MCSMessages/licenses", [FakeResponse(body={"value": [{"date": "2026-10-01", "licenses": []}]})]),
        ]
    )
    so_far = {"agents": [AGENT], "environments": [{"id": "env-1", "displayName": "Prod"}]}
    out = collectors.collect_credits(ctx, so_far)
    rows = out["agent_credits"]
    # Sep and Oct windows; the Sep window paged once (continuation), Oct window got the last queued page
    resource_calls = session.urls("/resources")
    assert [c["params"]["fromDate"] for c in resource_calls] == ["2026-09-01", "2026-09-01", "2026-10-01"]
    assert resource_calls[1]["params"]["continuationToken"] == "c2"
    assert rows[0]["resourceName"] == "HR Bot" and rows[0]["environmentName"] == "Prod"
    assert rows[0]["periodTo"] == "2026-09-30"
    assert out["agent_credits_summary"][0]["consumed"] == 15.0
    assert out["user_credits"][0]["userId"] == "u1" and len(out["user_credits"]) == 2
    assert out["credit_license_trends"][0]["entitlement"] == "MCSMessages"


def test_dataverse_agents_components_and_sessions(token_env, monkeypatch):
    monkeypatch.setattr(TokenProvider, "get_token", lambda self, scope: "dv-token")
    env = {"id": "env-1", "properties": {"linkedEnvironmentMetadata": {"instanceUrl": "https://contoso.crm4.dynamics.com/"}}}
    assert collectors.find_dataverse_url(env) == "https://contoso.crm4.dynamics.com"
    ctx, session = make_ctx(
        [
            ("GET", "/bots?", [FakeResponse(body={"value": [{"botid": "b1", "name": "HR Bot"}]})]),
            ("GET", "/botcomponents?", [FakeResponse(body={"value": [
                {"_parentbotid_value": "b1", "componenttype": 0, "componenttype@OData.Community.Display.V1.FormattedValue": "Topic"}]})]),
            ("GET", "/conversationtranscripts?", [FakeResponse(body={"value": [
                {"_bot_conversationtranscriptid_value": "b1", "conversationstarttime": "2026-10-02T10:00:00Z"},
                {"_bot_conversationtranscriptid_value": "b1", "conversationstarttime": "2026-10-02T11:00:00Z"},
            ]})]),
        ]
    )
    out = collectors.collect_dataverse(ctx, {"environments": [env]})
    assert out["dataverse_agents"][0]["componentCount"] == 1
    assert out["agent_components"][0]["componentKind"] == "Topic"
    assert out["agent_sessions"][0]["botName"] == "HR Bot"
    assert out["agent_sessions_daily"] == [
        {"environmentId": "env-1", "botId": "b1", "botName": "HR Bot", "date": "2026-10-02", "sessions": 2}
    ]
    transcript_call = session.urls("conversationtranscripts")[0]
    assert "content" not in transcript_call["url"].split("$filter")[0]
    assert transcript_call["headers"]["Prefer"].startswith("odata.include-annotations")


def test_dataverse_skips_environment_on_403(token_env, monkeypatch):
    monkeypatch.setattr(TokenProvider, "get_token", lambda self, scope: "t")
    env = {"id": "env-1", "dataverseUrl": "https://x.crm.dynamics.com"}
    ctx, _ = make_ctx([("GET", "/bots?", [FakeResponse(403, {"error": "no"})])])
    out = collectors.collect_dataverse(ctx, {"environments": [env]})
    assert out["dataverse_agents"] == [] and out["agent_sessions_daily"] == []


def test_advisor_follows_scenarios(token_env):
    ctx, _ = make_ctx(
        [
            ("GET", "/advisorRecommendations/UnusedApps/resources", [FakeResponse(body={"value": [{"resourceId": "app-1", "resourceUsage": 0}]})]),
            ("GET", "/advisorRecommendations?", [FakeResponse(body={"value": [{"scenario": "UnusedApps"}]})]),
        ]
    )
    out = collectors.collect_advisor(ctx, {})
    assert out["advisor_resources"] == [{"scenario": "UnusedApps", "resourceId": "app-1", "resourceUsage": 0}]


def test_audit_polls_until_succeeded(token_env):
    ctx, session = make_ctx(
        [
            ("POST", "/security/auditLog/queries", [FakeResponse(body={"id": "q1", "status": "notStarted"})]),
            ("GET", "/queries/q1/records", [FakeResponse(body={"value": [{"operation": "LaunchPowerApp"}]})]),
            ("GET", "/queries/q1", [FakeResponse(body={"status": "running"}), FakeResponse(body={"status": "succeeded"})]),
        ]
    )
    out = collectors.collect_audit(ctx, {})
    assert out["audit_events"] == [{"operation": "LaunchPowerApp"}]
    assert "microsoftFlow" in session.calls[0]["json"]["recordTypeFilters"]


def test_users_join_license_names(token_env):
    ctx, _ = make_ctx(
        [
            ("GET", "/v1.0/users", [FakeResponse(body={"value": [
                {"id": "u1", "userPrincipalName": "a@x.com", "assignedLicenses": [{"skuId": "s1"}]}]})]),
            ("GET", "/v1.0/subscribedSkus", [FakeResponse(body={"value": [{"skuId": "s1", "skuPartNumber": "POWERAPPS_PER_USER"}]})]),
        ]
    )
    out = collectors.collect_users(ctx, {})
    assert out["user_licenses"][0]["skuPartNumber"] == "POWERAPPS_PER_USER"


def test_retries_on_429_then_raises_on_403(token_env):
    ctx, _ = make_ctx(
        [
            ("GET", "/a", [FakeResponse(429, {"error": "slow"}, {"Retry-After": "1"}), FakeResponse(body={"ok": 1})]),
            ("GET", "/b", [FakeResponse(403, {"error": "forbidden"})]),
        ]
    )
    assert ctx.client.get_json("https://x/a", "https://api.powerplatform.com/.default") == {"ok": 1}
    with pytest.raises(ApiError) as err:
        ctx.client.get_json("https://x/b", "https://api.powerplatform.com/.default")
    assert err.value.status == 403


def test_flatten_and_csv():
    row = {"a": 1, "p": {"x": "y", "n": {"z": 2}}, "l": [1, 2]}
    assert flatten(row) == {"a": 1, "p.x": "y", "p.n.z": 2, "l": "[1, 2]"}
    assert to_csv([row, {"b": 3}]).splitlines()[0] == "a,p.x,p.n.z,l,b"


def test_select_collectors_defaults_include_only_skip():
    names = lambda s: [c.name for c in collectors.select_collectors(s)]
    assert "audit" not in names(Settings()) and "dataverse" in names(Settings())
    assert "audit" in names(Settings(include=["audit"]))
    assert names(Settings(only=["credits", "inventory"])) == ["inventory", "credits"]
    assert "users" not in names(Settings(skip=["users"]))
    with pytest.raises(ValueError):
        collectors.select_collectors(Settings(only=["nope"]))


def test_cli_pull_writes_partitioned_files(token_env, monkeypatch):
    out_dir = token_env / "out"
    session = FakeSession(
        [
            ("GET", "/environmentmanagement/environments", [FakeResponse(body={"value": [{"id": "env-1", "displayName": "Prod"}]})]),
            ("POST", "/resourcequery/resources/query", [FakeResponse(body={"data": [AGENT]})]),
            ("GET", "/licensing/tenantCapacity/currencyReports", [FakeResponse(body=[{"currencyType": "MCSMessages", "purchased": 25000}])]),
            ("GET", "/licensing/tenantCapacity", [FakeResponse(body={"tenantCapacities": [
                {"capacityType": "Database", "totalCapacity": 100, "consumption": {"actual": 40}}]})]),
            ("GET", "/licensing/allocationsByEnvironment", [FakeResponse(body=[
                {"environmentId": "env-1", "currencyAllocations": [{"currencyType": "MCSMessages", "allocated": 500}]}])]),
            ("GET", "/MCSMessages/resources", [FakeResponse(body={"value": [{"resourceId": "agent-1", "consumed": 3}]})]),
        ],
        fallback=EMPTY,
    )
    monkeypatch.setattr("power_admin.http.requests.Session", lambda: session)
    result = CliRunner().invoke(cli.app, ["pull", "-o", str(out_dir), "--auth", "token", "--from", "2026-10-01"])
    assert result.exit_code == 0, result.output

    credits = list(out_dir.glob("agent_credits/snapshot_date=*/agent_credits.jsonl"))
    row = json.loads(credits[0].read_text().splitlines()[0])
    assert row["resourceName"] == "HR Bot" and row["periodFrom"] == "2026-10-01"
    assert list(out_dir.glob("currency_allocations/snapshot_date=*/currency_allocations.csv"))
    manifest = json.loads(next(out_dir.glob("_runs/*.json")).read_text())
    assert manifest["datasets"]["agents"] == 1 and manifest["errors"] == {}
    assert "audit" not in manifest["collectors"]


def test_cli_app_only_skips_inventory(token_env, monkeypatch):
    monkeypatch.setattr("power_admin.auth.TokenProvider.get_token", lambda self, scope: "t")
    session = FakeSession([("GET", "/environmentmanagement/environments", [EMPTY])])
    monkeypatch.setattr("power_admin.http.requests.Session", lambda: session)
    result = CliRunner().invoke(
        cli.app,
        ["pull", "-o", str(token_env / "o"), "--auth", "client-secret", "--only", "inventory", "--only", "environments",
         "-t", "t", "-c", "c", "--client-secret", "s"],
    )
    assert result.exit_code == 1
    assert "needs a signed-in user" in result.output
    assert not session.urls("/resourcequery")


def test_delegated_mode_defaults_to_powershell_client(monkeypatch, tmp_path):
    monkeypatch.setattr("power_admin.auth.TOKEN_CACHE_FILE", tmp_path / "cache.json")
    from power_admin.auth import DEFAULT_PUBLIC_CLIENT_ID

    seen = {}

    class FakePCA:
        def __init__(self, client_id, authority, token_cache):
            seen["client_id"], seen["authority"] = client_id, authority

    monkeypatch.setattr("power_admin.auth.msal.PublicClientApplication", FakePCA)
    TokenProvider(Settings(auth="interactive", tenant_id="contoso.com"))._client()
    assert seen == {"client_id": DEFAULT_PUBLIC_CLIENT_ID, "authority": "https://login.microsoftonline.com/contoso.com"}
