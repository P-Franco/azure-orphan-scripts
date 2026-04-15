#!/usr/bin/env python3
"""
orphan_report.py

Queries Azure Resource Graph for orphaned resources across ALL subscriptions
in the current tenant and prints a human-readable report.

Prerequisites:
  - pip install -r requirements.txt
  - Azure CLI authenticated (az login) or other DefaultAzureCredential method

Usage:
  python3 orphan_report.py                        # Scan all enabled subscriptions
  python3 orphan_report.py --subscription <id>    # Scan a single subscription
"""

import argparse
import csv
import json
import logging
import re
import sys
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

from azure.identity import DefaultAzureCredential
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest, QueryRequestOptions
from azure.mgmt.subscription import SubscriptionClient
from azure.core.exceptions import HttpResponseError
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

# ── Logging ───────────────────────────────────────────────────────────────────
logger = logging.getLogger("orphan-report")
logger.setLevel(logging.INFO)
_log_fh = logging.FileHandler("orphan-report.log", mode="a", encoding="utf-8")
_log_fh.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logger.addHandler(_log_fh)

# ── Colors ───────────────────────────────────────────────────────────────────
BOLD = "\033[1m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
GREEN = "\033[32m"
RESET = "\033[0m"


# ── Environment classification ────────────────────────────────────────────
NON_PROD_KEYWORDS = [
    "dev", "development", "qa", "uat", "test", "staging", "sandbox",
    "lab", "pilot", "poc", "nonprod", "non-prod", "nonprd", "non-prd",
    "preprod", "pre-prod", "stg", "demo",
]


def classify_subscription(sub_name: str) -> str:
    """Classify a subscription as PRODUCTION or NON-PRODUCTION based on name.
    Subscriptions containing dev/qa/uat/test/staging keywords are non-production.
    Everything else (including SharedServices) defaults to PRODUCTION.
    """
    name_lower = sub_name.lower()
    for kw in NON_PROD_KEYWORDS:
        if kw in name_lower:
            return "NON-PRODUCTION"
    return "PRODUCTION"


# Tag keys commonly used for environment classification
_ENV_TAG_KEYS = {"environment", "env"}
# Regex patterns for word-boundary keyword matching (compiled once)
_NON_PROD_PATTERNS = [
    re.compile(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])")
    for kw in NON_PROD_KEYWORDS
]


def classify_resource(resource: dict, sub_envs: dict) -> str:
    """Classify a single resource as PRODUCTION or NON-PRODUCTION.

    Precedence (most specific wins):
      1. Resource tags — 'environment' or 'env' tag value
      2. Naming conventions — resource name or resource group name keywords
      3. Subscription name — fallback to subscription-level classification

    Word-boundary matching is used for names to avoid false positives
    (e.g. 'dev' won't match 'device').
    """
    # 1. Check environment tags
    tags = resource.get("tags") or {}
    if isinstance(tags, dict):
        for tk, tv in tags.items():
            if tk.lower() in _ENV_TAG_KEYS:
                val = str(tv).lower().strip()
                for kw in NON_PROD_KEYWORDS:
                    if kw in val:
                        return "NON-PRODUCTION"
                if any(p in val for p in ("prod", "prd")):
                    return "PRODUCTION"

    # 2. Check resource name and resource group naming conventions
    name_lower = resource.get("name", "").lower()
    rg_lower = resource.get("resourceGroup", "").lower()
    for pattern in _NON_PROD_PATTERNS:
        if pattern.search(name_lower) or pattern.search(rg_lower):
            return "NON-PRODUCTION"

    # 3. Fall back to subscription-level classification
    sub_id = resource.get("subscriptionId", "")
    return sub_envs.get(sub_id, "PRODUCTION")


# ── Resource Graph helper ────────────────────────────────────────────────────
@retry(
    retry=retry_if_exception_type(HttpResponseError),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(3),
    reraise=True,
)
def run_query(
    graph_client: ResourceGraphClient,
    query: str,
    *,
    sub_ids: list[str] | None = None,
    mgmt_group: str | None = None,
) -> list[dict]:
    """Run a Resource Graph query, handling pagination.
    Scope by subscription IDs or management group (tenant root for tenant-wide).
    Retries up to 3 times on transient Azure errors with exponential backoff.
    """
    results = []
    options = QueryRequestOptions(result_format="objectArray")
    kwargs = {"query": query, "options": options}
    if mgmt_group:
        kwargs["management_groups"] = [mgmt_group]
    elif sub_ids:
        kwargs["subscriptions"] = sub_ids
    request = QueryRequest(**kwargs)
    try:
        response = graph_client.resources(request)
        results.extend(response.data)

        while response.skip_token:
            options.skip_token = response.skip_token
            response = graph_client.resources(request)
            results.extend(response.data)
    except HttpResponseError as e:
        if e.status_code == 403:
            logger.warning(f"Insufficient permissions for query: {e.message}")
            return []
        logger.error(f"Resource Graph query failed: {e.message}")
        raise

    return results


