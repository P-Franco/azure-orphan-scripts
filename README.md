# Azure Orphan Resource Scripts

Identify and clean up orphaned Azure resources across all subscriptions in your tenant. Orphaned resources are those that are no longer attached to anything useful — they clutter your environment and many actively burn money.

## What It Finds

| Category | Cost? | Description |
|---|---|---|
| **App Service Plans** | ✅ | Plans with zero apps deployed |
| **Availability Sets** | ✅ | Sets with no VMs |
| **Public IPs** | ✅ | Unassociated (no NIC, gateway, or NAT) |
| **NICs** | ✅ | Not attached to a VM or private endpoint |
| **NSGs** | ✅ | Not associated with any subnet or NIC |
| **Load Balancers** | ✅ | Empty backend pools |
| **Application Gateways** | ✅ | Empty backend pools |
| **VNet Gateways** | ✅ | No connections |
| **Private Endpoints** | ✅ | Not connected to any resource |
| **Route Tables** | — | Not associated with any subnet |
| **NAT Gateways** | ✅ | Not associated with any subnet |
| **Front Door WAF Policies** | ✅ | Not linked to any Front Door |
| **Traffic Manager Profiles** | ✅ | No endpoints configured |
| **Virtual Networks** | — | No subnets |
| **Subnets** | — | No connected devices (excludes system subnets) |
| **IP Groups** | — | Not referenced by any firewall or policy |
| **Private DNS Zones** | — | No virtual network links |
| **DDoS Protection Plans** | ✅ | No associated virtual networks |
| **Managed Disks** | ✅ | Unattached (disk state = `Unattached`) |
| **SQL Elastic Pools** | ✅ | No databases |
| **Empty Resource Groups** | — | Zero resources inside |
| **Expired Certificates** | — | App Service certs past expiration |
| **API Connections** | — | Disconnected / not ready |
| **Stopped VMs** † | ✅ | Stopped but NOT deallocated — still billing full compute |
| **Deallocated VMs** † | ✅ | Compute free, but disks and reserved IPs still bill |
| **VM Images** † | ✅ | Not used by any existing VM or VMSS |
| **ExpressRoute Circuits** † | ✅ | Provider side never provisioned — full circuit fee for nothing |
| **Bastion Hosts** † | ✅ | In VNets with no VMs (verify peering before acting) |
| **Public IP Prefixes** † | ✅ | No IPs allocated from the prefix |
| **Disk Snapshots** † | ✅ | Older than 90 days, or source disk deleted |
| **Recovery Services Vaults** † | — | No backup or ASR protected items |
| **Backup Items** † | ✅ | Protecting resources that no longer exist |
| **AVD Application Groups** † | — | Not linked to any workspace |
| **User-Assigned Managed Identities** † | — | Not attached to any resource (security hygiene) |

† **Report-only categories** — `orphan_cleanup.py` will never delete these; they require human judgment (peering, backup policy, intentional dealloc).

Any resource tagged `DoNotDelete` (as a tag key or value) is excluded from **all** reports and cleanup automatically.

## Quick Start

```bash
pip install -r requirements.txt
az login
python3 orphan_report.py                    # Console report (default)
python3 orphan_report.py --format html       # Interactive HTML dashboard
python3 orphan_report.py --format json       # Machine-readable JSON
python3 orphan_cleanup.py                    # Dry-run cleanup (safe, no changes)
```

## Prerequisites

