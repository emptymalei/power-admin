"""Collectors: each pulls one area of admin data and returns {dataset_name: rows}."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable

from .config import GRAPH_API, GRAPH_SCOPE, POWER_PLATFORM_API, PP_SCOPE
from .http import ApiClient

PP_API_VERSION = "2024-10-01"

# Resource types in the PowerPlatformResources inventory table.
INVENTORY_TYPES = {
    "microsoft.powerapps/canvasapps": "app",
    "microsoft.powerapps/modeldrivenapps": "app",
    "microsoft.powerapps/codeapps": "app",
    "microsoft.powerapps/apps": "app",
    "microsoft.powerautomate/cloudflows": "flow",
    "microsoft.powerautomate/agentflows": "flow",
    "microsoft.powerautomate/m365agentflows": "flow",
    "microsoft.copilotstudio/agents": "agent",
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
    credit_entitlement: str = "MCSMessages"


@dataclass
class Collector:
    name: str
    description: str
    run: Callable[[PullContext, dict[str, list[dict]]], dict[str, list[dict]]]
    delegated_only: bool = False


def _pp(path: str) -> str:
    sep = "&" if "?" in path else "?"
    return f"{POWER_PLATFORM_API}{path}{sep}api-version={PP_API_VERSION}"


def collect_environments(ctx: PullContext, _: dict) -> dict[str, list[dict]]:
    rows = list(ctx.client.iter_odata(_pp("/environmentmanagement/environments"), PP_SCOPE))
    return {"environments": rows}


def inventory_query(skip_token: str | None = None) -> dict:
    """All apps, flows and agents joined with their environment (the admin center's default query)."""
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
    out: dict[str, list[dict]] = {"inventory": rows, "agents": [], "apps": [], "flows": []}
    for row in rows:
        kind = INVENTORY_TYPES.get(str(row.get("type", "")).lower())
        if kind:
            out[kind + "s"].append(row)
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


def collect_credits(ctx: PullContext, so_far: dict) -> dict[str, list[dict]]:
    """Copilot credit consumption per resource (agent) across all environments."""
    base = _pp(f"/licensing/entitlements/{ctx.credit_entitlement}/resources")
    params: dict = {"fromDate": ctx.date_from.isoformat(), "toDate": ctx.date_to.isoformat()}
    rows: list[dict] = []
    while True:
        page = ctx.client.get_json(base, PP_SCOPE, params=params) or {}
        rows.extend(page.get("value", []))
        token = page.get("continuationtoken") or page.get("continuationToken")
        if not token:
            break
        params = {**params, "continuationToken": token}

    # Attach agent names when inventory ran in the same pull.
    names = {
        str(a.get("name", "")).lower(): (a.get("properties") or {}).get("displayName") for a in so_far.get("agents", [])
    }
    envs = {
        str(e.get("id", e.get("name", ""))).lower(): e.get("displayName") or (e.get("properties") or {}).get("displayName")
        for e in so_far.get("environments", [])
    }
    for row in rows:
        row["resourceName"] = names.get(str(row.get("resourceId", "")).lower())
        row["environmentName"] = envs.get(str(row.get("environmentId", "")).lower())
        row["periodFrom"] = ctx.date_from.isoformat()
        row["periodTo"] = ctx.date_to.isoformat()
    return {"agent_credits": rows}


def collect_users(ctx: PullContext, _: dict) -> dict[str, list[dict]]:
    users = list(
        ctx.client.iter_odata(
            f"{GRAPH_API}/v1.0/users", GRAPH_SCOPE, params={"$select": GRAPH_USER_FIELDS, "$top": "999"}
        )
    )
    skus = list(ctx.client.iter_odata(f"{GRAPH_API}/v1.0/subscribedSkus", GRAPH_SCOPE))
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


# Order matters: credits uses names from inventory and environments.
COLLECTORS: dict[str, Collector] = {
    c.name: c
    for c in [
        Collector("environments", "Environments with type, region, Dataverse and capacity details", collect_environments),
        Collector(
            "inventory",
            "All apps, flows and Copilot Studio agents (also split into agents/apps/flows)",
            collect_inventory,
            delegated_only=True,
        ),
        Collector("capacity", "Tenant storage/API capacity and currency (credit) reports", collect_capacity),
        Collector("credits", "Copilot credit consumption per agent for the date range", collect_credits),
        Collector("users", "Entra users, tenant license SKUs, and per-user license assignments", collect_users),
    ]
}
