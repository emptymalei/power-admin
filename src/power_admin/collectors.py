"""Collectors: each pulls one area of admin data and returns {dataset_name: rows}.

Collectors marked default=False are opt-in (`--include NAME` or `include:` in the config)
because they are slow, per-environment, or need extra permissions.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterator

from .config import GRAPH_API, GRAPH_SCOPE, POWER_PLATFORM_API, PP_SCOPE, Settings
from .http import ApiClient, ApiError

PP_API_VERSION = "2024-10-01"
DATAVERSE_API = "api/data/v9.2"

# Resource types in the PowerPlatformResources inventory table, mapped to split datasets.
INVENTORY_TYPES = {
    "microsoft.powerapps/canvasapps": "apps",
    "microsoft.powerapps/modeldrivenapps": "apps",
    "microsoft.powerapps/codeapps": "apps",
    "microsoft.powerapps/apps": "apps",
    "microsoft.powerautomate/cloudflows": "flows",
    "microsoft.powerautomate/agentflows": "flows",
    "microsoft.powerautomate/m365agentflows": "flows",
    "microsoft.copilotstudio/agents": "agents",
    "microsoft.powerplatformconnector/connectors": "connectors",
    "microsoft.powerplatform/environments": "inventory_environments",
    "microsoft.powerplatform/environmentgroups": "environment_groups",
}

GRAPH_USER_FIELDS = (
    "id,displayName,userPrincipalName,mail,department,jobTitle,companyName,"
    "officeLocation,country,accountEnabled,userType,createdDateTime,assignedLicenses"
)


@dataclass
class PullContext:
    client: ApiClient
    date_from: date
    date_to: date
    settings: Settings = field(default_factory=Settings)
    log: Callable[[str], None] = lambda msg: None
    sleep: Callable[[float], None] = time.sleep


@dataclass
class Collector:
    name: str
    description: str
    run: Callable[[PullContext, dict[str, list[dict]]], dict[str, list[dict]]]
    delegated_only: bool = False
    default: bool = True
    needs: str = ""  # extra permissions worth mentioning


def _pp(path: str) -> str:
    sep = "&" if "?" in path else "?"
    return f"{POWER_PLATFORM_API}{path}{sep}api-version={PP_API_VERSION}"


def _pages(
    client: ApiClient, url: str, scope: str, params: dict | None = None, headers: dict | None = None
) -> Iterator[dict]:
    """Follow @odata.nextLink / nextLink, and accept bare-array responses."""
    next_url: str | None = url
    while next_url:
        page = client.get_json(next_url, scope, params=params, headers=headers)
        params = None
        if page is None:
            return
        if isinstance(page, list):
            yield from page
            return
        yield from page.get("value", [])
        next_url = page.get("@odata.nextLink") or page.get("nextLink")


def _env_id(env: dict) -> str:
    return str(env.get("id") or env.get("name") or "").split("/")[-1]


def find_dataverse_url(obj) -> str | None:
    """Find the Dataverse org URL (https://org.crm*.dynamics.com) anywhere in an environment record."""
    if isinstance(obj, str):
        if obj.startswith("https://") and ".dynamics.com" in obj and "api." not in obj:
            return obj.rstrip("/")
        return None
    if isinstance(obj, dict):
        for key in ("instanceUrl", "url", "dataverseUrl", "orgUrl"):
            hit = find_dataverse_url(obj.get(key))
            if hit:
                return hit
        obj = list(obj.values())
    if isinstance(obj, list):
        for item in obj:
            hit = find_dataverse_url(item)
            if hit:
                return hit
    return None


def _target_environments(ctx: PullContext, so_far: dict) -> list[dict]:
    envs = so_far.get("environments")
    if envs is None:
        envs = list(_pages(ctx.client, _pp("/environmentmanagement/environments"), PP_SCOPE))
        so_far["environments"] = envs
    wanted = {e.lower() for e in ctx.settings.environments}
    return [e for e in envs if not wanted or _env_id(e).lower() in wanted]


# --- Power Platform API -------------------------------------------------------------


def collect_environments(ctx: PullContext, _: dict) -> dict[str, list[dict]]:
    envs = list(_pages(ctx.client, _pp("/environmentmanagement/environments"), PP_SCOPE))
    for env in envs:
        env["dataverseUrl"] = find_dataverse_url(env)
    return {"environments": envs}


def inventory_query(skip_token: str | None = None) -> dict:
    """All inventory resources joined with their environment (the admin center's default query)."""
    return {
        "TableName": "PowerPlatformResources",
        "Options": {"Top": 1000, "Skip": 0, "SkipToken": skip_token or ""},
        "Clauses": [
            {"$type": "extend", "FieldName": "joinKey", "Expression": "tolower(tostring(properties.environmentId))"},
            {
                "$type": "join",
                "JoinKind": "leftouter",
                "RightTable": {
                    "TableName": "PowerPlatformResources",
                    "Clauses": [
                        {
                            "$type": "where",
                            "FieldName": "type",
                            "Operator": "==",
                            "Values": ["'microsoft.powerplatform/environments'"],
                        },
                        {
                            "$type": "project",
                            "FieldList": [
                                "joinKey = tolower(name)",
                                "environmentName = properties.displayName",
                                "environmentRegion = location",
                                "environmentType = properties.environmentType",
                                "isManagedEnvironment = properties.isManaged",
                            ],
                        },
                    ],
                },
                "LeftColumnName": "joinKey",
                "RightColumnName": "joinKey",
            },
            {
                "$type": "where",
                "FieldName": "type",
                "Operator": "in~",
                "Values": [f"'{t}'" for t in INVENTORY_TYPES],
            },
        ],
    }


def collect_inventory(ctx: PullContext, _: dict) -> dict[str, list[dict]]:
    url = _pp("/resourcequery/resources/query")
    rows: list[dict] = []
    skip_token = None
    while True:
        page = ctx.client.post_json(url, PP_SCOPE, inventory_query(skip_token)) or {}
        rows.extend(page.get("data", []))
        skip_token = page.get("skipToken")
        if not skip_token:
            break
    out: dict[str, list[dict]] = {"inventory": rows, **{name: [] for name in set(INVENTORY_TYPES.values())}}
    for row in rows:
        target = INVENTORY_TYPES.get(str(row.get("type", "")).lower())
        if target:
            out[target].append(row)
    return out


def collect_capacity(ctx: PullContext, _: dict) -> dict[str, list[dict]]:
    details = ctx.client.get_json(_pp("/licensing/tenantCapacity"), PP_SCOPE) or {}
    rows = []
    for cap in details.get("tenantCapacities", []):
        consumption = cap.get("consumption") or {}
        rows.append(
            {
                "capacityType": cap.get("capacityType"),
                "capacityUnits": cap.get("capacityUnits"),
                "status": cap.get("status"),
                "totalCapacity": cap.get("totalCapacity"),
                "maxCapacity": cap.get("maxCapacity"),
                "consumedActual": consumption.get("actual"),
                "consumedRated": consumption.get("rated"),
                "consumedUpdatedOn": consumption.get("actualUpdatedOn"),
                "capacityEntitlements": cap.get("capacityEntitlements"),
            }
        )
    currency = ctx.client.get_json(
        _pp("/licensing/tenantCapacity/currencyReports?includeAllocations=true&includeConsumptions=true"), PP_SCOPE
    )
    return {"capacity": rows, "capacity_raw": [details], "currency": currency or []}


def collect_allocations(ctx: PullContext, _: dict) -> dict[str, list[dict]]:
    """Credit/currency allocations and enforcement rules per environment, plus pay-as-you-go billing policies."""
    by_env = ctx.client.get_json(_pp("/licensing/allocationsByEnvironment"), PP_SCOPE) or []
    rows = [
        {"environmentId": env.get("environmentId"), **alloc}
        for env in by_env
        for alloc in env.get("currencyAllocations") or []
    ]
    policies = list(_pages(ctx.client, _pp("/licensing/billingPolicies"), PP_SCOPE))
    return {"currency_allocations": rows, "billing_policies": policies}


def date_windows(start: date, end: date, granularity: str) -> list[tuple[date, date]]:
    """Split [start, end] into calendar months, single days, or one window."""
    if granularity == "none" or start > end:
        return [(start, end)]
    windows = []
    cur = start
    while cur <= end:
        if granularity == "day":
            stop = cur
        else:
            next_month = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
            stop = min(end, next_month - timedelta(days=1))
        windows.append((cur, stop))
        cur = stop + timedelta(days=1)
    return windows


def _paged_continuation(ctx: PullContext, url: str, params: dict) -> Iterator[dict]:
    while True:
        page = ctx.client.get_json(url, PP_SCOPE, params=params) or {}
        yield from page.get("value", [])
        token = page.get("continuationtoken") or page.get("continuationToken")
        if not token:
            return
        params = {**params, "continuationToken": token}


def collect_credits(ctx: PullContext, so_far: dict) -> dict[str, list[dict]]:
    """Copilot credit usage history per agent and per user, split by month (or day), plus license trends."""
    agent_names = {
        str(a.get("name", "")).lower(): (a.get("properties") or {}).get("displayName") for a in so_far.get("agents", [])
    }
    agent_names.update({str(b.get("botid", "")).lower(): b.get("name") for b in so_far.get("dataverse_agents", [])})
    env_names = {
        _env_id(e).lower(): e.get("displayName") or (e.get("properties") or {}).get("displayName")
        for e in so_far.get("environments", [])
    }
    agents: list[dict] = []
    users: list[dict] = []
    trends: list[dict] = []
    for entitlement in ctx.settings.credit_entitlements:
        base = f"/licensing/entitlements/{entitlement}"
        for w_from, w_to in date_windows(ctx.date_from, ctx.date_to, ctx.settings.credit_granularity):
            period = {"entitlement": entitlement, "periodFrom": w_from.isoformat(), "periodTo": w_to.isoformat()}
            params = {"fromDate": w_from.isoformat(), "toDate": w_to.isoformat()}
            for row in _paged_continuation(ctx, _pp(f"{base}/resources"), params):
                row.update(period)
                row["resourceName"] = agent_names.get(str(row.get("resourceId", "")).lower())
                row["environmentName"] = env_names.get(str(row.get("environmentId", "")).lower())
                agents.append(row)
            for page in _paged_continuation(ctx, _pp(f"{base}/users"), params):
                for row in page.get("users") or []:
                    row.update(period)
                    row["environmentName"] = env_names.get(str(row.get("environmentId", "")).lower())
                    users.append(row)
        params = {"fromDate": ctx.date_from.isoformat(), "toDate": ctx.date_to.isoformat()}
        try:
            for row in _pages(ctx.client, _pp(f"{base}/licenses"), PP_SCOPE, params=params):
                trends.append({"entitlement": entitlement, **row})
        except ApiError as exc:
            ctx.log(f"  license trends for {entitlement}: {exc.status}")

    totals: dict[tuple, float] = {}
    for row in agents:
        key = (
            row["entitlement"],
            str(row.get("environmentId") or "").lower(),
            str(row.get("resourceId") or "").lower(),
            row.get("resourceName"),
        )
        totals[key] = totals.get(key, 0.0) + float(row.get("consumed") or 0)
    summary = [
        {
            "entitlement": k[0],
            "environmentId": k[1],
            "resourceId": k[2],
            "resourceName": k[3],
            "consumed": v,
            "periodFrom": ctx.date_from.isoformat(),
            "periodTo": ctx.date_to.isoformat(),
        }
        for k, v in sorted(totals.items(), key=lambda kv: -kv[1])
    ]
    return {
        "agent_credits": agents,
        "agent_credits_summary": summary,
        "user_credits": users,
        "credit_license_trends": trends,
    }


def collect_advisor(ctx: PullContext, _: dict) -> dict[str, list[dict]]:
    """Admin center 'Actions' recommendations and every affected resource (with 30-day unique users)."""
    recs = list(_pages(ctx.client, _pp("/analytics/advisorRecommendations"), PP_SCOPE))
    resources: list[dict] = []
    for rec in recs:
        scenario = rec.get("scenario")
        if not scenario:
            continue
        for res in _pages(ctx.client, _pp(f"/analytics/advisorRecommendations/{scenario}/resources"), PP_SCOPE):
            resources.append({"scenario": scenario, **res})
    return {"advisor_recommendations": recs, "advisor_resources": resources}


def collect_rbac(ctx: PullContext, _: dict) -> dict[str, list[dict]]:
    """Power Platform RBAC role assignments (preview API)."""
    return {"role_assignments": list(_pages(ctx.client, _pp("/authorization/roleAssignments"), PP_SCOPE))}


def collect_flow_runs(ctx: PullContext, so_far: dict) -> dict[str, list[dict]]:
    """Cloud flows and their run history per Dataverse environment. Slow: one call per flow."""
    flows_out: list[dict] = []
    runs_out: list[dict] = []
    for env in _target_environments(ctx, so_far):
        env_id = _env_id(env)
        try:
            flows = list(_pages(ctx.client, _pp(f"/powerautomate/environments/{env_id}/cloudFlows"), PP_SCOPE))
        except ApiError as exc:
            if exc.status in (403, 404):  # no Dataverse, or no access
                continue
            raise
        for flow in flows:
            flow["environmentId"] = env_id
        flows_out.extend(flows)
        for flow in flows[: ctx.settings.max_flows_per_environment]:
            workflow_id = flow.get("workflowId") or flow.get("workflowid") or flow.get("id")
            if not workflow_id:
                continue
            url = _pp(f"/powerautomate/environments/{env_id}/flowRuns?workflowId={workflow_id}")
            for run in _pages(ctx.client, url, PP_SCOPE):
                runs_out.append({"environmentId": env_id, "workflowId": workflow_id, **run})
        ctx.log(f"  {env_id}: {len(flows)} flows")
    return {"cloud_flows": flows_out, "flow_runs": runs_out}


# --- Dataverse (per environment) ----------------------------------------------------

FORMATTED = "OData.Community.Display.V1.FormattedValue"
# Ask Dataverse to also return display labels for option sets (e.g. component type "Topic").
DATAVERSE_HEADERS = {"Prefer": f'odata.include-annotations="{FORMATTED}"', "OData-MaxVersion": "4.0", "OData-Version": "4.0"}

TRANSCRIPT_COLUMNS = (
    "conversationtranscriptid,name,conversationstarttime,createdon,modifiedon,"
    "_bot_conversationtranscriptid_value,schematype,schemaversion,metadata"
)



def _dataverse_rows(ctx: PullContext, org_url: str, entity: str, query: str) -> list[dict]:
    url = f"{org_url}/{DATAVERSE_API}/{entity}?{query}"
    return list(_pages(ctx.client, url, f"{org_url}/.default", headers=DATAVERSE_HEADERS))


def collect_dataverse(ctx: PullContext, so_far: dict) -> dict[str, list[dict]]:
    """Copilot Studio agents and conversation sessions from each environment's Dataverse.

    Sessions come from the ConversationTranscript table (kept 30 days by default), so they give
    per-agent session counts. Transcript content is not downloaded.
    """
    bots_out: list[dict] = []
    components_out: list[dict] = []
    sessions_out: list[dict] = []
    start = f"{ctx.date_from.isoformat()}T00:00:00Z"
    end = f"{ctx.date_to.isoformat()}T23:59:59Z"
    for env in _target_environments(ctx, so_far):
        org_url = env.get("dataverseUrl") or find_dataverse_url(env)
        if not org_url:
            continue
        env_id = _env_id(env)
        try:
            bots = _dataverse_rows(
                ctx,
                org_url,
                "bots",
                "$select=botid,name,schemaname,statecode,publishedon,createdon,modifiedon,language,authenticationmode,_ownerid_value",
            )
            components = _dataverse_rows(
                ctx,
                org_url,
                "botcomponents",
                "$select=botcomponentid,name,schemaname,componenttype,category,description,statecode,createdon,modifiedon,_parentbotid_value",
            )
            columns = TRANSCRIPT_COLUMNS + (",content" if ctx.settings.transcript_content else "")
            sessions = _dataverse_rows(
                ctx,
                org_url,
                "conversationtranscripts",
                f"$select={columns}&$filter=conversationstarttime ge {start} and conversationstarttime le {end}",
            )
        except ApiError as exc:
            ctx.log(f"  {env_id}: skipped ({exc.status})")
            continue
        names = {b.get("botid"): b.get("name") for b in bots}
        for b in bots:
            b.update(environmentId=env_id, dataverseUrl=org_url)
        for comp in components:
            bot_id = comp.get("_parentbotid_value")
            comp.update(
                environmentId=env_id,
                botId=bot_id,
                botName=names.get(bot_id),
                componentKind=comp.get(f"componenttype@{FORMATTED}"),
            )
        for s in sessions:
            bot_id = s.get("_bot_conversationtranscriptid_value")
            s.update(environmentId=env_id, botId=bot_id, botName=names.get(bot_id))
        bots_out.extend(bots)
        components_out.extend(components)
        sessions_out.extend(sessions)
        ctx.log(f"  {env_id}: {len(bots)} agents, {len(components)} components, {len(sessions)} sessions")

    daily = Counter(
        (s["environmentId"], s.get("botId"), s.get("botName"), str(s.get("conversationstarttime", ""))[:10])
        for s in sessions_out
    )
    sessions_daily = [
        {"environmentId": e, "botId": b, "botName": n, "date": d, "sessions": c} for (e, b, n, d), c in sorted(daily.items(), key=str)
    ]
    per_agent = Counter(c["botId"] for c in components_out)
    for b in bots_out:
        b["componentCount"] = per_agent.get(b.get("botid"), 0)
    return {
        "dataverse_agents": bots_out,
        "agent_components": components_out,
        "agent_sessions": sessions_out,
        "agent_sessions_daily": sessions_daily,
    }


# --- Microsoft Graph ----------------------------------------------------------------


def collect_users(ctx: PullContext, _: dict) -> dict[str, list[dict]]:
    users = list(
        _pages(ctx.client, f"{GRAPH_API}/v1.0/users", GRAPH_SCOPE, params={"$select": GRAPH_USER_FIELDS, "$top": "999"})
    )
    skus = list(_pages(ctx.client, f"{GRAPH_API}/v1.0/subscribedSkus", GRAPH_SCOPE))
    sku_names = {s.get("skuId"): s.get("skuPartNumber") for s in skus}
    user_licenses = [
        {
            "userId": u.get("id"),
            "userPrincipalName": u.get("userPrincipalName"),
            "skuId": lic.get("skuId"),
            "skuPartNumber": sku_names.get(lic.get("skuId")),
        }
        for u in users
        for lic in u.get("assignedLicenses") or []
    ]
    return {"users": users, "licenses": skus, "user_licenses": user_licenses}


def collect_admin_roles(ctx: PullContext, _: dict) -> dict[str, list[dict]]:
    """Members of activated Entra directory roles (Power Platform Administrator, Global Admin, ...)."""
    rows = []
    for role in _pages(ctx.client, f"{GRAPH_API}/v1.0/directoryRoles", GRAPH_SCOPE):
        for member in _pages(ctx.client, f"{GRAPH_API}/v1.0/directoryRoles/{role['id']}/members", GRAPH_SCOPE):
            rows.append(
                {
                    "roleId": role.get("id"),
                    "roleName": role.get("displayName"),
                    "roleTemplateId": role.get("roleTemplateId"),
                    "memberId": member.get("id"),
                    "memberType": member.get("@odata.type"),
                    "memberName": member.get("displayName"),
                    "userPrincipalName": member.get("userPrincipalName"),
                }
            )
    return {"admin_roles": rows}


def collect_sign_ins(ctx: PullContext, _: dict) -> dict[str, list[dict]]:
    """Last sign-in per user. Needs AuditLog.Read.All and Entra ID P1."""
    rows = list(
        _pages(
            ctx.client,
            f"{GRAPH_API}/v1.0/users",
            GRAPH_SCOPE,
            params={"$select": "id,userPrincipalName,signInActivity", "$top": "999"},
        )
    )
    return {"user_sign_ins": rows}


def collect_audit(ctx: PullContext, _: dict) -> dict[str, list[dict]]:
    """Power Platform events from the Purview unified audit log (app launches, flow edits, admin actions...)."""
    queries = f"{GRAPH_API}/v1.0/security/auditLog/queries"
    body = {
        "displayName": f"power-admin {datetime.now(timezone.utc):%Y-%m-%dT%H:%M}",
        "filterStartDateTime": f"{ctx.date_from.isoformat()}T00:00:00Z",
        "filterEndDateTime": f"{ctx.date_to.isoformat()}T23:59:59Z",
        "recordTypeFilters": ctx.settings.audit_record_types,
    }
    query = ctx.client.post_json(queries, GRAPH_SCOPE, body) or {}
    query_id = query.get("id")
    if not query_id:
        raise ApiError(0, queries, f"audit query was not created: {query}")
    deadline = time.monotonic() + ctx.settings.audit_timeout_minutes * 60
    status = query.get("status")
    while status not in ("succeeded", "failed", "cancelled"):
        if time.monotonic() > deadline:
            raise ApiError(0, queries, f"audit query {query_id} still {status} after {ctx.settings.audit_timeout_minutes} min")
        ctx.sleep(15)
        status = (ctx.client.get_json(f"{queries}/{query_id}", GRAPH_SCOPE) or {}).get("status")
        ctx.log(f"  audit query {status}...")
    if status != "succeeded":
        raise ApiError(0, queries, f"audit query {query_id} {status}")
    records = list(_pages(ctx.client, f"{queries}/{query_id}/records", GRAPH_SCOPE))
    return {"audit_events": records}


# Order matters: later collectors reuse environments/agents gathered earlier.
COLLECTORS: dict[str, Collector] = {
    c.name: c
    for c in [
        Collector("environments", "Environments with type, region and Dataverse URL", collect_environments),
        Collector(
            "inventory",
            "Every app, flow, agent, connector, environment and environment group (split per type)",
            collect_inventory,
            delegated_only=True,
        ),
        Collector("capacity", "Tenant storage/API capacity and currency (credit) balances", collect_capacity),
        Collector("allocations", "Credit allocations and enforcement per environment, billing policies", collect_allocations),
        Collector(
            "dataverse",
            "Per environment: agent details, topics/tools/knowledge, and session history from Dataverse",
            collect_dataverse,
            needs="read access to each environment's Dataverse",
        ),
        Collector(
            "credits",
            "Copilot credit usage history per agent and per user (monthly by default), license trends",
            collect_credits,
        ),
        Collector("advisor", "Admin center recommendations and affected resources (with 30-day users)", collect_advisor),
        Collector("rbac", "Power Platform RBAC role assignments (preview)", collect_rbac),
        Collector("users", "Entra users, license SKUs, per-user license assignments", collect_users),
        Collector("admin_roles", "Members of Entra admin roles", collect_admin_roles, needs="RoleManagement.Read.Directory"),
        Collector(
            "flow_runs",
            "Per environment: cloud flows and their run history",
            collect_flow_runs,
            default=False,
        ),
        Collector(
            "audit",
            "Purview audit log events for Power Apps, Power Automate and admin activity",
            collect_audit,
            default=False,
            needs="AuditLogsQuery.Read.All (usually your own app registration)",
        ),
        Collector(
            "sign_ins",
            "Last sign-in date per user",
            collect_sign_ins,
            default=False,
            needs="AuditLog.Read.All and Entra ID P1",
        ),
    ]
}


def select_collectors(settings: Settings) -> list[Collector]:
    unknown = [n for n in settings.only + settings.include + settings.skip if n not in COLLECTORS]
    if unknown:
        raise ValueError(f"Unknown dataset(s): {', '.join(unknown)}. See `power-admin datasets`.")
    if settings.only:
        chosen = set(settings.only)
    else:
        chosen = {n for n, c in COLLECTORS.items() if c.default} | set(settings.include)
    chosen -= set(settings.skip)
    return [c for n, c in COLLECTORS.items() if n in chosen]