- **Python 3.10+**
- **Azure CLI** authenticated (`az login`) or any auth method supported by [DefaultAzureCredential](https://learn.microsoft.com/en-us/python/api/azure-identity/azure.identity.defaultazurecredential)

## Setup

```bash
# Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate    # Windows

# Install dependencies
pip install -r requirements.txt
```

## Cloud Shell (drop-in)

The `cloudshell/` folder is a self-contained bundle of the whole toolkit. Upload it to Azure Cloud Shell (or `git clone` the repo and `cd cloudshell`) and run everything through `run.sh`, which builds a venv, installs dependencies, and reuses the Cloud Shell portal session for auth — no `az login` needed.

```bash
chmod +x run.sh

./run.sh                                              # scan, console output
./run.sh --format excel --tenant <tenant-id>          # Excel workbook, tenant-scoped
./run.sh cleanup --tenant <id> --ids-file approved.txt           # preview deletions (dry-run)
./run.sh cleanup --tenant <id> --ids-file approved.txt --confirm # delete the approved list
./run.sh pptx --input scan.json --client "Acme"       # CIR PowerPoint deck
./run.sh vm-backup --tenant <id>                      # VM backup gap analysis
```

The bundle is kept byte-identical to the root scripts by a test (`test_cloudshell_copies_match_root`); re-run `cp <script> cloudshell/` and the suite will confirm there's no drift.

## Tests (offline, no Azure needed)

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

Everything Azure is mocked. The suite covers KQL regression guards (case-sensitive joins, `isnull()` on string columns, missing `todatetime()`), environment classification, the subscription-exclusion and `DoNotDelete` filters, export formats, Cost Management retry/throttle/cache behavior, and an end-to-end mocked scan. Live query semantics still need a real tenant — run a scan after `az login` to validate those.

## Usage

### Report (read-only)

```bash
# Scan all enabled subscriptions in the tenant
python3 orphan_report.py

# Scope to a single subscription
python3 orphan_report.py --subscription <subscription-id>

# Export to JSON (includes tags, estimated costs, environment)
python3 orphan_report.py --format json
python3 orphan_report.py --format json --output results.json

# Export to CSV
python3 orphan_report.py --format csv

# Exclude specific subscriptions from scanning
python3 orphan_report.py --exclude-subscriptions <sub-id-1> <sub-id-2>

# Generate interactive HTML dashboard (self-contained, no server needed)
python3 orphan_report.py --format html
python3 orphan_report.py --format html --output dashboard.html

# Generate formatted Excel workbook (4 sheets)
python3 generate_excel_report.py
python3 generate_excel_report.py --output my_report.xlsx

# Generate a CIR-ready PowerPoint summary slide
python3 orphan_report.py --format json --output scan.json
python3 generate_pptx_slide.py --input scan.json --client "Contoso Corp"
```

The report automatically groups results into two sections:

- **Production & Shared Services** — Resources safe to review for immediate cleanup.
- **Dev / QA / UAT** — Resources that may be intentionally reserved for active development, testing, or future use. A warning banner reminds you to verify with resource owners before removing.

Ends with a summary count including an environment breakdown.

The **HTML dashboard** (`--format html`) generates a single self-contained `.html` file you can open directly in any browser — no server required. It includes summary cards, bar charts, environment breakdown, and a fully searchable/sortable/filterable data table with tag badges.

### Cleanup

```bash
# Dry-run (default) — shows what WOULD be deleted, touches nothing
python3 orphan_cleanup.py

# Explicit dry-run
python3 orphan_cleanup.py --dry-run

# Actually delete orphaned resources
python3 orphan_cleanup.py --confirm

# Scope to a single subscription
python3 orphan_cleanup.py --subscription <subscription-id> --confirm

# Only clean up production resources (skip Dev/QA/UAT)
python3 orphan_cleanup.py --production-only --dry-run
python3 orphan_cleanup.py --production-only --confirm
```

- **`--dry-run`** (default): Previews all deletions without making changes.
- **`--confirm`**: Permanently deletes resources. A 5-second countdown gives you a chance to cancel with `Ctrl+C`.
- **`--production-only`**: Restricts cleanup to production and shared-services subscriptions only. Non-production resources are skipped with a count shown in the output.
- All actions are logged with timestamps to `orphan-cleanup.log`.

### Deletion Order

Resources are deleted in safe dependency order to avoid conflicts:

1. Private Endpoints
2. Application Gateways
3. Load Balancers
4. VNet Gateways
5. NICs
6. Public IPs
7. NSGs
8. Route Tables
9. NAT Gateways
10. Front Door WAF Policies
11. Traffic Manager Profiles
12. IP Groups
13. DDoS Protection Plans
14. Private DNS Zones
15. Subnets
16. Virtual Networks
17. Managed Disks
18. Availability Sets
19. App Service Plans
20. SQL Elastic Pools
21. Expired Certificates
22. API Connections
23. Empty Resource Groups (last)

## Environment Classification

Each resource is classified as **Production** or **Non-Production** using a three-tier approach (most specific wins):

| Priority | Source | Example |
|---|---|---|
| **1. Resource tags** | `environment` or `env` tag on the resource | `environment: dev` → Non-Production |
| **2. Naming conventions** | Keywords in resource name or resource group name | `rg-centralus-dev-network` → Non-Production |
| **3. Subscription name** | Fallback to subscription-level keywords | `ORGS_Development` → Non-Production |

Naming convention matching uses **word boundaries** to avoid false positives — `dev` matches `rg-dev-network` but not `device-manager`.

**Non-Production keywords**: `dev`, `development`, `qa`, `uat`, `test`, `staging`, `sandbox`, `lab`, `pilot`, `poc`, `nonprod`, `non-prod`, `nonprd`, `non-prd`, `preprod`, `pre-prod`, `stg`, `demo`

**Production**: Everything else (including `SharedServices`, `*_Production`, etc.), or any resource with an `environment` tag containing `prod` or `prd`.

This classification drives:
- **Report**: Resources grouped into separate Production vs Dev/QA/UAT sections with a warning on the non-production group.
- **Cleanup**: The `--production-only` flag filters out non-production resources. Each resource line shows a `[PROD]` or `[NON-PROD]` tag.

> **Why default to Production?** If no tag, name, or subscription matches a non-production keyword, the resource is treated as production. This is the safer default — you'd rather accidentally protect a dev resource than accidentally delete a production one.

## How It Works

- Uses **Azure Resource Graph** with management group scoping for fast, tenant-wide queries in a single API call.
- Empty Resource Groups are detected via two Resource Graph queries (all RGs minus RGs that contain resources) — no slow per-RG ARM iteration.
- The report runs all queries **in parallel** (ThreadPoolExecutor) for speed.
- The cleanup uses **type-specific Azure SDK clients** (NetworkManagementClient, ComputeManagementClient, etc.) for correct API versions and proper async delete handling.

## Output Formats

| Format | Command | Includes |
|---|---|---|
| **Console** (default) | `python3 orphan_report.py` | Color-coded tables, environment grouping, summary |
| **JSON** | `python3 orphan_report.py -f json` | Tags, estimated costs, environment, all fields |
| **CSV** | `python3 orphan_report.py -f csv` | Same as JSON, flat for spreadsheet import |
| **HTML** | `python3 orphan_report.py -f html` | Interactive dashboard with charts, search, sort, filter |
| **Excel** | `python3 generate_excel_report.py` | 4 formatted sheets: Summary, Production, Dev/QA/UAT, All Resources |
| **PowerPoint** | `python3 generate_pptx_slide.py -i scan.json` | 3-slide CIR deck: Executive Summary, Breakdown, Action Items |

All export formats include **resource tags** and **estimated monthly cost** per resource.

## Files

```
├── orphan_report.py           # Report script (read-only, console/json/csv/html)
├── orphan_cleanup.py          # Cleanup script (dry-run default)
├── generate_excel_report.py   # Excel workbook generator
├── generate_pptx_slide.py     # PowerPoint CIR deck generator (3 slides)
├── requirements.txt           # Python dependencies
└── README.md
```
