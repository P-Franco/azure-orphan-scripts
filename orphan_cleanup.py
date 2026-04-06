#!/usr/bin/env python3
"""
orphan_cleanup.py

Deletes orphaned Azure resources across ALL subscriptions in the current
tenant (or a single subscription if --subscription is specified).
Resources are deleted in safe dependency order.

Prerequisites:
  - pip install -r requirements.txt
  - Azure CLI authenticated (az login) or other DefaultAzureCredential method

Usage:
  python3 orphan_cleanup.py                        # Dry-run, all subs (default)
  python3 orphan_cleanup.py --dry-run              # Same as above, explicit
  python3 orphan_cleanup.py --confirm              # Delete across all subs
  python3 orphan_cleanup.py --subscription <id>    # Scope to one subscription

WARNING: --confirm will PERMANENTLY DELETE resources. Review dry-run output
         carefully before running with --confirm.
"""

import argparse
import logging
import re
import sys
import time
from datetime import datetime, timezone

from azure.identity import DefaultAzureCredential
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest, QueryRequestOptions
from azure.mgmt.subscription import SubscriptionClient
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.web import WebSiteManagementClient
from azure.core.exceptions import HttpResponseError

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


_ENV_TAG_KEYS = {"environment", "env"}
_NON_PROD_PATTERNS = [
    re.compile(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])")
    for kw in NON_PROD_KEYWORDS
]


def classify_resource(resource: dict, sub_envs: dict) -> str:
    """Classify a resource using tags, naming conventions, then subscription fallback."""
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
    name_lower = resource.get("name", "").lower()
    rg_lower = resource.get("resourceGroup", "").lower()
    for pattern in _NON_PROD_PATTERNS:
        if pattern.search(name_lower) or pattern.search(rg_lower):
            return "NON-PRODUCTION"
    sub_id = resource.get("subscriptionId", "")
    return sub_envs.get(sub_id, "PRODUCTION")


