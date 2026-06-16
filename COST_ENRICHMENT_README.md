# Real Cost Data — Running Instructions

This tool now pulls actual cost numbers from the Azure Cost Management Query API
instead of using hardcoded per-category estimates. This doc covers what changed,
what RBAC you need, and how to run it.

## What changed

| File | Change |
|---|---|
| `azure_cost_enrichment.py` | New. Cost Management Query API client with QPU-aware retry and daily caching. |
| `orphan_report.py` | Queries now project `id=tolower(id)` so each orphan row can be joined back to cost data. Main flow calls the enrichment module and populates `estimatedMonthlyCost` / `rolling30dCost` / `lastBillingMonthCost` per resource. |
| `generate_excel_report.py` | Same query update. New Summary columns: `Monthly Cost`, `Rolling 30d (actual)`, `Last Billing Month`. New detail-sheet columns same plus `Cost Source`. |
| `generate_pptx_slide.py` | No code change — reads `estimatedMonthlyCost` from the JSON, which now holds real numbers automatically. |
| `requirements.txt` | Adds `requests>=2.31.0` for the Cost Management REST calls. |

## How the cost math works

The enrichment module runs **two** queries per subscription against
`Microsoft.CostManagement/query` API version `2023-11-01`:

1. **Rolling 30 days**, `AmortizedCost` metric, custom timeframe
   `[today-30, today-1]`. Ends **yesterday** to avoid the 8-24 hour data lag
   on same-day costs. This is the canonical "what are we burning per month
   right now" number.

2. **TheLastBillingMonth**, `AmortizedCost` metric. Invoice-aligned number
   finance can reconcile against the actual bill. Shown as a second column.

Both queries group by the `ResourceId` dimension with `granularity: None`, so
each row is `(resource_id_lowercase, total_cost, currency)`. The results are
joined back to the orphan list via the `id` field projected from Resource Graph.

**Why AmortizedCost?** It reallocates reservation and savings-plan costs back
to the specific resource that consumed them. ActualCost would attribute the
whole reservation to whichever resource happened to get billed first.
AmortizedCost is the correct metric for per-resource chargeback and is what
the FinOps Framework recommends for orphan-level waste reporting.

**Fallback behavior.** If Cost Management has no billing history for a
resource (too new, subscription not queried, or CM returned $0 for some other
reason), the row falls back to the hardcoded `COST_ESTIMATES` value for that
category. The `Cost Source` column in the Excel detail sheets tells you which:

- `costManagement` — real data from the API
- `estimate` — hardcoded fallback, no CM data at all
- `estimate-zero-cm` — CM matched the resource but returned $0, using estimate

## RBAC prerequisites

The identity running the scripts needs **Cost Management Reader** at the
**subscription** or **management group** level for every subscription you
scan. Resource Graph Reader alone is not enough — Cost Management is a
separate RBAC plane.

### Check what your current identity already has

```bash
az login

# List any Cost Management roles on your account
az role assignment list \
  --assignee $(az account show --query user.name -o tsv) \
  --all \
  --query "[?contains(roleDefinitionName, 'Cost Management')].{Role:roleDefinitionName, Scope:scope}" \
  -o table
```

If nothing comes back, you do not have the role and the enrichment will
fall back to hardcoded estimates with warnings in the output.

### Grant Cost Management Reader (if needed)

At the **tenant root management group** — one assignment covers every
subscription in the tenant:

```bash
# Find the tenant root MG ID
TENANT_ID=$(az account show --query tenantId -o tsv)

# Assign to yourself at tenant root
az role assignment create \
  --assignee $(az account show --query user.name -o tsv) \
  --role "Cost Management Reader" \
  --scope "/providers/Microsoft.Management/managementGroups/${TENANT_ID}"
```

