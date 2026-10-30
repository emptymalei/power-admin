# power-admin

A small CLI that signs in to your Microsoft tenant and pulls Power Platform admin data into dated snapshots. You can save to a local folder or to S3.

> Community tool. Not affiliated with or endorsed by Microsoft.

Run `power-admin datasets` to see every collector. Each one writes one or more datasets:

| Collector | Datasets | What's in them | Source |
| --- | --- | --- | --- |
| `environments` | `environments` | Environments with type, region and Dataverse URL | Power Platform API |
| `inventory` | `inventory`, `agents`, `apps`, `flows`, `connectors`, `environment_groups`, `inventory_environments` | Every app, flow, Copilot Studio agent and connector, with owner, dates and connectors used, joined to its environment | Power Platform inventory API |
| `capacity` | `capacity`, `capacity_raw`, `currency` | Storage and API capacity, plus purchased, allocated and consumed amounts per currency (Copilot credits, AI Builder and others) | Power Platform API, licensing |
| `allocations` | `currency_allocations`, `billing_policies` | Credits allocated to each environment, the enforcement rules (alert, deny, pay-as-you-go), and billing policies | Power Platform API, licensing |
| `dataverse` | `dataverse_agents`, `agent_components`, `agent_sessions`, `agent_sessions_daily` | **Agent details** (state, publish date, authentication, language), each agent's topics, tools and knowledge sources, and **activity history**: one row per conversation session, plus daily session counts per agent | Each environment's Dataverse (`bot`, `botcomponent`, `conversationtranscript` tables) |
| `credits` | `agent_credits`, `agent_credits_summary`, `user_credits`, `credit_license_trends` | **Credit usage history**: Copilot credits consumed per agent and per user for each month (or day) in the date range, a per-agent total, and license and capacity trends | Power Platform API, licensing insights |
| `advisor` | `advisor_recommendations`, `advisor_resources` | The admin center's **Actions** recommendations (for example unused apps or ownerless resources) and every resource they affect, including unique users over the last 30 days | Power Platform API, analytics |
| `rbac` | `role_assignments` | Power Platform role assignments | Power Platform API, authorization (preview) |
| `users` | `users`, `licenses`, `user_licenses` | Entra users, the tenant's license SKUs, and which user has which SKU | Microsoft Graph |
| `admin_roles` | `admin_roles` | Members of each activated Entra admin role | Microsoft Graph |
| `flow_runs` *(opt-in)* | `cloud_flows`, `flow_runs` | Cloud flows per environment and their run history | Power Platform API, Power Automate |
| `audit` *(opt-in)* | `audit_events` | Purview audit events for Power Apps, Power Automate, Power Pages and admin actions, such as app launches, flow edits and DLP changes | Microsoft Graph audit log query API |
| `sign_ins` *(opt-in)* | `user_sign_ins` | Last sign-in date per user | Microsoft Graph |

Opt-in collectors are slow or need extra permissions. Turn them on with `--include audit` or `include: [audit]` in the config.

## Install

