# power-admin

A small CLI that signs in to your Microsoft tenant and pulls Power Platform admin data into dated snapshots. You can save to a local folder or to S3.

> Community tool. Not affiliated with or endorsed by Microsoft.

| Dataset | What's in it | Source |
| --- | --- | --- |
| `environments` | Environments, their type and region, Dataverse details | Power Platform API `environmentmanagement` |
| `inventory`, `agents`, `apps`, `flows` | Every canvas, model-driven and code app, every cloud and agent flow, and every Copilot Studio agent, joined to its environment | Power Platform inventory API (`PowerPlatformResources`) |
| `capacity`, `capacity_raw`, `currency` | Tenant storage and API capacity, plus purchased, allocated and consumed amounts per currency (Copilot credits, AI Builder and others) | Power Platform API `licensing/tenantCapacity` |
| `agent_credits` | Copilot credit consumption per agent and environment for a date range, with agent names attached | Power Platform API `licensing/entitlements/{id}/resources` |
| `users`, `licenses`, `user_licenses` | Entra users, the tenant's license SKUs, and which user has which SKU | Microsoft Graph |

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
power-admin pull --only credits --from 2026-10-01 --to 2026-10-31
power-admin pull --skip users -f csv    # CSV only
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

## Auth modes

Pick a mode with `--auth` / `-a`, or set it in `power-admin configure` or with `POWER_ADMIN_AUTH_MODE`.

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

## Caveats

- **The per-agent credit endpoints may change or fail.** They are marked *deprecated* in the Power Platform API reference but still documented. The entitlement ID `MCSMessages` (Copilot credits) is our best reading of the docs and hasn't been tested against a live tenant yet; if you get a 400 or 404, try `--credit-entitlement` with another value. Whatever happens, the admin center's **Licensing > Copilot Studio > Download report** remains the authoritative source.
- **Several licensing APIs are still in preview**, so their field names may change. JSONL keeps the full raw response; CSV flattens nested fields into `a.b.c` columns.
- **Usage metrics are missing.** Per-app users, per-flow runs and per-agent sessions from the admin center's Usage page don't have a public API yet.

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

CI runs the tests on every push and pull request. Publishing a GitHub release whose tag is `v<version>` (for example `v0.1.0`, matching `version` in `pyproject.toml`) builds the package and uploads it to PyPI.

The release uses PyPI trusted publishing, so no token is stored in GitHub. One-time setup:
1. Add a pending publisher on PyPI under **Your account > Publishing**, with project `power-admin`, owner `emptymalei`, repository `power-admin`, workflow `release.yml` and environment `pypi`.
2. In the GitHub repo, open **Settings > Environments** and create an environment called `pypi`.