At a specific **subscription** (narrower, if MG-level isn't available):

```bash
SUB_ID="your-subscription-id-here"
az role assignment create \
  --assignee $(az account show --query user.name -o tsv) \
  --role "Cost Management Reader" \
  --scope "/subscriptions/${SUB_ID}"
```

Role propagation takes 5-10 minutes. Wait before running the scripts.

### For service principals or managed identities

Same role, same scopes. Replace `--assignee` with the SP object ID or the
MI client ID.

## Install / update dependencies

```bash
cd C:/Users/PeterFranco/CascadeProjects/azure-orphan-scripts
.venv/Scripts/activate     # or: source .venv/bin/activate on mac/linux
pip install -r requirements.txt
```

The new dependency is `requests>=2.31.0` — everything else is already
installed if you've run the tool before.

## Run the reports

### Console report (what orphans exist, with real totals)

```bash
python orphan_report.py
```

At the bottom you'll see:

```
Estimated monthly waste: $4,812.47
  Real Cost Management rolling 30d: $4,561.22
  (38 of 42 rows backed by Cost Management data)
```

### JSON export (what feeds the PPTX generator)

```bash
python orphan_report.py --format json --output orphan-report-april2026.json
```

The JSON now includes a `costEnrichment` block with the 30-day window, the
subscriptions queried, and any that failed:

```json
{
  "scanDate": "2026-04-15T19:22:14+00:00",
  "totalResources": 42,
  "estimatedMonthlyCost": 4812.47,
  "totalRolling30dCost": 4561.22,
  "totalLastBillingMonthCost": 4933.08,
  "costEnrichment": {
    "generatedAt": "2026-04-15T19:22:00+00:00",
    "rolling30dWindow": { "from": "2026-03-16", "to": "2026-04-14" },
    "subscriptionsQueried": ["..."],
    "subscriptionsFailed": {},
    "totalRolling30d": 127843.55,
    "totalLastBillingMonth": 131204.12
  },
  "resources": [
    {
      "category": "Unattached Managed Disks",
      "resourceId": "/subscriptions/.../disks/old-vm-osdisk",
      "estimatedMonthlyCost": 18.30,
      "rolling30dCost": 18.30,
      "lastBillingMonthCost": 18.91,
      "costSource": "costManagement",
      ...
    }
  ]
}
```

### Excel report (full workbook for the CFO deck)

```bash
python generate_excel_report.py --output orphan-report-april2026.xlsx
```

Open the Summary sheet. The new columns are:

- `Monthly Cost` — what to quote as waste. Rolling 30d when available, estimate otherwise.
- `Rolling 30d (actual)` — raw CM number, $0 if no real data.
- `Last Billing Month` — invoice-aligned number, $0 if no real data.

The Production and Dev/QA/UAT detail sheets and the All Resources sheet have
the same three columns plus a `Cost Source` column showing per-row provenance.

### PowerPoint slide (auto-picks up real numbers)

```bash
# Regenerate the JSON first so the deck uses fresh cost data
python orphan_report.py --format json --output orphan-report-final.json
python generate_pptx_slide.py
```

The PPTX generator reads `estimatedMonthlyCost` from the JSON — no code
changes were needed. Once upstream JSON has real numbers, the slide
automatically shows them.

## Optional flags

All cost-related flags are available on both `orphan_report.py` and
`generate_excel_report.py`:

| Flag | Purpose |
|---|---|
| `--no-cost-data` | Skip Cost Management entirely. Use this if RBAC isn't set up yet or you're iterating on formatting and don't want to hit the API. |
| `--cost-cache-dir <dir>` | Where to read/write `cost-cache-YYYYMMDD.json`. Default: current directory. Same-day reruns load this instead of re-querying. |
| `--refresh-cost-data` | Force a fresh pull even if a same-day cache exists. |

## Caching

The enrichment module writes `cost-cache-YYYYMMDD.json` in the cache dir
after each successful pull. Any same-day rerun loads from that cache
instead of hitting the API. This matters because:

1. Cost Management has QPU throttling (12 queries / 10 seconds, 60 / min,
   600 / hour) and you don't want to burn quota regenerating the same deck
2. CM data only refreshes every 4 hours anyway
3. The cache file is human-readable JSON — you can grep it to spot-check

Delete the cache file or pass `--refresh-cost-data` to force a fresh pull.

## Gotchas

**Rolling 30d wobble.** Amortized costs for the current billing period get
re-rated throughout the month as Azure finalizes discounts and applies
reservation/savings-plan usage. Day-to-day the rolling number can shift
$100-500 for a large tenant. That's normal, not a bug. The `Last Billing
Month` column is the stable, invoice-matching number.

**Subscriptions you can't query.** If the running identity lacks Cost
Management Reader on one sub, that sub gets logged in `subscriptionsFailed`
in the JSON (and shown as a yellow warning in the console). Orphans in
that sub still appear in the report, but they'll use the hardcoded
estimate instead of real data.

**Zero-cost real queries.** A brand-new orphan that hasn't accrued any
billing yet will have `rolling30dCost = 0` from the API. The fallback
logic uses the estimate in that case so the CFO deck doesn't understate
waste — the `Cost Source` column will show `estimate-zero-cm` so you can
tell it apart from a true hardcoded-only row.

**DDoS Protection Plans.** The hardcoded estimate is $2,944/mo per plan
because that's the standard-SKU list price. In practice an orphaned DDoS
plan shows up as real cost in AmortizedCost immediately — so you should
see the actual number, not the estimate, within a day of the plan losing
its VNet associations.

**Subnets.** Subnets don't have their own Cost Management entries (they're
billed as part of the parent VNet). The query projects an empty `id` for
subnet rows, which falls through to $0 cost. That's correct behavior —
orphan subnets aren't a line-item waste driver.

## Troubleshooting

```bash
# Authenticated as the expected identity?
az account show

# Can I read Cost Management at all?
az consumption usage list --top 1 2>&1 | head -20

# Smoke-test the enrichment module in isolation
python azure_cost_enrichment.py --subscription <sub-id> --output cost-smoketest.json
cat cost-smoketest.json | python -m json.tool | head -50
```

If the smoke test works but the full report doesn't enrich, check
`orphan-report.log` for the specific failure.