You need [uv](https://docs.astral.sh/uv/). In PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   # if you don't have uv
uvx power-admin --help          # run without installing (once published to PyPI)
uv tool install power-admin     # or put `power-admin` on your PATH
# from a local checkout: uv tool install .
```

## Setup

You don't need your own Entra app. For `interactive` and `device-code` sign-in, power-admin uses the public client ID of Microsoft's Azure PowerShell app (`1950a258-227b-4e31-a9cf-717495945fc2`), which is the same one `Connect-AzAccount` uses. That app is already pre-authorized for the Power Platform API and Microsoft Graph, and it doesn't need an Azure subscription. Data access still comes from your own admin roles.

```powershell
power-admin configure --tenant-id contoso.onmicrosoft.com --output C:\power-admin
```

Your account needs the Power Platform Administrator role (or Global Administrator / Global Reader) so it can see the whole tenant. Graph reads need a role that can read users, such as Global Reader or User Administrator.

### Optional: use your own app registration

Use your own app if your tenant blocks the Azure PowerShell client through Conditional Access, if you want consent scoped to exactly these permissions, or if you need app-only auth.

1. Go to **Entra admin center > App registrations > New registration**. Choose **single tenant**.
2. Under **Authentication**, add the platform **Mobile and desktop applications** with the redirect URI `http://localhost`. If you'll use device-code sign-in, also set **Allow public client flows** to **Yes**.
3. Under **API permissions**, add the following **delegated** permissions, then click **Grant admin consent**:
   - **Power Platform API** (`8578e004-a5c6-46e7-913e-12f58912df43`): `ResourceQuery.Resources.Read`, `EnvironmentManagement.Environments.Read`, `Licensing.Allocations.Read`.
     If the API doesn't show up in the list, run `New-MgServicePrincipal -AppId 8578e004-a5c6-46e7-913e-12f58912df43` once, using Microsoft Graph PowerShell.
   - **Microsoft Graph**: `User.Read.All`, `Organization.Read.All`.
4. Pass `--client-id <app-id>` to `power-admin configure`.

### Or reuse an existing PowerShell sign-in

If you're already signed in with the Az module, you can hand those tokens to power-admin:

```powershell
Connect-AzAccount -Tenant contoso.onmicrosoft.com   # warns that there are no subscriptions, which is fine
$pp = Get-AzAccessToken -ResourceUrl "https://api.powerplatform.com" -AsSecureString
$gr = Get-AzAccessToken -ResourceTypeName MSGraph -AsSecureString
$env:POWER_ADMIN_PP_TOKEN    = $pp.Token | ConvertFrom-SecureString -AsPlainText
$env:POWER_ADMIN_GRAPH_TOKEN = $gr.Token | ConvertFrom-SecureString -AsPlainText
power-admin pull --auth token
```

Tokens last about an hour.

## Use

```powershell
power-admin login                       # opens a browser; the sign-in is cached in ~/.power-admin
power-admin pull                        # everything, credits from the 1st of last month to today
power-admin pull -o s3://my-bucket/power-platform --aws-profile admin
power-admin pull --only credits --from 2026-01-01   # credit history, one row per agent per month
power-admin pull --include audit --include flow_runs
power-admin pull -e <environment-id>    # limit per-environment collectors (dataverse, flow_runs)
power-admin pull --skip users -f csv    # CSV only
power-admin pull -C power-admin.yaml -p monthly     # reuse a saved profile
power-admin datasets                    # list what can be pulled
power-admin status                      # show settings and who's signed in
power-admin logout
```

Output layout. It's the same for a local folder and for S3, and it's partitioned so that Athena and Glue can read it directly:

```
<output>/agents/snapshot_date=2026-10-30/agents.jsonl
<output>/agents/snapshot_date=2026-10-30/agents.csv
<output>/agent_credits/snapshot_date=2026-10-30/agent_credits.csv
<output>/_runs/20261030T212400Z.json      # row counts and any errors for the run
```

Every run adds a new `snapshot_date`, so you build up history. If any dataset fails, the run still saves the rest, records the error in the manifest, and exits with code 1.

S3 uploads use the normal AWS credential chain, or the profile you pass with `--aws-profile`. Run `aws sso login --profile admin` first if you use SSO.

## Config files and profiles

All settings can live in a YAML file, so you can reuse them across runs. Run `power-admin init-config` to write a commented template to `power-admin.yaml`.

```yaml
tenant_id: contoso.onmicrosoft.com
output: ./power-admin-output
include: [audit]
credit_granularity: month        # month | day | none
transcript_content: false        # true also saves each session's conversation text

profiles:
  monthly:
    output: s3://my-bucket/power-platform
    aws_profile: admin
  automation:
    extends: monthly             # inherit everything from monthly
    auth: client-secret
    client_id: 00000000-0000-0000-0000-000000000000
```

- **Where the file is read from:** `-C file.yaml`, or `POWER_ADMIN_CONFIG`, or `~/.power-admin/config.yaml`.
- **Picking a profile:** `-p NAME` or `POWER_ADMIN_PROFILE`.
- **Precedence:** top-level keys < the profile (and whatever it `extends`) < `POWER_ADMIN_<KEY>` environment variables < command-line flags.
- **Checking the result:** `power-admin show-config -p monthly` prints the merged settings.
- **Saving from the command line:** `power-admin configure --profile monthly --output s3://...` writes to the file.
- **Secrets are never written to the file.** Pass a client secret through `POWER_ADMIN_CLIENT_SECRET`.

## Auth modes

Pick a mode with `--auth` / `-a`, or with `auth:` in the config file, or with `POWER_ADMIN_AUTH`.

| Mode | When to use | Needs |
| --- | --- | --- |
| `interactive` (default) | You, at your desk. Opens a browser. | `--tenant-id` (`--client-id` is optional) |
| `device-code` | Remote or SSH shells, or when a browser can't open. You enter a code at microsoft.com/devicelogin. | Same (with your own app, also turn on **Allow public client flows**) |
| `client-secret` | Unattended runs | `--tenant-id`, `--client-id`, `POWER_ADMIN_CLIENT_SECRET` |
| `certificate` | Unattended runs without secrets | `--certificate-path` (PEM private key) and `--certificate-thumbprint` |
| `token` | You already signed in with `Connect-AzAccount` or another tool | `POWER_ADMIN_PP_TOKEN` and `POWER_ADMIN_GRAPH_TOKEN` env vars |

How the app-only modes (`client-secret`, `certificate`) differ:
- **The inventory API rejects app-only tokens.** Microsoft only supports signed-in users there, so in these modes `inventory` is skipped and listed in the manifest.
- **The service principal needs extra access.** It must be given a Power Platform RBAC role (see Microsoft's guide *Assign RBAC roles to service principals*), and for `users` it needs the Graph *application* permissions `User.Read.All` and `Organization.Read.All`.

For monthly manual runs, `interactive` is the simplest option.

## Permissions per collector

The default sign-in (the Azure PowerShell client) covers the Power Platform API, Graph users and licenses, and Dataverse. The collectors that need more:
- **`dataverse`**: your account needs read access to each environment's Dataverse, for example the System Administrator role there. Environments you can't read are skipped and logged. Sessions come from the ConversationTranscript table, which keeps 30 days by default unless you've extended retention.
- **`admin_roles`**: needs `RoleManagement.Read.Directory` or `Directory.Read.All` (Global Reader works).
- **`audit`**: needs `AuditLogsQuery.Read.All`, which the default client usually doesn't have. Register your own app with that delegated Graph permission and pass `--client-id`. Each query runs on Microsoft's side and can take several minutes.
- **`sign_ins`**: needs `AuditLog.Read.All` and Entra ID P1.

## Caveats

- **The credit history endpoints may change or fail.** The per-agent and per-user endpoints are marked *deprecated* in the Power Platform API reference but still documented. The entitlement ID `MCSMessages` (Copilot credits) is our best reading of the docs and hasn't been tested against a live tenant yet. If you get a 400 or 404, set `credit_entitlements` to other values, for example `[MCSMessages, MCSSessions]`. Whatever happens, the admin center's **Licensing > Copilot Studio > Download report** remains the authoritative source.
- **Several APIs are still in preview** (licensing, RBAC, advisor), so their field names may change. JSONL keeps the full raw response; CSV flattens nested fields into `a.b.c` columns.
- **Some usage metrics are missing.** The admin center's Usage page (per-app users) has no public API. The closest stand-ins are `advisor_resources.resourceUsage`, `audit` app-launch events, `agent_sessions` and `flow_runs`.

## Querying in Athena (optional)

```sql
CREATE EXTERNAL TABLE agent_credits (
  resourceId string, resourceName string, environmentId string, environmentName string,
  consumed double, unit string, lastRefreshedDate string, periodFrom string, periodTo string
)
PARTITIONED BY (snapshot_date string)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
LOCATION 's3://my-bucket/power-platform/agent_credits/';
MSCK REPAIR TABLE agent_credits;
```

## Develop

```bash
uv sync
uv run pytest
```

## Releasing

CI runs the tests on every push and pull request. Publishing a GitHub release whose tag is `v<version>` (for example `v0.2.0`, matching `version` in `pyproject.toml`) builds the package and uploads it to PyPI.

The release uses PyPI trusted publishing, so no token is stored in GitHub. One-time setup:
1. Add a pending publisher on PyPI under **Your account > Publishing**, with project `power-admin`, owner `emptymalei`, repository `power-admin`, workflow `release.yml` and environment `pypi`.
2. In the GitHub repo, open **Settings > Environments** and create an environment called `pypi`.