# ── Queries ──────────────────────────────────────────────────────────────────
QUERIES = {
    # ── Compute ──────────────────────────────────────────────────────────────
    "App Service Plans with no apps": {
        "query": """Resources
| where type =~ 'microsoft.web/serverfarms'
| where properties.numberOfSites == 0
| project id=tolower(id), name, resourceGroup, location, subscriptionId, sku=tostring(sku.name), tags""",
        "cost": True,
        "extra_col": "SKU/Size",
        "section": "COMPUTE",
    },
    "Availability Sets with no VMs": {
        "query": """Resources
| where type =~ 'microsoft.compute/availabilitysets'
| where properties.virtualMachines == '[]' or array_length(properties.virtualMachines) == 0
| where tags !has 'DoNotDelete'
| project id=tolower(id), name, resourceGroup, location, subscriptionId, tags""",
        "cost": True,
        "extra_col": "",
        "section": "COMPUTE",
    },
    # ── Networking ───────────────────────────────────────────────────────────
    "Unassociated Public IPs": {
        "query": """Resources
| where type =~ 'microsoft.network/publicipaddresses'
| where properties.ipConfiguration == '' or isnull(properties.ipConfiguration)
| where properties.natGateway == '' or isnull(properties.natGateway)
| project id=tolower(id), name, resourceGroup, location, subscriptionId, sku=tostring(sku.name), tags""",
        "cost": True,
        "extra_col": "SKU/Size",
        "section": "NETWORKING",
    },
    "NICs not attached to a VM": {
        "query": """Resources
| where type =~ 'microsoft.network/networkinterfaces'
| where isnull(properties.virtualMachine) or properties.virtualMachine == ''
| where isnull(properties.privateEndpoint) or properties.privateEndpoint == ''
| project id=tolower(id), name, resourceGroup, location, subscriptionId, tags""",
        "cost": True,
        "extra_col": "",
        "section": "NETWORKING",
    },
    "NSGs not associated with subnet or NIC": {
        "query": """Resources
| where type =~ 'microsoft.network/networksecuritygroups'
| where isnull(properties.networkInterfaces) or properties.networkInterfaces == '[]' or array_length(properties.networkInterfaces) == 0
| where isnull(properties.subnets) or properties.subnets == '[]' or array_length(properties.subnets) == 0
| project id=tolower(id), name, resourceGroup, location, subscriptionId, tags""",
        "cost": True,
        "extra_col": "",
        "section": "NETWORKING",
    },
    "Load Balancers with empty backend pools": {
        "query": """Resources
| where type =~ 'microsoft.network/loadbalancers'
| where properties.backendAddressPools == '[]' or array_length(properties.backendAddressPools) == 0
| project id=tolower(id), name, resourceGroup, location, subscriptionId, sku=tostring(sku.name), tags""",
        "cost": True,
        "extra_col": "SKU/Size",
        "section": "NETWORKING",
    },
    "Application Gateways with empty backend pools": {
        "query": """Resources
| where type =~ 'microsoft.network/applicationgateways'
| where properties.backendAddressPools == '[]' or array_length(properties.backendAddressPools) == 0
| project id=tolower(id), name, resourceGroup, location, subscriptionId, sku=tostring(sku.tier), tags""",
        "cost": True,
        "extra_col": "SKU/Size",
        "section": "NETWORKING",
    },
    "VNet Gateways with no connections": {
        "query": """Resources
| where type =~ 'microsoft.network/virtualnetworkgateways'
| join kind=leftouter (
  Resources
  | where type =~ 'microsoft.network/connections'
  | mv-expand gw = pack_array(properties.virtualNetworkGateway1.id, properties.virtualNetworkGateway2.id)
  | project connectionGwId=tolower(tostring(gw))
) on $left.id == $right.connectionGwId
| where isnull(connectionGwId)
| project id=tolower(id), name, resourceGroup, location, subscriptionId, sku=tostring(properties.sku.name), tags""",
        "cost": True,
        "extra_col": "SKU/Size",
        "section": "NETWORKING",
    },
    "Private Endpoints not connected to a resource": {
        "query": """Resources
| where type =~ 'microsoft.network/privateendpoints'
| where isnull(properties.privateLinkServiceConnections) or array_length(properties.privateLinkServiceConnections) == 0
| where isnull(properties.manualPrivateLinkServiceConnections) or array_length(properties.manualPrivateLinkServiceConnections) == 0
| project id=tolower(id), name, resourceGroup, location, subscriptionId, tags""",
        "cost": True,
        "extra_col": "",
        "section": "NETWORKING",
    },
    "Route Tables not associated with a subnet": {
        "query": """Resources
| where type =~ 'microsoft.network/routetables'
| where isnull(properties.subnets) or properties.subnets == '[]' or array_length(properties.subnets) == 0
| project id=tolower(id), name, resourceGroup, location, subscriptionId, tags""",
        "cost": False,
        "extra_col": "",
        "section": "NETWORKING",
    },
    "NAT Gateways not associated with a subnet": {
        "query": """Resources
| where type =~ 'microsoft.network/natgateways'
| where isnull(properties.subnets) or properties.subnets == '[]' or array_length(properties.subnets) == 0
| project id=tolower(id), name, resourceGroup, location, subscriptionId, sku=tostring(sku.name), tags""",
        "cost": True,
        "extra_col": "SKU/Size",
        "section": "NETWORKING",
    },
    "Front Door WAF Policies not linked to a Front Door": {
        "query": """Resources
| where type =~ 'microsoft.network/frontdoorwebapplicationfirewallpolicies'
| where (isnull(properties.frontendEndpointLinks) or array_length(properties.frontendEndpointLinks) == 0)
| where (isnull(properties.securityPolicyLinks) or array_length(properties.securityPolicyLinks) == 0)
| project id=tolower(id), name, resourceGroup, location, subscriptionId, sku=tostring(sku.name), tags""",
        "cost": True,
        "extra_col": "SKU/Size",
        "section": "NETWORKING",
    },
    "Traffic Manager Profiles with no endpoints": {
        "query": """Resources
| where type =~ 'microsoft.network/trafficmanagerprofiles'
| where properties.endpoints == '[]' or array_length(properties.endpoints) == 0
| project id=tolower(id), name, resourceGroup, location, subscriptionId, tags""",
        "cost": True,
        "extra_col": "",
        "section": "NETWORKING",
    },
    "Virtual Networks with no subnets": {
        "query": """Resources
| where type =~ 'microsoft.network/virtualnetworks'
| where isnull(properties.subnets) or array_length(properties.subnets) == 0
| project id=tolower(id), name, resourceGroup, location, subscriptionId, tags""",
        "cost": False,
        "extra_col": "",
        "section": "NETWORKING",
    },
    "Subnets without connected devices": {
        # Subnets don't have their own resource IDs in Cost Management, so we
        # project an empty id. They fall through to $0 cost (which is correct).
        "query": """Resources
| where type =~ 'microsoft.network/virtualnetworks'
| mv-expand subnet = properties.subnets
| where subnet.name !in~ ('GatewaySubnet', 'AzureFirewallSubnet', 'AzureFirewallManagementSubnet', 'AzureBastionSubnet', 'RouteServerSubnet')
| where (isnull(subnet.properties.ipConfigurations) or array_length(subnet.properties.ipConfigurations) == 0)
| where (isnull(subnet.properties.privateEndpoints) or array_length(subnet.properties.privateEndpoints) == 0)
| where (isnull(subnet.properties.delegations) or array_length(subnet.properties.delegations) == 0)
| extend subnetName = tostring(subnet.name)
| project id='', name=subnetName, resourceGroup, location, subscriptionId, sku=name, tags""",
        "cost": False,
        "extra_col": "VNet",
        "section": "NETWORKING",
    },
    "IP Groups not referenced by any firewall": {
        "query": """Resources
| where type =~ 'microsoft.network/ipgroups'
| where (isnull(properties.firewalls) or array_length(properties.firewalls) == 0)
| where (isnull(properties.firewallPolicies) or array_length(properties.firewallPolicies) == 0)
| project id=tolower(id), name, resourceGroup, location, subscriptionId, tags""",
        "cost": False,
        "extra_col": "",
        "section": "NETWORKING",
    },
    "Private DNS Zones with no VNet links": {
        "query": """Resources
| where type =~ 'microsoft.network/privatednszones'
| where properties.numberOfVirtualNetworkLinks == 0
| project id=tolower(id), name, resourceGroup, location, subscriptionId, tags""",
        "cost": False,
        "extra_col": "",
        "section": "NETWORKING",
    },
    "DDoS Protection Plans with no associated VNets": {
        "query": """Resources
| where type =~ 'microsoft.network/ddosprotectionplans'
| where isnull(properties.virtualNetworks) or properties.virtualNetworks == '[]' or array_length(properties.virtualNetworks) == 0
| project id=tolower(id), name, resourceGroup, location, subscriptionId, tags""",
        "cost": True,
        "extra_col": "",
        "section": "NETWORKING",
    },
    # ── Storage ──────────────────────────────────────────────────────────────
    "Unattached Managed Disks": {
        "query": """Resources
| where type =~ 'microsoft.compute/disks'
| where properties.diskState =~ 'Unattached'
| project id=tolower(id), name, resourceGroup, location, subscriptionId, sku=tostring(sku.name), tags""",
        "cost": True,
        "extra_col": "SKU/Size",
        "section": "STORAGE",
    },
    # ── Database ─────────────────────────────────────────────────────────────
    "SQL Elastic Pools with no databases": {
        "query": """Resources
| where type =~ 'microsoft.sql/servers/elasticpools'
| where isnull(properties.perDatabaseSettings) or properties.numberOfDatabases == 0
| project id=tolower(id), name, resourceGroup, location, subscriptionId, sku=tostring(sku.name), tags""",
        "cost": False,
        "extra_col": "SKU/Size",
        "section": "DATABASE",
    },
    # ── Other ────────────────────────────────────────────────────────────────
    "Expired App Service Certificates": {
        "query": """Resources
| where type =~ 'microsoft.web/certificates'
| where properties.expirationDate < now()
| project id=tolower(id), name, resourceGroup, location, subscriptionId, sku=tostring(properties.expirationDate), tags""",
        "cost": False,
        "extra_col": "Expiry",
        "section": "OTHER",
    },
    "Disconnected API Connections": {
        "query": """Resources
| where type =~ 'microsoft.web/connections'
| where isnotnull(properties.statuses)
| where array_length(properties.statuses) > 0
| extend connStatus = tostring(properties.statuses[0]['status'])
| where connStatus !in~ ('Connected', 'Ready')
| project id=tolower(id), name, resourceGroup, location, subscriptionId, sku=connStatus, tags""",
        "cost": False,
        "extra_col": "Status",
        "section": "OTHER",
    },
}