# ── Logging ──────────────────────────────────────────────────────────────────
LOG_FILE = "orphan-cleanup.log"
logger = logging.getLogger("orphan-cleanup")
logger.setLevel(logging.INFO)
fh = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
fh.setFormatter(logging.Formatter("[%(asctime)s UTC] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
fh.converter = time.gmtime
logger.addHandler(fh)


# ── Resource Graph helper ────────────────────────────────────────────────────
def run_query(
    graph_client: ResourceGraphClient,
    query: str,
    *,
    sub_ids: list[str] | None = None,
    mgmt_group: str | None = None,
) -> list[dict]:
    """Run a Resource Graph query, handling pagination.
    Scope by subscription IDs or management group (tenant root for tenant-wide).
    """
    results = []
    options = QueryRequestOptions(result_format="objectArray")
    kwargs = {"query": query, "options": options}
    if mgmt_group:
        kwargs["management_groups"] = [mgmt_group]
    elif sub_ids:
        kwargs["subscriptions"] = sub_ids
    request = QueryRequest(**kwargs)
    response = graph_client.resources(request)
    results.extend(response.data)

    while response.skip_token:
        options.skip_token = response.skip_token
        response = graph_client.resources(request)
        results.extend(response.data)

    return results


# ── Empty Resource Groups (Resource Graph) ───────────────────────────────────
def find_empty_rgs(graph_client: ResourceGraphClient, **query_kwargs) -> list[dict]:
    """Find empty resource groups using 2 Resource Graph queries + set difference."""
    all_rgs = run_query(
        graph_client,
        """ResourceContainers
| where type =~ 'microsoft.resources/subscriptions/resourcegroups'
| project name, resourceGroup=name, location, subscriptionId""",
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


# ── Deletion functions ───────────────────────────────────────────────────────
def delete_by_resource_id(credential, resource_id: str, api_version: str = "2021-04-01") -> None:
    """Generic delete using ResourceManagementClient.resources.begin_delete_by_id."""
    from azure.mgmt.resource import ResourceManagementClient
    # Extract subscription from resource ID
    parts = resource_id.split("/")
    sub_idx = parts.index("subscriptions") + 1
    sub_id = parts[sub_idx]
    client = ResourceManagementClient(credential, sub_id)
    poller = client.resources.begin_delete_by_id(resource_id, api_version)
    poller.result()  # Wait for completion


def delete_resource_group(credential, rg_name: str, sub_id: str) -> None:
    """Delete an empty resource group."""
    client = ResourceManagementClient(credential, sub_id)
    poller = client.resource_groups.begin_delete(rg_name)
    # Don't wait — RG deletes can be slow; fire and forget
    _ = poller


# ── Resource-type-specific deleters ──────────────────────────────────────────
# Each returns a callable(credential, resource) -> None
# Using specific SDK clients for correct API versions

def _delete_network_resource(resource_type: str):
    """Return a deleter for a network resource type."""
    def _delete(credential, r: dict) -> None:
        sub_id = r["subscriptionId"]
        rg = r["resourceGroup"]
        name = r["name"]
        client = NetworkManagementClient(credential, sub_id)
        method_map = {
            "private_endpoint": client.private_endpoints.begin_delete,
            "application_gateway": client.application_gateways.begin_delete,
            "load_balancer": client.load_balancers.begin_delete,
            "vnet_gateway": client.virtual_network_gateways.begin_delete,
            "nic": client.network_interfaces.begin_delete,
            "public_ip": client.public_ip_addresses.begin_delete,
            "nsg": client.network_security_groups.begin_delete,
            "route_table": client.route_tables.begin_delete,
            "nat_gateway": client.nat_gateways.begin_delete,
            "ip_group": client.ip_groups.begin_delete,
            "ddos_protection_plan": client.ddos_protection_plans.begin_delete,
            "virtual_network": client.virtual_networks.begin_delete,
        }
        poller = method_map[resource_type](rg, name)
        poller.result()
    return _delete


def _delete_disk(credential, r: dict) -> None:
    sub_id = r["subscriptionId"]
    client = ComputeManagementClient(credential, sub_id)
    poller = client.disks.begin_delete(r["resourceGroup"], r["name"])
    poller.result()


def _delete_availability_set(credential, r: dict) -> None:
    sub_id = r["subscriptionId"]
    client = ComputeManagementClient(credential, sub_id)
    client.availability_sets.delete(r["resourceGroup"], r["name"])


def _delete_app_service_plan(credential, r: dict) -> None:
    sub_id = r["subscriptionId"]
    client = WebSiteManagementClient(credential, sub_id)
    client.app_service_plans.delete(r["resourceGroup"], r["name"])


def _delete_subnet(credential, r: dict) -> None:
    sub_id = r["subscriptionId"]
    client = NetworkManagementClient(credential, sub_id)
    vnet_name = r.get("vnetName", "")
    poller = client.subnets.begin_delete(r["resourceGroup"], vnet_name, r["name"])
    poller.result()


def _delete_generic(credential, r: dict) -> None:
    """Fallback: delete via resource ID."""
    delete_by_resource_id(credential, r["id"])


def _delete_rg(credential, r: dict) -> None:
    delete_resource_group(credential, r["name"], r["subscriptionId"])


# ── Deletion categories in safe dependency order ─────────────────────────────
DELETION_ORDER = [
    # (category_name, query, deleter_func)
    ("Private Endpoint", """Resources
| where type =~ 'microsoft.network/privateendpoints'
| where isnull(properties.privateLinkServiceConnections) or array_length(properties.privateLinkServiceConnections) == 0
| where isnull(properties.manualPrivateLinkServiceConnections) or array_length(properties.manualPrivateLinkServiceConnections) == 0
| project id, name, resourceGroup, subscriptionId, location""",
     _delete_network_resource("private_endpoint")),

    ("Application Gateway", """Resources
| where type =~ 'microsoft.network/applicationgateways'
| where properties.backendAddressPools == '[]' or array_length(properties.backendAddressPools) == 0
| project id, name, resourceGroup, subscriptionId, location""",
     _delete_network_resource("application_gateway")),

    ("Load Balancer", """Resources
| where type =~ 'microsoft.network/loadbalancers'
| where properties.backendAddressPools == '[]' or array_length(properties.backendAddressPools) == 0
| project id, name, resourceGroup, subscriptionId, location""",
     _delete_network_resource("load_balancer")),

    ("VNet Gateway", """Resources
| where type =~ 'microsoft.network/virtualnetworkgateways'
| join kind=leftouter (
  Resources
  | where type =~ 'microsoft.network/connections'
  | mv-expand gw = pack_array(properties.virtualNetworkGateway1.id, properties.virtualNetworkGateway2.id)
  | project connectionGwId=tolower(tostring(gw))
) on $left.id == $right.connectionGwId
| where isnull(connectionGwId)
| project id, name, resourceGroup, subscriptionId, location""",
     _delete_network_resource("vnet_gateway")),

    ("NIC", """Resources
| where type =~ 'microsoft.network/networkinterfaces'
| where isnull(properties.virtualMachine) or properties.virtualMachine == ''
| where isnull(properties.privateEndpoint) or properties.privateEndpoint == ''
| project id, name, resourceGroup, subscriptionId, location""",
     _delete_network_resource("nic")),

    ("Public IP", """Resources
| where type =~ 'microsoft.network/publicipaddresses'
| where properties.ipConfiguration == '' or isnull(properties.ipConfiguration)
| where properties.natGateway == '' or isnull(properties.natGateway)
| project id, name, resourceGroup, subscriptionId, location""",
     _delete_network_resource("public_ip")),

    ("NSG", """Resources
| where type =~ 'microsoft.network/networksecuritygroups'
| where isnull(properties.networkInterfaces) or properties.networkInterfaces == '[]' or array_length(properties.networkInterfaces) == 0
| where isnull(properties.subnets) or properties.subnets == '[]' or array_length(properties.subnets) == 0
| project id, name, resourceGroup, subscriptionId, location""",
     _delete_network_resource("nsg")),

    ("Route Table", """Resources
| where type =~ 'microsoft.network/routetables'
| where isnull(properties.subnets) or properties.subnets == '[]' or array_length(properties.subnets) == 0
| project id, name, resourceGroup, subscriptionId, location""",
     _delete_network_resource("route_table")),

    ("NAT Gateway", """Resources
| where type =~ 'microsoft.network/natgateways'
| where isnull(properties.subnets) or properties.subnets == '[]' or array_length(properties.subnets) == 0
| project id, name, resourceGroup, subscriptionId, location""",
     _delete_network_resource("nat_gateway")),

    ("Front Door WAF Policy", """Resources
| where type =~ 'microsoft.network/frontdoorwebapplicationfirewallpolicies'
| where (isnull(properties.frontendEndpointLinks) or array_length(properties.frontendEndpointLinks) == 0)
| where (isnull(properties.securityPolicyLinks) or array_length(properties.securityPolicyLinks) == 0)
| project id, name, resourceGroup, subscriptionId, location""",
     _delete_generic),

    ("Traffic Manager Profile", """Resources
| where type =~ 'microsoft.network/trafficmanagerprofiles'
| where properties.endpoints == '[]' or array_length(properties.endpoints) == 0
| project id, name, resourceGroup, subscriptionId, location""",
     _delete_generic),

    ("IP Group", """Resources
| where type =~ 'microsoft.network/ipgroups'
| where (isnull(properties.firewalls) or array_length(properties.firewalls) == 0)
| where (isnull(properties.firewallPolicies) or array_length(properties.firewallPolicies) == 0)
| project id, name, resourceGroup, subscriptionId, location""",
     _delete_network_resource("ip_group")),

    ("DDoS Protection Plan", """Resources
| where type =~ 'microsoft.network/ddosprotectionplans'
| where isnull(properties.virtualNetworks) or properties.virtualNetworks == '[]' or array_length(properties.virtualNetworks) == 0
| project id, name, resourceGroup, subscriptionId, location""",
     _delete_network_resource("ddos_protection_plan")),

    ("Private DNS Zone", """Resources
| where type =~ 'microsoft.network/privatednszones'
| where properties.numberOfVirtualNetworkLinks == 0
| project id, name, resourceGroup, subscriptionId, location""",
     _delete_generic),

    ("Subnet", """Resources
| where type =~ 'microsoft.network/virtualnetworks'
| mv-expand subnet = properties.subnets
| where subnet.name !in~ ('GatewaySubnet', 'AzureFirewallSubnet', 'AzureFirewallManagementSubnet', 'AzureBastionSubnet', 'RouteServerSubnet')
| where (isnull(subnet.properties.ipConfigurations) or array_length(subnet.properties.ipConfigurations) == 0)
| where (isnull(subnet.properties.privateEndpoints) or array_length(subnet.properties.privateEndpoints) == 0)
| where (isnull(subnet.properties.delegations) or array_length(subnet.properties.delegations) == 0)
| extend subnetName = tostring(subnet.name)
| project id=subnet.id, name=subnetName, resourceGroup, subscriptionId, location, vnetName=name""",
     _delete_subnet),

    ("Virtual Network", """Resources
| where type =~ 'microsoft.network/virtualnetworks'
| where isnull(properties.subnets) or array_length(properties.subnets) == 0
| project id, name, resourceGroup, subscriptionId, location""",
     _delete_network_resource("virtual_network")),

    ("Managed Disk", """Resources
| where type =~ 'microsoft.compute/disks'
| where properties.diskState =~ 'Unattached'
| project id, name, resourceGroup, subscriptionId, location""",
     _delete_disk),

    ("Availability Set", """Resources
| where type =~ 'microsoft.compute/availabilitysets'
| where properties.virtualMachines == '[]' or array_length(properties.virtualMachines) == 0
| project id, name, resourceGroup, subscriptionId, location""",
     _delete_availability_set),

    ("App Service Plan", """Resources
| where type =~ 'microsoft.web/serverfarms'
| where properties.numberOfSites == 0
| project id, name, resourceGroup, subscriptionId, location""",
     _delete_app_service_plan),

    ("SQL Elastic Pool", """Resources
| where type =~ 'microsoft.sql/servers/elasticpools'
| where isnull(properties.perDatabaseSettings) or properties.numberOfDatabases == 0
| project id, name, resourceGroup, subscriptionId, location""",
     _delete_generic),

    ("Expired Certificate", """Resources
| where type =~ 'microsoft.web/certificates'
| where properties.expirationDate < now()
| project id, name, resourceGroup, subscriptionId, location""",
     _delete_generic),

    ("API Connection", """Resources
| where type =~ 'microsoft.web/connections'
| where isnotnull(properties.statuses)
| where array_length(properties.statuses) > 0
| extend connStatus = tostring(properties.statuses[0]['status'])
| where connStatus !in~ ('Connected', 'Ready')
| project id, name, resourceGroup, subscriptionId, location""",
     _delete_generic),
]


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Azure Orphaned Resources Cleanup")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True, help="Preview only (default)")
    mode.add_argument("--confirm", action="store_true", help="Actually delete resources")
    parser.add_argument("--subscription", "-s", help="Scope to a single subscription ID")
    parser.add_argument("--production-only", action="store_true",
                        help="Only clean up resources in production subscriptions (skip Dev/QA/UAT)")
    args = parser.parse_args()

    dry_run = not args.confirm

    credential = DefaultAzureCredential()
    graph_client = ResourceGraphClient(credential)
    sub_client = SubscriptionClient(credential)

    # ── Collect subscriptions ────────────────────────────────────────────────
    query_kwargs: dict = {}  # passed to every run_query call

    if args.subscription:
        query_kwargs["sub_ids"] = [args.subscription]
        sub_names = {args.subscription: args.subscription}
        sub_display = f"1 subscription ({args.subscription})"
    else:
        # Use tenant root management group for full tenant coverage
        first_tenant = next(sub_client.tenants.list(), None)
        if first_tenant is None:
            print(f"{RED}No tenants found.{RESET}")
            sys.exit(1)
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
        sub_names = {s["subscriptionId"]: s["name"] for s in all_subs}
        sub_display = f"{len(all_subs)} subscriptions (tenant-wide)"

    # ── Classify subscriptions by environment ─────────────────────────────
    sub_envs = {sid: classify_subscription(sname) for sid, sname in sub_names.items()}

    logger.info("=" * 50)
    logger.info(f"orphan_cleanup.py started (DRY_RUN={dry_run}, PRODUCTION_ONLY={args.production_only})")
    logger.info(f"Scope: {sub_display}")
    logger.info("=" * 50)

    # ── Header ───────────────────────────────────────────────────────────────
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    mode_str = f"{YELLOW}DRY-RUN{RESET}{BOLD} (no resources will be deleted)" if dry_run else f"{RED}CONFIRM{RESET}{BOLD} (resources WILL be deleted!)"
    print()
    print(f"{BOLD}╔══════════════════════════════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}║           AZURE ORPHANED RESOURCES CLEANUP                          ║{RESET}")
    print(f"{BOLD}║  Scope:     {sub_display}{RESET}")
    print(f"{BOLD}║  Mode:      {mode_str}{RESET}")
    print(f"{BOLD}║  Timestamp: {now}{RESET}")
    if args.production_only:
        print(f"{BOLD}║  Filter:    {GREEN}Production only{RESET}{BOLD} (Dev/QA/UAT will be skipped){RESET}")
    print(f"{BOLD}╚══════════════════════════════════════════════════════════════════════╝{RESET}")
    print()

    if not dry_run:
        print(f"{RED}{BOLD}⚠  WARNING: This will PERMANENTLY DELETE resources. You have 5 seconds to cancel (Ctrl+C)...{RESET}")
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            print(f"\n{YELLOW}Cancelled.{RESET}")
            sys.exit(0)
        print()

    # ── Discovery ────────────────────────────────────────────────────────────
    print(f"{BOLD}Discovering orphaned resources...{RESET}")
    print()

    deleted = 0
    failed = 0
    skipped = 0

    # ── Process each category in dependency order ────────────────────────────
    print(f"{BOLD}Processing deletions in safe dependency order...{RESET}")
    print()

    for category, query, deleter in DELETION_ORDER:
        try:
            resources = run_query(graph_client, query, **query_kwargs)
        except Exception as e:
            print(f"  {RED}Query failed for {category}: {e}{RESET}")
            logger.info(f"[QUERY-FAILED] {category}: {e}")
            continue

        # Filter by environment if --production-only
        nonprod_skipped = 0
        if args.production_only:
            all_count = len(resources)
            resources = [r for r in resources
                         if classify_resource(r, sub_envs) == "PRODUCTION"]
            nonprod_skipped = all_count - len(resources)
            skipped += nonprod_skipped

        if not resources:
            if nonprod_skipped > 0:
                print(f"{BOLD}{CYAN}── {category}{RESET}")
                print(f"  {YELLOW}({nonprod_skipped} non-production resource(s) skipped){RESET}")
                print()
            continue

        print(f"{BOLD}{CYAN}── {category} ({len(resources)} resources){RESET}")
        if nonprod_skipped > 0:
            print(f"  {YELLOW}({nonprod_skipped} non-production resource(s) skipped){RESET}")
        logger.info(f"Category: {category} — {len(resources)} resources (skipped {nonprod_skipped} non-prod)")

        for r in resources:
            name = r.get("name", "N/A")
            rg = r.get("resourceGroup", "N/A")
            sub_id = r.get("subscriptionId", "")
            sub_label = sub_names.get(sub_id, sub_id)
            env = classify_resource(r, sub_envs)
            env_tag = f" {GREEN}[PROD]{RESET}" if env == "PRODUCTION" else f" {YELLOW}[NON-PROD]{RESET}"

            if dry_run:
                print(f"  {YELLOW}[DRY-RUN]{RESET} Would delete {category}: {BOLD}{name}{RESET} in {BOLD}{rg}{RESET} ({sub_label}){env_tag}")
                logger.info(f"[DRY-RUN] Would delete {category}: {name} in {rg} (sub={sub_id}, env={env})")
                skipped += 1
            else:
                print(f"  {CYAN}[DELETING]{RESET} {category}: {BOLD}{name}{RESET} in {BOLD}{rg}{RESET} ({sub_label}){env_tag}")
                logger.info(f"[DELETING] {category}: {name} in {rg} (sub={sub_id}, env={env})")
                try:
                    deleter(credential, r)
                    print(f"  {GREEN}[DONE]{RESET} {name}")
                    logger.info(f"[DONE] {name}")
                    deleted += 1
                except (HttpResponseError, Exception) as e:
                    err_msg = str(e).split("\n")[0][:120]
                    print(f"  {RED}[FAILED]{RESET} {name}: {err_msg}")
                    logger.info(f"[FAILED] {name}: {err_msg}")
                    failed += 1

        print()

    # ── Empty Resource Groups (last) ─────────────────────────────────────────
    empty_rgs = find_empty_rgs(graph_client, **query_kwargs)

    # Filter by environment if --production-only
    rg_nonprod_skipped = 0
    if args.production_only:
        all_rg_count = len(empty_rgs)
        empty_rgs = [r for r in empty_rgs
                     if classify_resource(r, sub_envs) == "PRODUCTION"]
        rg_nonprod_skipped = all_rg_count - len(empty_rgs)
        skipped += rg_nonprod_skipped

    if empty_rgs:
        print(f"{BOLD}{CYAN}── Empty Resource Groups ({len(empty_rgs)} resources){RESET}")
        if rg_nonprod_skipped > 0:
            print(f"  {YELLOW}({rg_nonprod_skipped} non-production resource group(s) skipped){RESET}")
        logger.info(f"Category: Empty Resource Groups — {len(empty_rgs)} resources (skipped {rg_nonprod_skipped} non-prod)")

        for r in empty_rgs:
            rg_name = r.get("name", "N/A")
            sub_id = r.get("subscriptionId", "")
            sub_label = sub_names.get(sub_id, sub_id)
            env = classify_resource(r, sub_envs)
            env_tag = f" {GREEN}[PROD]{RESET}" if env == "PRODUCTION" else f" {YELLOW}[NON-PROD]{RESET}"

            if dry_run:
                print(f"  {YELLOW}[DRY-RUN]{RESET} Would delete Resource Group: {BOLD}{rg_name}{RESET} in {BOLD}{sub_label}{RESET}{env_tag}")
                logger.info(f"[DRY-RUN] Would delete Resource Group: {rg_name} (sub={sub_id}, env={env})")
                skipped += 1
            else:
                print(f"  {CYAN}[DELETING]{RESET} Resource Group: {BOLD}{rg_name}{RESET} in {BOLD}{sub_label}{RESET}{env_tag}")
                logger.info(f"[DELETING] Resource Group: {rg_name} (sub={sub_id}, env={env})")
                try:
                    _delete_rg(credential, r)
                    print(f"  {GREEN}[DONE]{RESET} {rg_name}")
                    logger.info(f"[DONE] {rg_name}")
                    deleted += 1
                except (HttpResponseError, Exception) as e:
                    err_msg = str(e).split("\n")[0][:120]
                    print(f"  {RED}[FAILED]{RESET} {rg_name}: {err_msg}")
                    logger.info(f"[FAILED] {rg_name}: {err_msg}")
                    failed += 1

        print()
    elif rg_nonprod_skipped > 0:
        print(f"{BOLD}{CYAN}── Empty Resource Groups{RESET}")
        print(f"  {YELLOW}({rg_nonprod_skipped} non-production resource group(s) skipped){RESET}")
        print()

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"{BOLD}╔══════════════════════════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}║                    CLEANUP SUMMARY                             ║{RESET}")
    print(f"{BOLD}╠══════════════════════════════════════════════════════════════════╣{RESET}")
    if dry_run:
        print(f"{BOLD}║{RESET}  {'Skipped (dry-run):':<45s} {YELLOW}{skipped:>5}{RESET}          {BOLD}║{RESET}")
        print(f"{BOLD}║{RESET}  {'Deleted:':<45s} {deleted:>5}          {BOLD}║{RESET}")
        print(f"{BOLD}║{RESET}  {'Failed:':<45s} {failed:>5}          {BOLD}║{RESET}")
    else:
        print(f"{BOLD}║{RESET}  {'Deleted:':<45s} {GREEN}{deleted:>5}{RESET}          {BOLD}║{RESET}")
        print(f"{BOLD}║{RESET}  {'Failed:':<45s} {RED}{failed:>5}{RESET}          {BOLD}║{RESET}")
        print(f"{BOLD}║{RESET}  {'Skipped:':<45s} {skipped:>5}          {BOLD}║{RESET}")
    print(f"{BOLD}╚══════════════════════════════════════════════════════════════════╝{RESET}")
    print()

    logger.info("=" * 50)
    logger.info(f"Cleanup finished: DELETED={deleted} FAILED={failed} SKIPPED={skipped}")
    logger.info("=" * 50)

    print(f"Full log: {BOLD}{LOG_FILE}{RESET}")
    print()


if __name__ == "__main__":
    main()
