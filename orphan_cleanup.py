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
import sys
import time
from datetime import datetime, timezone

from azure.identity import DefaultAzureCredential
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.subscription import SubscriptionClient
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.web import WebSiteManagementClient
from azure.core.exceptions import HttpResponseError

# Single source of truth for orphan queries, classification, and safety
# filters is orphan_report.py — cleanup must never drift from what the
# report shows. Categories without an entry in DELETION_ORDER below are
# REPORT-ONLY and are never touched by this tool.
from orphan_report import (
    QUERIES,
    classify_resource,
    classify_subscription,
    find_empty_rgs,
    has_do_not_delete_tag,
    run_query,
)

# ── Colors ───────────────────────────────────────────────────────────────────
BOLD = "\033[1m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
GREEN = "\033[32m"
RESET = "\033[0m"


# ── Logging ──────────────────────────────────────────────────────────────────
LOG_FILE = "orphan-cleanup.log"
logger = logging.getLogger("orphan-cleanup")
logger.setLevel(logging.INFO)
fh = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
fh.setFormatter(logging.Formatter("[%(asctime)s UTC] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
fh.converter = time.gmtime
logger.addHandler(fh)


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
# (display_name, QUERIES key, deleter_func) — query text comes from
# orphan_report.QUERIES so report and cleanup always agree on what counts
# as an orphan. Categories present in QUERIES but absent here (stopped or
# deallocated VMs, ExpressRoute circuits, snapshots, backup items, Bastion,
# IP prefixes, images, AVD app groups, managed identities) are REPORT-ONLY:
# this tool will never delete them.
DELETION_ORDER = [
    ("Private Endpoint", "Private Endpoints not connected to a resource",
     _delete_network_resource("private_endpoint")),
    ("Application Gateway", "Application Gateways with empty backend pools",
     _delete_network_resource("application_gateway")),
    ("Load Balancer", "Load Balancers with empty backend pools",
     _delete_network_resource("load_balancer")),
    ("VNet Gateway", "VNet Gateways with no connections",
     _delete_network_resource("vnet_gateway")),
    ("NIC", "NICs not attached to a VM",
     _delete_network_resource("nic")),
    ("Public IP", "Unassociated Public IPs",
     _delete_network_resource("public_ip")),
    ("NSG", "NSGs not associated with subnet or NIC",
     _delete_network_resource("nsg")),
    ("Route Table", "Route Tables not associated with a subnet",
     _delete_network_resource("route_table")),
    ("NAT Gateway", "NAT Gateways not associated with a subnet",
     _delete_network_resource("nat_gateway")),
    ("Front Door WAF Policy", "Front Door WAF Policies not linked to a Front Door",
     _delete_generic),
    ("Traffic Manager Profile", "Traffic Manager Profiles with no endpoints",
     _delete_generic),
    ("IP Group", "IP Groups not referenced by any firewall",
     _delete_network_resource("ip_group")),
    ("DDoS Protection Plan", "DDoS Protection Plans with no associated VNets",
     _delete_network_resource("ddos_protection_plan")),
    ("Private DNS Zone", "Private DNS Zones with no VNet links",
     _delete_generic),
    ("Subnet", "Subnets without connected devices",
     _delete_subnet),
    ("Virtual Network", "Virtual Networks with no subnets",
     _delete_network_resource("virtual_network")),
    ("Managed Disk", "Unattached Managed Disks",
     _delete_disk),
    ("Availability Set", "Availability Sets with no VMs",
     _delete_availability_set),
    ("App Service Plan", "App Service Plans with no apps",
     _delete_app_service_plan),
    ("SQL Elastic Pool", "SQL Elastic Pools with no databases",
     _delete_generic),
    ("Expired Certificate", "Expired App Service Certificates",
     _delete_generic),
    ("API Connection", "Disconnected API Connections",
     _delete_generic),
]

# Sanity check at import time: every cleanup category must exist in QUERIES.
for _disp, _key, _ in DELETION_ORDER:
    if _key not in QUERIES:
        raise KeyError(f"DELETION_ORDER references unknown category: {_key!r}")


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Azure Orphaned Resources Cleanup")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True, help="Preview only (default)")
    mode.add_argument("--confirm", action="store_true", help="Actually delete resources")
    parser.add_argument("--subscription", "-s", help="Scope to a single subscription ID")
    parser.add_argument("--exclude-subscriptions", nargs="+", default=[],
                        help="Subscription IDs to exclude from cleanup")
    parser.add_argument("--production-only", action="store_true",
                        help="Only clean up resources in production subscriptions (skip Dev/QA/UAT)")
    args = parser.parse_args()

    dry_run = not args.confirm

    try:
        credential = DefaultAzureCredential()
        graph_client = ResourceGraphClient(credential)
        sub_client = SubscriptionClient(credential)
    except Exception as e:
        print(f"{RED}Authentication failed: {e}{RESET}")
        logger.error(f"Authentication failed: {e}")
        return 1

    # ── Collect subscriptions ────────────────────────────────────────────────
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
    report_only = [c for c in QUERIES if c not in {k for _, k, _ in DELETION_ORDER}]
    if report_only:
        print(f"{YELLOW}{len(report_only)} categories are report-only and will "
              f"never be deleted by this tool (run orphan_report.py to see them).{RESET}")
        print()

    print(f"{BOLD}Processing deletions in safe dependency order...{RESET}")
    print()

    for category, query_key, deleter in DELETION_ORDER:
        query = QUERIES[query_key]["query"]
        try:
            resources = run_query(graph_client, query, **query_kwargs)
        except Exception as e:
            print(f"  {RED}Query failed for {category}: {e}{RESET}")
            logger.info(f"[QUERY-FAILED] {category}: {e}")
            continue

        # Excluded subscriptions: tenant-wide scans scope by management
        # group, so excluded subs still come back — drop them HERE, before
        # anything can be deleted from them.
        if excluded:
            resources = [r for r in resources
                         if r.get("subscriptionId") not in excluded]

        # Honor the DoNotDelete tag across every category.
        dnd_count = sum(1 for r in resources if has_do_not_delete_tag(r))
        if dnd_count:
            print(f"  {YELLOW}({dnd_count} {category} resource(s) skipped — DoNotDelete tag){RESET}")
            logger.info(f"[SKIPPED-DND] {category}: {dnd_count} tagged DoNotDelete")
            resources = [r for r in resources if not has_do_not_delete_tag(r)]

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

    # Same safety filters as the category loop.
    if excluded:
        empty_rgs = [r for r in empty_rgs
                     if r.get("subscriptionId") not in excluded]
    rg_dnd = sum(1 for r in empty_rgs if has_do_not_delete_tag(r))
    if rg_dnd:
        print(f"  {YELLOW}({rg_dnd} resource group(s) skipped — DoNotDelete tag){RESET}")
        logger.info(f"[SKIPPED-DND] Empty Resource Groups: {rg_dnd}")
        empty_rgs = [r for r in empty_rgs if not has_do_not_delete_tag(r)]

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

    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main() or 0)