# Ordered section list for display
SECTION_ORDER = ["COMPUTE", "NETWORKING", "STORAGE", "DATABASE", "OTHER"]

# ── Estimated monthly cost per resource type (USD) ────────────────────────────
# Rough estimates when Azure Cost Management data is unavailable.
# Actual costs vary by region, SKU, and usage.
COST_ESTIMATES = {
    "App Service Plans with no apps": 55.0,
    "Availability Sets with no VMs": 0.0,
    "Unassociated Public IPs": 3.60,
    "NICs not attached to a VM": 0.0,
    "NSGs not associated with subnet or NIC": 0.0,
    "Load Balancers with empty backend pools": 25.0,
    "Application Gateways with empty backend pools": 140.0,
    "VNet Gateways with no connections": 140.0,
    "Private Endpoints not connected to a resource": 7.30,
    "Route Tables not associated with a subnet": 0.0,
    "NAT Gateways not associated with a subnet": 32.0,
    "Front Door WAF Policies not linked to a Front Door": 5.0,
    "Traffic Manager Profiles with no endpoints": 0.36,
    "Virtual Networks with no subnets": 0.0,
    "Subnets without connected devices": 0.0,
    "IP Groups not referenced by any firewall": 0.0,
    "Private DNS Zones with no VNet links": 0.25,
    "DDoS Protection Plans with no associated VNets": 2944.0,
    "Unattached Managed Disks": 5.0,
    "SQL Elastic Pools with no databases": 150.0,
    "Expired App Service Certificates": 0.0,
    "Disconnected API Connections": 0.0,
    "Empty Resource Groups": 0.0,
}


# ── JSON / CSV output formatters ──────────────────────────────────────────────
def _flatten_row(
    r: dict,
    category: str,
    cfg: dict,
    sub_names: dict,
    sub_envs: dict,
    cost_result=None,
) -> dict:
    """Flatten a resource row into a dict suitable for JSON/CSV export.

    If `cost_result` (an EnrichmentResult) is supplied and contains real data
    for this resource ID, those numbers take precedence over COST_ESTIMATES.
    Falls back to the hardcoded per-category estimate when real data is
    unavailable (e.g. resource too new to have billing history, CM query
    failed for that subscription, or no Cost Management Reader RBAC).
    """
    sub_id = r.get("subscriptionId", "")
    resource_id = str(r.get("id", "") or "").lower()
    tags = r.get("tags") or {}

    # Real cost lookup.
    rolling30d = 0.0
    last_billing_month = 0.0
    currency = "USD"
    cost_source = "estimate"

    if cost_result is not None and resource_id:
        rec = cost_result.get_cost(resource_id)
        if rec is not None:
            rolling30d = rec.rolling30d
            last_billing_month = rec.last_billing_month
            currency = rec.currency
            cost_source = "costManagement"

    # Fallback: per-category hardcoded estimate if no real data found.
    estimate = float(COST_ESTIMATES.get(category, 0.0))
    if cost_source == "estimate":
        monthly_cost = estimate
    else:
        # Prefer rolling 30d as the "canonical monthly cost" — reflects the
        # current rate of spend, not a stale prior invoice.
        monthly_cost = rolling30d if rolling30d > 0 else last_billing_month
        if monthly_cost <= 0:
            # Real query returned $0 — likely a resource too new to have
            # racked up billing yet. Fall back to the estimate so the CFO
            # deck doesn't understate waste.
            monthly_cost = estimate
            cost_source = "estimate-zero-cm"

    return {
        "category": category,
        "section": cfg.get("section", "OTHER"),
        "name": r.get("name", ""),
        "resourceGroup": r.get("resourceGroup", ""),
        "location": r.get("location", ""),
        "subscription": sub_names.get(sub_id, sub_id),
        "subscriptionId": sub_id,
        "resourceId": resource_id,
        "sku": r.get("sku", "") or "",
        "incursCost": cfg.get("cost", False),
        "estimatedMonthlyCost": round(monthly_cost, 2),
        "rolling30dCost": round(rolling30d, 2),
        "lastBillingMonthCost": round(last_billing_month, 2),
        "costCurrency": currency,
        "costSource": cost_source,
        "environment": classify_resource(r, sub_envs),
        "tags": tags if isinstance(tags, dict) else {},
    }


def _collect_flat_rows(category_results, sub_names, sub_envs, cost_result=None):
    """Build a flat list of all orphan rows for export."""
    rows = []
    for cat_name, cfg in QUERIES.items():
        for r in category_results.get(cat_name, []):
            rows.append(_flatten_row(r, cat_name, cfg, sub_names, sub_envs, cost_result))
    # Empty RGs
    for r in category_results.get("Empty Resource Groups", []):
        rows.append(_flatten_row(
            r, "Empty Resource Groups",
            {"cost": False, "section": "OTHER"},
            sub_names, sub_envs, cost_result,
        ))
    return rows


def export_json(rows: list[dict], filepath: str, cost_result=None) -> str:
    """Write orphan data to JSON, including per-row real costs and a
    top-level block describing the Cost Management enrichment window."""
    total_cost = sum(r["estimatedMonthlyCost"] for r in rows)
    total_rolling = sum(r.get("rolling30dCost", 0.0) for r in rows)
    total_last_month = sum(r.get("lastBillingMonthCost", 0.0) for r in rows)

    output: dict = {
        "scanDate": datetime.now(timezone.utc).isoformat(),
        "totalResources": len(rows),
        "estimatedMonthlyCost": round(total_cost, 2),
        "totalRolling30dCost": round(total_rolling, 2),
        "totalLastBillingMonthCost": round(total_last_month, 2),
        "resources": rows,
    }
    if cost_result is not None:
        output["costEnrichment"] = {
            "generatedAt": cost_result.generated_at,
            "rolling30dWindow": {
                "from": cost_result.rolling30d_window[0],
                "to": cost_result.rolling30d_window[1],
            },
            "subscriptionsQueried": cost_result.subscriptions_queried,
            "subscriptionsFailed": cost_result.subscriptions_failed,
            "totalRolling30d": round(cost_result.total_rolling30d, 2),
            "totalLastBillingMonth": round(cost_result.total_last_billing_month, 2),
        }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, default=str)
    return filepath


def export_csv(rows: list[dict], filepath: str) -> str:
    """Write orphan data to CSV."""
    fieldnames = [
        "category", "section", "name", "resourceGroup", "location",
        "subscription", "subscriptionId", "resourceId", "sku", "incursCost",
        "estimatedMonthlyCost", "rolling30dCost", "lastBillingMonthCost",
        "costCurrency", "costSource", "environment", "tags",
    ]
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            row = {k: r.get(k, "") for k in fieldnames}
            row["tags"] = json.dumps(row["tags"]) if row.get("tags") else "{}"
            writer.writerow(row)
    return filepath


def export_html(rows: list[dict], filepath: str, scan_scope: str = "") -> str:
    """Write orphan data to a self-contained interactive HTML dashboard."""
    import html as _html

    total = len(rows)
    total_cost = sum(r["estimatedMonthlyCost"] for r in rows)
    prod_count = sum(1 for r in rows if r["environment"] == "PRODUCTION")
    nonprod_count = total - prod_count

    # Aggregate by category
    by_cat: dict[str, int] = {}
    cost_by_cat: dict[str, float] = {}
    for r in rows:
        c = r["category"]
        by_cat[c] = by_cat.get(c, 0) + 1
        cost_by_cat[c] = cost_by_cat.get(c, 0.0) + r["estimatedMonthlyCost"]
    sorted_cats = sorted(by_cat.items(), key=lambda x: -x[1])
    max_cat_count = max(by_cat.values()) if by_cat else 1

    # Aggregate by section
    by_section: dict[str, int] = {}
    for r in rows:
        s = r["section"]
        by_section[s] = by_section.get(s, 0) + 1

    # Build bar chart HTML
    bar_items = ""
    colors = ["#2563eb", "#0891b2", "#059669", "#d97706", "#dc2626",
              "#7c3aed", "#db2777", "#65a30d", "#ea580c", "#4f46e5",
              "#0d9488", "#ca8a04", "#9333ea", "#e11d48", "#16a34a",
              "#2563eb", "#0891b2", "#059669", "#d97706", "#dc2626",
              "#7c3aed", "#db2777", "#65a30d"]
    for i, (cat, cnt) in enumerate(sorted_cats):
        pct = (cnt / max_cat_count) * 100
        cost = cost_by_cat.get(cat, 0.0)
        color = colors[i % len(colors)]
        bar_items += f"""
        <div class="bar-row">
          <div class="bar-label" title="{_html.escape(cat)}">{_html.escape(cat)}</div>
          <div class="bar-track">
            <div class="bar-fill" style="width:{pct:.1f}%;background:{color}"></div>
          </div>
          <div class="bar-value">{cnt}</div>
          <div class="bar-cost">${cost:,.2f}</div>
        </div>"""

    # Build section pills
    section_pills = ""
    section_colors = {"COMPUTE": "#2563eb", "NETWORKING": "#0891b2",
                      "STORAGE": "#059669", "DATABASE": "#d97706", "OTHER": "#6b7280"}
    for sec, cnt in sorted(by_section.items(), key=lambda x: -x[1]):
        sc = section_colors.get(sec, "#6b7280")
        section_pills += f'<span class="pill" style="background:{sc}">{_html.escape(sec)}: {cnt}</span> '

    # Build table rows
    table_rows = ""
    for r in rows:
        tags = r.get("tags", {})
        tag_badges = ""
        if isinstance(tags, dict):
            for k, v in tags.items():
                tag_badges += (
                    f'<span class="tag">{_html.escape(str(k))}: '
                    f'{_html.escape(str(v))}</span> '
                )
        env_cls = "env-prod" if r["environment"] == "PRODUCTION" else "env-nonprod"
        cost_cls = "cost-yes" if r.get("incursCost") else "cost-no"
        table_rows += f"""
        <tr>
          <td>{_html.escape(r.get('category', ''))}</td>
          <td>{_html.escape(r.get('section', ''))}</td>
          <td class="name-cell">{_html.escape(r.get('name', ''))}</td>
          <td>{_html.escape(r.get('resourceGroup', ''))}</td>
          <td>{_html.escape(r.get('location', ''))}</td>
          <td>{_html.escape(r.get('subscription', ''))}</td>
          <td>{_html.escape(str(r.get('sku', '') or ''))}</td>
          <td class="{cost_cls}">{'Yes' if r.get('incursCost') else 'No'}</td>
          <td class="cost-cell">${r.get('estimatedMonthlyCost', 0):.2f}</td>
          <td class="{env_cls}">{_html.escape(r.get('environment', ''))}</td>
          <td class="tags-cell">{tag_badges or '<span class="no-tags">—</span>'}</td>
        </tr>"""

    scan_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    scope_text = _html.escape(scan_scope) if scan_scope else "Tenant-wide"

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Azure Orphaned Resources Dashboard</title>
<style>
  :root {{
    --bg: #f8fafc; --card: #ffffff; --border: #e2e8f0;
    --text: #1e293b; --muted: #64748b; --accent: #2563eb;
    --green: #059669; --yellow: #d97706; --red: #dc2626;
  }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: var(--bg); color: var(--text); line-height: 1.5; }}
  .container {{ max-width: 1400px; margin: 0 auto; padding: 24px; }}

  /* Header */
  .header {{ background: linear-gradient(135deg, #1e3a5f 0%, #2563eb 100%);
             color: white; padding: 32px; border-radius: 12px; margin-bottom: 24px; }}
  .header h1 {{ font-size: 24px; font-weight: 700; margin-bottom: 4px; }}
  .header .meta {{ font-size: 13px; opacity: 0.85; }}

  /* Cards */
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px; margin-bottom: 24px; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px;
           padding: 20px; text-align: center; }}
  .card .value {{ font-size: 32px; font-weight: 700; }}
  .card .label {{ font-size: 13px; color: var(--muted); margin-top: 4px; }}
  .card.total .value {{ color: var(--accent); }}
  .card.cost .value {{ color: var(--red); }}
  .card.prod .value {{ color: var(--green); }}
  .card.nonprod .value {{ color: var(--yellow); }}

  /* Charts panel */
  .panels {{ display: grid; grid-template-columns: 2fr 1fr; gap: 16px; margin-bottom: 24px; }}
  @media (max-width: 900px) {{ .panels {{ grid-template-columns: 1fr; }} }}
  .panel {{ background: var(--card); border: 1px solid var(--border);
            border-radius: 10px; padding: 20px; }}
  .panel h2 {{ font-size: 15px; font-weight: 600; margin-bottom: 12px; color: var(--muted); }}

  /* Bar chart */
  .bar-row {{ display: flex; align-items: center; margin-bottom: 6px; }}
  .bar-label {{ width: 260px; font-size: 12px; white-space: nowrap; overflow: hidden;
                text-overflow: ellipsis; flex-shrink: 0; padding-right: 8px; }}
  .bar-track {{ flex: 1; height: 20px; background: #f1f5f9; border-radius: 4px;
                overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 4px; transition: width 0.6s ease; }}
  .bar-value {{ width: 36px; text-align: right; font-size: 13px; font-weight: 600;
                margin-left: 8px; flex-shrink: 0; }}
  .bar-cost {{ width: 80px; text-align: right; font-size: 12px; color: var(--muted);
               margin-left: 8px; flex-shrink: 0; }}

  /* Section pills */
  .pill {{ display: inline-block; padding: 4px 12px; border-radius: 20px;
           color: white; font-size: 13px; font-weight: 600; margin: 4px; }}

  /* Env split */
  .env-bar {{ display: flex; height: 32px; border-radius: 6px; overflow: hidden;
              margin: 8px 0; }}
  .env-bar .prod {{ background: var(--green); }}
  .env-bar .nonprod {{ background: var(--yellow); }}
  .env-legend {{ font-size: 13px; margin-top: 8px; }}
  .env-legend span {{ margin-right: 16px; }}
  .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%;
          margin-right: 4px; vertical-align: middle; }}

  /* Table */
  .table-panel {{ background: var(--card); border: 1px solid var(--border);
                  border-radius: 10px; padding: 20px; overflow-x: auto; }}
  .controls {{ display: flex; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; align-items: center; }}
  .controls input, .controls select {{ padding: 8px 12px; border: 1px solid var(--border);
    border-radius: 6px; font-size: 13px; background: white; }}
  .controls input {{ flex: 1; min-width: 200px; }}
  .controls select {{ min-width: 140px; }}
  .count-badge {{ font-size: 13px; color: var(--muted); margin-left: auto; }}

  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ background: #f1f5f9; padding: 10px 8px; text-align: left; font-weight: 600;
       cursor: pointer; user-select: none; white-space: nowrap; position: sticky; top: 0; }}
  th:hover {{ background: #e2e8f0; }}
  th .sort-arrow {{ font-size: 10px; margin-left: 4px; opacity: 0.5; }}
  td {{ padding: 8px; border-bottom: 1px solid var(--border); vertical-align: top; }}
  tr:hover {{ background: #f8fafc; }}
  .name-cell {{ font-weight: 500; }}
  .cost-cell {{ text-align: right; font-family: monospace; }}
  .env-prod {{ color: var(--green); font-weight: 600; font-size: 12px; }}
  .env-nonprod {{ color: var(--yellow); font-weight: 600; font-size: 12px; }}
  .cost-yes {{ color: var(--red); font-weight: 600; }}
  .cost-no {{ color: var(--green); }}
  .tags-cell {{ max-width: 300px; }}
  .tag {{ display: inline-block; background: #e0f2fe; color: #0369a1; padding: 2px 6px;
          border-radius: 4px; font-size: 11px; margin: 1px; white-space: nowrap; }}
  .no-tags {{ color: #cbd5e1; }}

  /* Footer */
  .footer {{ text-align: center; padding: 24px; font-size: 12px; color: var(--muted); }}
</style>
</head>
<body>
<div class="container">

  <div class="header">
    <h1>Azure Orphaned Resources Dashboard</h1>
    <div class="meta">Generated: {scan_ts} &nbsp;|&nbsp; Scope: {scope_text}</div>
  </div>

  <div class="cards">
    <div class="card total">
      <div class="value">{total}</div>
      <div class="label">Total Orphaned Resources</div>
    </div>
    <div class="card cost">
      <div class="value">${total_cost:,.2f}</div>
      <div class="label">Est. Monthly Waste</div>
    </div>
    <div class="card prod">
      <div class="value">{prod_count}</div>
      <div class="label">Production</div>
    </div>
    <div class="card nonprod">
      <div class="value">{nonprod_count}</div>
      <div class="label">Dev / QA / UAT</div>
    </div>
  </div>

  <div class="panels">
    <div class="panel">
      <h2>Resources by Category</h2>
      {bar_items}
    </div>
    <div class="panel">
      <h2>Environment Split</h2>
      <div class="env-bar">
        <div class="prod" style="width:{(prod_count/max(total,1)*100):.1f}%"></div>
        <div class="nonprod" style="width:{(nonprod_count/max(total,1)*100):.1f}%"></div>
      </div>
      <div class="env-legend">
        <span><span class="dot" style="background:var(--green)"></span>Production: {prod_count}</span>
        <span><span class="dot" style="background:var(--yellow)"></span>Non-Production: {nonprod_count}</span>
      </div>
      <h2 style="margin-top:24px;">By Section</h2>
      <div>{section_pills}</div>
    </div>
  </div>

  <div class="table-panel">
    <div class="controls">
      <input type="text" id="search" placeholder="Search resources..." oninput="filterTable()">
      <select id="envFilter" onchange="filterTable()">
        <option value="">All Environments</option>
        <option value="PRODUCTION">Production</option>
        <option value="NON-PRODUCTION">Non-Production</option>
      </select>
      <select id="sectionFilter" onchange="filterTable()">
        <option value="">All Sections</option>
        <option value="COMPUTE">Compute</option>
        <option value="NETWORKING">Networking</option>
        <option value="STORAGE">Storage</option>
        <option value="DATABASE">Database</option>
        <option value="OTHER">Other</option>
      </select>
      <select id="costFilter" onchange="filterTable()">
        <option value="">All Cost</option>
        <option value="Yes">Cost-incurring</option>
        <option value="No">No direct cost</option>
      </select>
      <span class="count-badge" id="rowCount">{total} resources</span>
    </div>

    <table id="dataTable">
      <thead>
        <tr>
          <th onclick="sortTable(0)">Category <span class="sort-arrow">&#9650;&#9660;</span></th>
          <th onclick="sortTable(1)">Section <span class="sort-arrow">&#9650;&#9660;</span></th>
          <th onclick="sortTable(2)">Name <span class="sort-arrow">&#9650;&#9660;</span></th>
          <th onclick="sortTable(3)">Resource Group <span class="sort-arrow">&#9650;&#9660;</span></th>
          <th onclick="sortTable(4)">Location <span class="sort-arrow">&#9650;&#9660;</span></th>
          <th onclick="sortTable(5)">Subscription <span class="sort-arrow">&#9650;&#9660;</span></th>
          <th onclick="sortTable(6)">SKU <span class="sort-arrow">&#9650;&#9660;</span></th>
          <th onclick="sortTable(7)">Cost? <span class="sort-arrow">&#9650;&#9660;</span></th>
          <th onclick="sortTable(8)">Est. $/mo <span class="sort-arrow">&#9650;&#9660;</span></th>
          <th onclick="sortTable(9)">Environment <span class="sort-arrow">&#9650;&#9660;</span></th>
          <th>Tags</th>
        </tr>
      </thead>
      <tbody>
        {table_rows}
      </tbody>
    </table>
  </div>

  <div class="footer">
    Azure Orphan Resource Scripts &mdash; Generated {scan_ts}
  </div>

</div>

<script>
let sortCol = -1, sortAsc = true;

function sortTable(col) {{
  const table = document.getElementById('dataTable');
  const tbody = table.tBodies[0];
  const rows = Array.from(tbody.rows);
  if (sortCol === col) {{ sortAsc = !sortAsc; }} else {{ sortCol = col; sortAsc = true; }}
  rows.sort((a, b) => {{
    let va = a.cells[col].textContent.trim();
    let vb = b.cells[col].textContent.trim();
    if (col === 8) {{ va = parseFloat(va.replace('$','')) || 0; vb = parseFloat(vb.replace('$','')) || 0; return sortAsc ? va - vb : vb - va; }}
    return sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
  }});
  rows.forEach(r => tbody.appendChild(r));
}}

function filterTable() {{
  const search = document.getElementById('search').value.toLowerCase();
  const env = document.getElementById('envFilter').value;
  const section = document.getElementById('sectionFilter').value;
  const cost = document.getElementById('costFilter').value;
  const table = document.getElementById('dataTable');
  const rows = table.tBodies[0].rows;
  let visible = 0;
  for (let r of rows) {{
    const text = r.textContent.toLowerCase();
    const rowEnv = r.cells[9].textContent.trim();
    const rowSec = r.cells[1].textContent.trim();
    const rowCost = r.cells[7].textContent.trim();
    const show = text.includes(search)
      && (!env || rowEnv === env)
      && (!section || rowSec === section)
      && (!cost || rowCost === cost);
    r.style.display = show ? '' : 'none';
    if (show) visible++;
  }}
  document.getElementById('rowCount').textContent = visible + ' resources';
}}
</script>
</body>
</html>"""

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html_content)
    return filepath


# ── Printing helpers ─────────────────────────────────────────────────────────
def print_table(rows: list[dict], sub_names: dict, extra_col: str = "") -> None:
    """Print a formatted table of resources."""
    if not rows:
        print(f"  {GREEN}None found.{RESET}")
        print()
        return

    if extra_col:
        header = f"  {BOLD}{'NAME':<36s} {'RESOURCE GROUP':<26s} {'LOCATION':<16s} {'SUBSCRIPTION':<24s} {extra_col:<14s}{RESET}"
        sep = f"  {'─'*34:<36s} {'─'*24:<26s} {'─'*14:<16s} {'─'*22:<24s} {'─'*12:<14s}"
    else:
        header = f"  {BOLD}{'NAME':<36s} {'RESOURCE GROUP':<26s} {'LOCATION':<16s} {'SUBSCRIPTION':<24s}{RESET}"
        sep = f"  {'─'*34:<36s} {'─'*24:<26s} {'─'*14:<16s} {'─'*22:<24s}"

    print(header)
    print(sep)

    for r in rows:
        name = str(r.get("name", "N/A"))[:34]
        rg = str(r.get("resourceGroup", "N/A"))[:24]
        loc = str(r.get("location", "N/A"))[:14]
        sub_id = str(r.get("subscriptionId", ""))
        sub = sub_names.get(sub_id, sub_id[:22])[:22]
        sku = str(r.get("sku", "") or "")[:12]

        if extra_col:
            print(f"  {name:<36s} {rg:<26s} {loc:<16s} {sub:<24s} {sku:<14s}")
        else:
            print(f"  {name:<36s} {rg:<26s} {loc:<16s} {sub:<24s}")

    print()


# ── Empty Resource Groups (Resource Graph) ───────────────────────────────────
def find_empty_rgs(graph_client: ResourceGraphClient, **query_kwargs) -> list[dict]:
    """Find empty resource groups using 2 Resource Graph queries + set difference."""
    all_rgs = run_query(
        graph_client,
        """ResourceContainers
| where type =~ 'microsoft.resources/subscriptions/resourcegroups'
| project name, resourceGroup=name, location, subscriptionId, tags""",
        **query_kwargs,
    )

    nonempty = run_query(
        graph_client,
        """Resources
| summarize count() by resourceGroup, subscriptionId
| project resourceGroup, subscriptionId""",
        **query_kwargs,
    )

    occupied = {
        (r["resourceGroup"].lower(), r["subscriptionId"].lower())
        for r in nonempty
    }

    return [
        rg for rg in all_rgs
        if (rg["resourceGroup"].lower(), rg["subscriptionId"].lower()) not in occupied
    ]


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Azure Orphaned Resources Report")
    parser.add_argument("--subscription", "-s", help="Scope to a single subscription ID")
    parser.add_argument("--exclude-subscriptions", nargs="+", default=[],
                        help="Subscription IDs to exclude from scanning")
    parser.add_argument("--format", "-f", choices=["console", "json", "csv", "html"],
                        default="console", help="Output format (default: console)")
    parser.add_argument("--output", "-o", help="Output file path (auto-generated if omitted)")
    parser.add_argument("--no-cost-data", action="store_true",
                        help="Skip Cost Management API enrichment and use hardcoded "
                             "estimates only. Use this if the caller lacks the "
                             "'Cost Management Reader' role.")
    parser.add_argument("--cost-cache-dir", default=".",
                        help="Directory for daily cost-cache-YYYYMMDD.json files "
                             "(default: cwd)")
    parser.add_argument("--refresh-cost-data", action="store_true",
                        help="Force a fresh pull from Cost Management even if "
                             "a same-day cache exists.")
    args = parser.parse_args()

    try:
        credential = DefaultAzureCredential()
        graph_client = ResourceGraphClient(credential)
        sub_client = SubscriptionClient(credential)
    except Exception as e:
        print(f"{RED}Authentication failed: {e}{RESET}")
        logger.error(f"Authentication failed: {e}")
        return 1

    # ── Collect subscriptions ────────────────────────────────────────────────
    # Determine query scope and build subscription name lookup
    query_kwargs: dict = {}  # passed to every run_query call
    excluded = set(args.exclude_subscriptions)

    if args.subscription:
        query_kwargs["sub_ids"] = [args.subscription]
        sub_names = {args.subscription: args.subscription}
        sub_display = f"1 subscription ({args.subscription})"
    else:
        # Use tenant root management group for full tenant coverage
        first_tenant = next(sub_client.tenants.list(), None)
        if first_tenant is None:
            print(f"{RED}No tenants found.{RESET}")
            return 1
        tenant_id = first_tenant.tenant_id
        query_kwargs["mgmt_group"] = tenant_id

        # Discover all enabled subscriptions via Resource Graph for display names
        all_subs = run_query(
            graph_client,
            """ResourceContainers
| where type =~ 'microsoft.resources/subscriptions'
| where properties.state =~ 'Enabled'
| project subscriptionId, name""",
            mgmt_group=tenant_id,
        )
        sub_names = {s["subscriptionId"]: s["name"] for s in all_subs
                     if s["subscriptionId"] not in excluded}
        if excluded:
            logger.info(f"Excluded {len(excluded)} subscription(s): {excluded}")
        sub_display = f"{len(sub_names)} subscriptions (tenant-wide)"

    # ── Classify subscriptions by environment ─────────────────────────────
    sub_envs = {sid: classify_subscription(sname) for sid, sname in sub_names.items()}

    # ── Header ───────────────────────────────────────────────────────────
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print()
    print(f"{BOLD}╔══════════════════════════════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}║           AZURE ORPHANED RESOURCES REPORT                          ║{RESET}")
    print(f"{BOLD}║  Scope:     {sub_display}{RESET}")
    print(f"{BOLD}║  Generated: {now}{RESET}")
    print(f"{BOLD}╚══════════════════════════════════════════════════════════════════════╝{RESET}")
    print()

    # ── Run all Resource Graph queries in parallel ───────────────────────
    category_results: dict[str, list[dict]] = {}
    category_counts: dict[str, int] = {}

    def _run(cat_name: str, q: str) -> tuple[str, list[dict]]:
        return cat_name, run_query(graph_client, q, **query_kwargs)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_run, name, cfg["query"]): name
            for name, cfg in QUERIES.items()
        }
        # Also submit empty RG detection
        empty_rg_future = pool.submit(find_empty_rgs, graph_client, **query_kwargs)

        for future in as_completed(futures):
            name = futures[future]
            try:
                _, data = future.result()
                category_results[name] = data
            except Exception as e:
                print(f"  {RED}Query failed for {name}: {e}{RESET}")
                category_results[name] = []

        try:
            category_results["Empty Resource Groups"] = empty_rg_future.result()
        except Exception as e:
            print(f"  {RED}Empty RG query failed: {e}{RESET}")
            category_results["Empty Resource Groups"] = []

    # ── Cost Management enrichment (real cost data) ──────────────────────
    cost_result = None
    if not args.no_cost_data:
        try:
            from azure_cost_enrichment import enrich_costs

            # Collect the subscription IDs that actually turned up orphans —
            # no point querying Cost Management for subs with zero waste.
            subs_with_orphans = {
                r.get("subscriptionId", "")
                for rows in category_results.values()
                for r in rows
                if r.get("subscriptionId")
            }
            if subs_with_orphans:
                print(f"{BOLD}Enriching with Cost Management data...{RESET}")
                cost_result = enrich_costs(
                    credential,
                    sorted(subs_with_orphans),
                    cache_dir=args.cost_cache_dir,
                    use_cache=not args.refresh_cost_data,
                )
                window = cost_result.rolling30d_window
                if window[0]:
                    print(f"  Rolling 30d window: {window[0]} → {window[1]}")
                print(
                    f"  {len(cost_result.subscriptions_queried)} subscription(s) "
                    f"queried, {len(cost_result.cost_map)} resource records, "
                    f"${cost_result.total_rolling30d:,.2f} total rolling 30d spend"
                )
                if cost_result.subscriptions_failed:
                    for sid, err in cost_result.subscriptions_failed.items():
                        name = sub_names.get(sid, sid)
                        print(f"  {YELLOW}⚠  {name}: {err}{RESET}")
                print()
        except ImportError:
            print(f"  {YELLOW}azure_cost_enrichment module not available — "
                  f"using hardcoded estimates.{RESET}")
            cost_result = None
        except Exception as e:
            print(f"  {YELLOW}Cost enrichment failed ({e}) — "
                  f"falling back to hardcoded estimates.{RESET}")
            logger.warning(f"Cost enrichment failed: {e}")
            cost_result = None

    # ── JSON / CSV / HTML export ─────────────────────────────────────────
    if args.format in ("json", "csv", "html"):
        flat_rows = _collect_flat_rows(category_results, sub_names, sub_envs, cost_result)
        total_cost = sum(r["estimatedMonthlyCost"] for r in flat_rows)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        if args.format == "json":
            path = args.output or f"orphan-report-{ts}.json"
            export_json(flat_rows, path, cost_result)
        elif args.format == "csv":
            path = args.output or f"orphan-report-{ts}.csv"
            export_csv(flat_rows, path)
        else:
            path = args.output or f"orphan-dashboard-{ts}.html"
            export_html(flat_rows, path, sub_display)
        print(f"Exported {len(flat_rows)} resources to {BOLD}{path}{RESET}")
        print(f"Estimated monthly waste: {BOLD}${total_cost:,.2f}{RESET}")
        if cost_result is not None:
            real_count = sum(1 for r in flat_rows if r.get("costSource") == "costManagement")
            print(f"  {GREEN}{real_count}{RESET} rows with real Cost Management data, "
                  f"{len(flat_rows) - real_count} using fallback estimates")
        return

    # ── Print results grouped by environment ─────────────────────────────
    total_orphans = 0
    prod_orphans = 0
    nonprod_orphans = 0

    env_groups = [
        ("PRODUCTION",
         f"{BOLD}{GREEN}═══ PRODUCTION & SHARED SERVICES ═══{RESET}",
         None),
        ("NON-PRODUCTION",
         f"{BOLD}{YELLOW}═══ DEV / QA / UAT — REVIEW BEFORE CLEANUP ═══{RESET}",
         f"  {YELLOW}⚠  These resources may be intentionally reserved for active\n     development, testing, or future use. Verify with resource owners\n     before removing.{RESET}"),
    ]

    for env_key, env_banner, env_warning in env_groups:
        print(env_banner)
        if env_warning:
            print(env_warning)
        print()

        env_has_resources = False

        for section in SECTION_ORDER:
            cats_in_section = [
                (name, cfg) for name, cfg in QUERIES.items() if cfg["section"] == section
            ]
            if section == "OTHER":
                cats_in_section.insert(0, ("Empty Resource Groups", {
                    "cost": False, "extra_col": "", "section": "OTHER"
                }))

            section_printed = False

            for name, cfg in cats_in_section:
                cost = cfg.get("cost", False)
                extra_col = cfg.get("extra_col", "")
                all_rows = category_results.get(name, [])
                rows = [r for r in all_rows
                        if classify_resource(r, sub_envs) == env_key]
                if not rows:
                    continue

                env_has_resources = True
                if not section_printed:
                    print(f"{BOLD}{YELLOW}━━━ {section} ━━━{RESET}")
                    print()
                    section_printed = True

                count = len(rows)
                category_counts[name] = category_counts.get(name, 0) + count
                total_orphans += count
                if env_key == "PRODUCTION":
                    prod_orphans += count
                else:
                    nonprod_orphans += count

                cost_tag = f" {RED}[COST]{RESET}" if cost else ""
                print(f"{BOLD}{CYAN}── {name}{cost_tag}{RESET}")
                print_table(rows, sub_names, extra_col)

        if not env_has_resources:
            print(f"  {GREEN}No orphaned resources found in this environment.{RESET}")
            print()

    # ── Summary ──────────────────────────────────────────────────────────
    print()
    print(f"{BOLD}╔══════════════════════════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}║                         SUMMARY                                ║{RESET}")
    print(f"{BOLD}╠══════════════════════════════════════════════════════════════════╣{RESET}")

    for name, count in sorted(category_counts.items(), key=lambda x: -x[1]):
        if count > 0:
            print(f"{BOLD}║{RESET}  {name:<50s} {count:>5}     {BOLD}║{RESET}")
        else:
            print(f"║  {name:<50s} {count:>5}     ║")

    print(f"{BOLD}╠══════════════════════════════════════════════════════════════════╣{RESET}")
    print(f"{BOLD}║  {'TOTAL ORPHANED RESOURCES':<50s} {total_orphans:>5}     ║{RESET}")
    print(f"{BOLD}╠══════════════════════════════════════════════════════════════════╣{RESET}")
    print(f"{BOLD}║{RESET}  {GREEN}{'Production / Shared Services':<50s} {prod_orphans:>5}{RESET}     {BOLD}║{RESET}")
    print(f"{BOLD}║{RESET}  {YELLOW}{'Dev / QA / UAT':<50s} {nonprod_orphans:>5}{RESET}     {BOLD}║{RESET}")
    print(f"{BOLD}╚══════════════════════════════════════════════════════════════════╝{RESET}")
    print()

    # ── Monthly waste total (console path) ──────────────────────────────
    if total_orphans > 0:
        console_rows = _collect_flat_rows(category_results, sub_names, sub_envs, cost_result)
        total_waste = sum(r["estimatedMonthlyCost"] for r in console_rows)
        total_rolling = sum(r.get("rolling30dCost", 0.0) for r in console_rows)
        print(f"{BOLD}Estimated monthly waste: {RED}${total_waste:,.2f}{RESET}")
        if cost_result is not None and total_rolling > 0:
            print(f"  Real Cost Management rolling 30d: {BOLD}${total_rolling:,.2f}{RESET}")
            real_count = sum(1 for r in console_rows if r.get("costSource") == "costManagement")
            print(f"  ({real_count} of {len(console_rows)} rows backed by Cost Management data)")
        print()

    if total_orphans > 0:
        print(f"{BOLD}Cleanup commands:{RESET}")
        print(f"  {GREEN}python3 orphan_cleanup.py --production-only --dry-run{RESET}  ← Production only (recommended)")
        print(f"  {YELLOW}python3 orphan_cleanup.py --dry-run{RESET}                    ← All environments")
        if nonprod_orphans > 0:
            print()
            print(f"  {YELLOW}⚠  {nonprod_orphans} resource(s) are in Dev/QA/UAT subscriptions and may be")
            print(f"     intentionally reserved. Verify with resource owners before cleanup.{RESET}")
    print()

    logger.info(f"Report complete: {total_orphans} orphans (prod={prod_orphans}, nonprod={nonprod_orphans})")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
