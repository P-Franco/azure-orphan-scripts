"""Offline test suite for the orphan scanner toolchain.

Everything Azure is mocked — these tests verify query structure (regression
guards for the KQL bugs fixed in June 2026), Python plumbing, filters,
export formats, and the Cost Management enrichment module. Live KQL
semantics still need a real tenant (az login + run in Cloud Shell).

Run:  python -m pytest tests/ -v
"""

import csv
import io
import json
import os
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import orphan_report  # noqa: E402
import azure_cost_enrichment as ace  # noqa: E402
from orphan_report import (  # noqa: E402
    COST_ESTIMATES,
    QUERIES,
    SECTION_ORDER,
    _flatten_row,
    apply_subscription_exclusions,
    classify_resource,
    classify_subscription,
    export_csv,
    export_html,
    export_json,
    filter_do_not_delete,
    find_empty_rgs,
    has_do_not_delete_tag,
)


# ── Query structural integrity ────────────────────────────────────────────────
REQUIRED_PROJECTION_TOKENS = ["name", "resourceGroup", "location",
                              "subscriptionId", "tags"]


@pytest.mark.parametrize("category", sorted(QUERIES))
def test_query_config_shape(category):
    cfg = QUERIES[category]
    assert isinstance(cfg["query"], str) and cfg["query"].strip()
    assert isinstance(cfg["cost"], bool)
    assert isinstance(cfg["extra_col"], str)
    assert cfg["section"] in SECTION_ORDER


@pytest.mark.parametrize("category", sorted(QUERIES))
def test_query_projects_required_columns(category):
    q = QUERIES[category]["query"]
    for token in REQUIRED_PROJECTION_TOKENS:
        assert token in q, f"{category} query missing {token}"


@pytest.mark.parametrize("category", sorted(QUERIES))
def test_every_category_has_cost_estimate(category):
    assert category in COST_ESTIMATES, (
        f"{category} has no COST_ESTIMATES entry — flatten would default to "
        f"$0 silently")


def test_no_orphaned_cost_estimates():
    known = set(QUERIES) | {"Empty Resource Groups"}
    extras = set(COST_ESTIMATES) - known
    assert not extras, f"COST_ESTIMATES has entries for unknown categories: {extras}"


def test_leftouter_joins_use_isempty_not_isnull():
    """Regression: isnull() on a string column ALWAYS returns false in KQL,
    so an unmatched-join check via isnull silently hides every orphan."""
    for category, cfg in QUERIES.items():
        q = cfg["query"]
        if "kind=leftouter" in q:
            assert "isempty(" in q, (
                f"{category} uses leftouter join but never checks isempty() "
                f"for unmatched rows")
            # The specific historical bug:
            assert "where isnull(connectionGwId)" not in q


def test_vnet_gateway_query_regression():
    q = QUERIES["VNet Gateways with no connections"]["query"]
    # Left side must be lowercased BEFORE the case-sensitive join.
    assert "extend id = tolower(id)" in q
    assert "isempty(connectionGwId)" in q
    assert "tolower(tostring(gw))" in q


def test_elastic_pool_query_regression():
    q = QUERIES["SQL Elastic Pools with no databases"]["query"]
    # numberOfDatabases does not exist in Resource Graph — membership lives
    # on databases' elasticPoolId.
    assert "numberOfDatabases" not in q
    assert "elasticPoolId" in q
    assert "isempty(poolId)" in q
    assert QUERIES["SQL Elastic Pools with no databases"]["cost"] is True


def test_expired_cert_query_regression():
    q = QUERIES["Expired App Service Certificates"]["query"]
    assert "todatetime(properties.expirationDate) < now()" in q


def test_snapshot_query_uses_todatetime():
    q = QUERIES["Disk Snapshots stale or source-deleted"]["query"]
    assert "todatetime(properties.timeCreated)" in q
    assert "isempty(diskId)" in q


def test_subnet_query_covers_app_gateways():
    q = QUERIES["Subnets without connected devices"]["query"]
    assert "applicationGatewayIPConfigurations" in q
    assert "vnetName=name" in q  # cleanup's subnet deleter needs this


def test_private_endpoint_query_catches_disconnected():
    q = QUERIES["Private Endpoints not connected to a resource"]["query"]
    assert "Disconnected" in q


def test_new_categories_present():
    expected = [
        "VMs stopped but not deallocated",
        "Deallocated VMs (disks and IPs still billing)",
        "Images not used by any VM or VMSS",
        "ExpressRoute Circuits not provisioned",
        "Bastion Hosts in VNets with no VMs",
        "Public IP Prefixes with no allocated IPs",
        "Disk Snapshots stale or source-deleted",
        "Recovery Services Vaults with no protected items",
        "Backup Items protecting deleted resources",
        "AVD Application Groups not linked to a workspace",
        "User-Assigned Managed Identities not attached",
    ]
    for cat in expected:
        assert cat in QUERIES, f"missing new category: {cat}"


def test_do_not_delete_not_in_queries():
    """The DoNotDelete filter moved to Python — no query should half-apply it."""
    for category, cfg in QUERIES.items():
        assert "DoNotDelete" not in cfg["query"], (
            f"{category} still filters DoNotDelete in KQL; the uniform "
            f"Python filter handles this now")


# ── Environment classification ────────────────────────────────────────────────
def test_classify_subscription():
    assert classify_subscription("Contoso-Dev") == "NON-PRODUCTION"
    assert classify_subscription("UAT Subscription") == "NON-PRODUCTION"
    assert classify_subscription("Production") == "PRODUCTION"
    assert classify_subscription("SharedServices") == "PRODUCTION"


def test_classify_resource_tag_precedence():
    sub_envs = {"sub1": "PRODUCTION"}
    r = {"name": "vm-01", "resourceGroup": "rg-core", "subscriptionId": "sub1",
         "tags": {"environment": "dev"}}
    assert classify_resource(r, sub_envs) == "NON-PRODUCTION"

    r = {"name": "qa-vm", "resourceGroup": "rg", "subscriptionId": "sub1",
         "tags": {"env": "Production"}}
    assert classify_resource(r, sub_envs) == "PRODUCTION"


def test_classify_resource_word_boundaries():
    sub_envs = {"sub1": "PRODUCTION"}
    # 'device' must not match 'dev'
    r = {"name": "device-hub", "resourceGroup": "rg-core",
         "subscriptionId": "sub1", "tags": {}}
    assert classify_resource(r, sub_envs) == "PRODUCTION"
    r = {"name": "qa-vm-01", "resourceGroup": "rg-core",
         "subscriptionId": "sub1", "tags": {}}
    assert classify_resource(r, sub_envs) == "NON-PRODUCTION"


def test_classify_resource_subscription_fallback():
    sub_envs = {"subX": "NON-PRODUCTION"}
    r = {"name": "vm", "resourceGroup": "rg", "subscriptionId": "subX", "tags": {}}
    assert classify_resource(r, sub_envs) == "NON-PRODUCTION"


# ── Post-query filters ────────────────────────────────────────────────────────
def test_has_do_not_delete_tag():
    assert has_do_not_delete_tag({"tags": {"DoNotDelete": "true"}})
    assert has_do_not_delete_tag({"tags": {"donotdelete": ""}})
    assert has_do_not_delete_tag({"tags": {"lifecycle": "DoNotDelete"}})
    assert not has_do_not_delete_tag({"tags": {"keep": "true"}})
    assert not has_do_not_delete_tag({"tags": None})
    assert not has_do_not_delete_tag({})


def test_filter_do_not_delete():
    results = {
        "CatA": [
            {"name": "a1", "tags": {"DoNotDelete": "1"}},
            {"name": "a2", "tags": {}},
        ],
        "CatB": [{"name": "b1", "tags": {"x": "DoNotDelete"}}],
    }
    filtered, dropped = filter_do_not_delete(results)
    assert dropped == 2
    assert [r["name"] for r in filtered["CatA"]] == ["a2"]
    assert filtered["CatB"] == []


def test_apply_subscription_exclusions():
    results = {
        "CatA": [
            {"name": "a1", "subscriptionId": "keep"},
            {"name": "a2", "subscriptionId": "drop"},
        ],
        "CatB": [{"name": "b1", "subscriptionId": "drop"}],
    }
    filtered, dropped = apply_subscription_exclusions(results, {"drop"})
    assert dropped == 2
    assert [r["name"] for r in filtered["CatA"]] == ["a1"]
    assert filtered["CatB"] == []

    same, none_dropped = apply_subscription_exclusions(results, set())
    assert same is results and none_dropped == 0


# ── Flatten + cost resolution ─────────────────────────────────────────────────
def _make_cost_result(mapping):
    result = ace.EnrichmentResult()
    for rid, (rolling, last) in mapping.items():
        result.cost_map[rid.lower()] = ace.CostRecord(
            rolling30d=rolling, last_billing_month=last)
    return result


SUBS = {"sub1": "Prod-Sub"}
ENVS = {"sub1": "PRODUCTION"}


def test_flatten_row_estimate_fallback():
    r = {"id": "/subscriptions/sub1/x", "name": "gw", "resourceGroup": "rg",
         "location": "eastus", "subscriptionId": "sub1", "tags": {}}
    cfg = QUERIES["VNet Gateways with no connections"]
    row = _flatten_row(r, "VNet Gateways with no connections", cfg, SUBS, ENVS)
    assert row["estimatedMonthlyCost"] == COST_ESTIMATES[
        "VNet Gateways with no connections"]
    assert row["costSource"] == "estimate"
    assert row["incursCost"] is True


def test_flatten_row_real_cost_wins():
    rid = "/subscriptions/sub1/providers/microsoft.network/virtualnetworkgateways/gw"
    cost = _make_cost_result({rid: (42.5, 50.0)})
    r = {"id": rid, "name": "gw", "resourceGroup": "rg", "location": "eastus",
         "subscriptionId": "sub1", "tags": {}}
    cfg = QUERIES["VNet Gateways with no connections"]
    row = _flatten_row(r, "VNet Gateways with no connections", cfg, SUBS, ENVS, cost)
    assert row["estimatedMonthlyCost"] == 42.5
    assert row["rolling30dCost"] == 42.5
    assert row["lastBillingMonthCost"] == 50.0
    assert row["costSource"] == "costManagement"


def test_flatten_row_zero_cm_falls_back_to_estimate():
    rid = "/subscriptions/sub1/x"
    cost = _make_cost_result({rid: (0.0, 0.0)})
    r = {"id": rid, "name": "gw", "resourceGroup": "rg", "location": "eastus",
         "subscriptionId": "sub1", "tags": {}}
    cfg = QUERIES["VNet Gateways with no connections"]
    row = _flatten_row(r, "VNet Gateways with no connections", cfg, SUBS, ENVS, cost)
    assert row["estimatedMonthlyCost"] == COST_ESTIMATES[
        "VNet Gateways with no connections"]
    assert row["costSource"] == "estimate-zero-cm"


# ── Exports ───────────────────────────────────────────────────────────────────
def _sample_rows():
    cfg = QUERIES["Unattached Managed Disks"]
    rows = []
    for i, sub in enumerate(["sub1", "sub1"]):
        r = {"id": f"/subscriptions/{sub}/disk{i}", "name": f"disk{i}",
             "resourceGroup": "rg", "location": "eastus",
             "subscriptionId": sub, "sku": "Premium_LRS", "tags": {"a": "b"}}
        rows.append(_flatten_row(r, "Unattached Managed Disks", cfg, SUBS, ENVS))
    return rows


def test_export_json_roundtrip(tmp_path):
    rows = _sample_rows()
    path = str(tmp_path / "out.json")
    export_json(rows, path)
    data = json.loads(io.open(path, encoding="utf-8").read())
    assert data["totalResources"] == 2
    assert data["estimatedMonthlyCost"] == pytest.approx(
        2 * COST_ESTIMATES["Unattached Managed Disks"])
    assert data["resources"][0]["category"] == "Unattached Managed Disks"


def test_export_csv_roundtrip(tmp_path):
    rows = _sample_rows()
    path = str(tmp_path / "out.csv")
    export_csv(rows, path)
    with io.open(path, encoding="utf-8", newline="") as f:
        parsed = list(csv.DictReader(f))
    assert len(parsed) == 2
    assert parsed[0]["category"] == "Unattached Managed Disks"
    assert json.loads(parsed[0]["tags"]) == {"a": "b"}


def test_export_html_smoke(tmp_path):
    rows = _sample_rows()
    path = str(tmp_path / "out.html")
    export_html(rows, path, "test scope")
    html = io.open(path, encoding="utf-8").read()
    assert "disk0" in html and "Unattached Managed Disks" in html


# ── Empty resource groups ─────────────────────────────────────────────────────
def test_find_empty_rgs_set_difference(monkeypatch):
    def fake_run_query(client, query, **kwargs):
        if "ResourceContainers" in query:
            return [
                {"name": "RG-Occupied", "resourceGroup": "RG-Occupied",
                 "location": "eastus", "subscriptionId": "SUB1", "tags": {}},
                {"name": "rg-empty", "resourceGroup": "rg-empty",
                 "location": "eastus", "subscriptionId": "SUB1", "tags": {}},
            ]
        return [{"resourceGroup": "rg-occupied", "subscriptionId": "sub1"}]

    monkeypatch.setattr(orphan_report, "run_query", fake_run_query)
    empty = find_empty_rgs(mock.MagicMock())
    assert [r["name"] for r in empty] == ["rg-empty"]


# ── Cost enrichment module ────────────────────────────────────────────────────
def test_rolling_window_dates():
    fixed = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)
    start, end = ace._rolling_30d_window(fixed)
    assert start == "2026-05-12"
    assert end == "2026-06-10"


def test_build_query_body_shapes():
    body = ace._build_query_body("TheLastBillingMonth")
    assert body["type"] == "AmortizedCost"
    assert body["dataset"]["grouping"][0]["name"] == "ResourceId"
    assert "timePeriod" not in body

    body = ace._build_query_body("Custom", time_period=("2026-05-01", "2026-05-30"))
    assert body["timePeriod"]["from"].startswith("2026-05-01")

    with pytest.raises(ValueError):
        ace._build_query_body("Custom")


def test_parse_rows_column_order_independent():
    payload = {"properties": {
        "columns": [{"name": "Currency"}, {"name": "Cost"}, {"name": "ResourceId"}],
        "rows": [
            ["USD", 12.5, "/subscriptions/S/r1"],
            ["USD", None, "/subscriptions/S/r2"],
            ["USD", 3.0, None],          # empty rid skipped
            ["USD", "bad"],              # short row skipped
        ],
    }}
    rows = ace._parse_rows(payload)
    assert rows[0] == ("/subscriptions/s/r1", 12.5, "USD")
    assert rows[1][1] == 0.0
    assert len(rows) == 2


class _FakeResp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def test_post_query_handles_429_then_200(monkeypatch):
    responses = [
        _FakeResp(429, headers={
            "x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after": "1"}),
        _FakeResp(200, {"ok": True}),
    ]
    monkeypatch.setattr(ace.requests, "post", lambda *a, **k: responses.pop(0))
    monkeypatch.setattr(ace.time, "sleep", lambda s: None)
    out = ace._post_query("tok", "sub", {})
    assert out == {"ok": True}


def test_post_query_403_returns_none(monkeypatch):
    monkeypatch.setattr(ace.requests, "post", lambda *a, **k: _FakeResp(403))
    assert ace._post_query("tok", "sub", {}) is None


def test_post_query_5xx_exhausts_retries(monkeypatch):
    calls = []
    monkeypatch.setattr(ace.requests, "post",
                        lambda *a, **k: calls.append(1) or _FakeResp(503))
    monkeypatch.setattr(ace.time, "sleep", lambda s: None)
    assert ace._post_query("tok", "sub", {}) is None
    assert len(calls) == ace.MAX_RETRIES


def test_cache_scope_validation(tmp_path):
    cache_dir = str(tmp_path)
    result = ace.EnrichmentResult(
        generated_at="2026-06-11T00:00:00+00:00",
        rolling30d_window=("2026-05-12", "2026-06-10"),
        subscriptions_queried=["a"],
        subscriptions_failed={"b": "403"},
    )
    result.cost_map["/subscriptions/a/r1"] = ace.CostRecord(rolling30d=1.0)
    ace._save_cache(result, cache_dir)

    # Covered: queried ∪ failed
    assert ace._load_cache(cache_dir, requested_sub_ids=["a"]) is not None
    assert ace._load_cache(cache_dir, requested_sub_ids=["a", "b"]) is not None
    # Not covered → force refetch
    assert ace._load_cache(cache_dir, requested_sub_ids=["a", "c"]) is None


def test_enrich_costs_merges_and_caches(tmp_path, monkeypatch):
    payload = {"properties": {
        "columns": [{"name": "Cost"}, {"name": "ResourceId"}, {"name": "Currency"}],
        "rows": [[10.0, "/subscriptions/s1/r1", "USD"],
                 [5.0, "/subscriptions/s1/r2", "USD"]],
    }}
    monkeypatch.setattr(ace, "_post_query", lambda tok, sub, body: payload)
    monkeypatch.setattr(
        ace, "_TokenProvider",
        lambda cred: mock.MagicMock(get_token=lambda: "tok"))

    out = ace.enrich_costs(mock.MagicMock(), ["s1"], cache_dir=str(tmp_path),
                           use_cache=False)
    assert out.subscriptions_queried == ["s1"]
    # Both timeframes return the same payload, so each rid gets rolling AND
    # last-month populated.
    rec = out.get_cost("/subscriptions/s1/r1")
    assert rec.rolling30d == 10.0 and rec.last_billing_month == 10.0
    assert out.total_rolling30d == 15.0
    assert os.path.exists(ace._cache_path(str(tmp_path)))


def test_enrich_costs_all_failures_skips_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ace, "_post_query", lambda tok, sub, body: None)
    monkeypatch.setattr(
        ace, "_TokenProvider",
        lambda cred: mock.MagicMock(get_token=lambda: "tok"))
    out = ace.enrich_costs(mock.MagicMock(), ["s1"], cache_dir=str(tmp_path),
                           use_cache=False)
    assert out.subscriptions_failed == {"s1": "rolling30d query failed"}
    assert not os.path.exists(ace._cache_path(str(tmp_path)))


# ── End-to-end main() with canned Resource Graph data ─────────────────────────
def _dispatching_run_query(canned):
    """Return a fake run_query that dispatches canned rows by matching a
    marker substring in the query text."""
    def fake(client, query, **kwargs):
        for marker, rows in canned.items():
            if marker in query:
                return rows
        return []
    return fake


def test_main_end_to_end_json(tmp_path, monkeypatch, capsys):
    out_path = str(tmp_path / "report.json")
    gw = {"id": "/subscriptions/keep/gw1", "name": "gw1", "resourceGroup": "rg",
          "location": "eastus", "subscriptionId": "keep",
          "sku": "VpnGw1", "tags": {}}
    excluded_disk = {"id": "/subscriptions/dropme/d1", "name": "d1",
                     "resourceGroup": "rg", "location": "eastus",
                     "subscriptionId": "dropme", "sku": "Premium_LRS", "tags": {}}
    dnd_ip = {"id": "/subscriptions/keep/ip1", "name": "ip1",
              "resourceGroup": "rg", "location": "eastus",
              "subscriptionId": "keep", "sku": "Standard",
              "tags": {"DoNotDelete": "yes"}}

    canned = {
        "virtualnetworkgateways": [gw],
        "diskState =~ 'Unattached'": [excluded_disk],
        "publicipaddresses": [dnd_ip],
    }
    monkeypatch.setattr(orphan_report, "run_query",
                        _dispatching_run_query(canned))
    monkeypatch.setattr(orphan_report, "DefaultAzureCredential", mock.MagicMock())
    monkeypatch.setattr(orphan_report, "ResourceGraphClient", mock.MagicMock())
    monkeypatch.setattr(orphan_report, "SubscriptionClient", mock.MagicMock())
    monkeypatch.setattr(sys, "argv", [
        "orphan_report.py", "--subscription", "keep",
        "--exclude-subscriptions", "dropme",
        "--format", "json", "--output", out_path, "--no-cost-data",
    ])

    orphan_report.main()

    data = json.loads(io.open(out_path, encoding="utf-8").read())
    names = {r["name"] for r in data["resources"]}
    assert "gw1" in names                # the orphan we expect
    assert "d1" not in names             # excluded subscription dropped
    assert "ip1" not in names            # DoNotDelete honored
    assert data["totalResources"] == 1
    assert data["resources"][0]["estimatedMonthlyCost"] == COST_ESTIMATES[
        "VNet Gateways with no connections"]


# ── Cross-script consistency ──────────────────────────────────────────────────
def test_cleanup_uses_shared_queries():
    import orphan_cleanup
    assert orphan_cleanup.QUERIES is QUERIES
    deletable = {key for _, key, _ in orphan_cleanup.DELETION_ORDER}
    assert deletable <= set(QUERIES)
    # New categories must be report-only — never auto-deleted.
    report_only = set(QUERIES) - deletable
    for must_be_safe in [
        "VMs stopped but not deallocated",
        "Deallocated VMs (disks and IPs still billing)",
        "Backup Items protecting deleted resources",
        "Bastion Hosts in VNets with no VMs",
        "Recovery Services Vaults with no protected items",
        "User-Assigned Managed Identities not attached",
    ]:
        assert must_be_safe in report_only, (
            f"{must_be_safe} must never be in DELETION_ORDER")


def test_excel_uses_shared_queries():
    import generate_excel_report
    assert generate_excel_report.QUERIES is QUERIES
    assert generate_excel_report.COST_ESTIMATES is COST_ESTIMATES


# ── Targeted cleanup by resource ID (orphan_cleanup.py) ───────────────────────
import orphan_cleanup as oc  # noqa: E402


def test_parse_resource_id_proper_case():
    rid = "/subscriptions/SUB/resourceGroups/RG/providers/Microsoft.Network/privateEndpoints/pe1"
    r = oc.parse_resource_id(rid)
    assert r["subscriptionId"] == "SUB"
    assert r["resourceGroup"] == "RG"
    assert r["name"] == "pe1"
    assert r["type"] == "microsoft.network/privateendpoints"


def test_parse_resource_id_lowercased():
    # Resource Graph returns id=tolower(id); the parser must still work.
    rid = "/subscriptions/sub/resourcegroups/rg/providers/microsoft.network/publicipaddresses/ip1"
    r = oc.parse_resource_id(rid)
    assert r["type"] == "microsoft.network/publicipaddresses"
    assert r["name"] == "ip1"


@pytest.mark.parametrize("bad", ["not-an-id", "/foo/bar", "/subscriptions/x", ""])
def test_parse_resource_id_malformed(bad):
    with pytest.raises(ValueError):
        oc.parse_resource_id(bad)


def test_read_ids_file(tmp_path):
    p = tmp_path / "ids.txt"
    p.write_text(
        "# header comment\n"
        "\n"
        "/subscriptions/a/resourceGroups/b/providers/x/y/z   # inline comment\n"
        "   \n"
        "/subscriptions/c/resourceGroups/d/providers/x/y/w\n",
        encoding="utf-8",
    )
    assert oc.read_ids_file(str(p)) == [
        "/subscriptions/a/resourceGroups/b/providers/x/y/z",
        "/subscriptions/c/resourceGroups/d/providers/x/y/w",
    ]


def test_delete_targeted_dispatch(monkeypatch):
    calls = {}
    monkeypatch.setattr(oc, "_ARM_TYPE_DELETERS", {
        "microsoft.network/privateendpoints": lambda cred, r: calls.setdefault("net", r),
    })
    monkeypatch.setattr(oc, "delete_by_resource_id",
                        lambda cred, rid: calls.setdefault("generic", rid))

    pe = oc.parse_resource_id(
        "/subscriptions/s/resourceGroups/g/providers/Microsoft.Network/privateEndpoints/pe")
    oc.delete_targeted(None, pe)
    assert calls.get("net") is pe and "generic" not in calls

    other = oc.parse_resource_id(
        "/subscriptions/s/resourceGroups/g/providers/Microsoft.Foo/bars/b")
    oc.delete_targeted(None, other)
    assert calls.get("generic") == other["id"]


def test_targeted_cleanup_refuses_out_of_scope(monkeypatch, tmp_path):
    # An ID whose subscription is NOT in the tenant's sub set must be refused,
    # never deleted, even on --confirm.
    p = tmp_path / "ids.txt"
    p.write_text(
        "/subscriptions/IN/resourceGroups/g/providers/Microsoft.Network/publicIPAddresses/keep\n"
        "/subscriptions/OUT/resourceGroups/g/providers/Microsoft.Network/publicIPAddresses/foreign\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(oc, "run_query",
                        lambda *a, **k: [{"id": "/subscriptions/in/resourcegroups/g/providers/microsoft.network/publicipaddresses/keep"}])
    deleted = []
    monkeypatch.setattr(oc, "delete_targeted", lambda cred, r: deleted.append(r["id"]))

    rc = oc.run_targeted_cleanup(
        credential=None, graph_client=None, ids_file=str(p),
        sub_names={"IN": "in-sub"}, sub_envs={"IN": "PRODUCTION"},
        dry_run=False, query_kwargs={},
    )
    # foreign sub never deleted; only the in-scope, still-existing one acted on
    assert all("OUT" not in d for d in deleted)
    assert deleted == ["/subscriptions/IN/resourceGroups/g/providers/Microsoft.Network/publicIPAddresses/keep"]
    assert rc == 0


# ── Cloud Shell bundle integrity ──────────────────────────────────────────────
def test_cloudshell_copies_match_root():
    """The cloudshell/ drop-in bundle must match the root scripts so a Cloud
    Shell run can never diverge from an azcli run. Compares content (line
    endings normalized — eol is enforced separately by .gitattributes)."""
    shared = [
        "orphan_report.py", "generate_excel_report.py", "azure_cost_enrichment.py",
        "orphan_cleanup.py", "generate_pptx_slide.py", "vm_backup_gap_analysis.py",
        "requirements.txt",
    ]
    cs_dir = os.path.join(REPO_ROOT, "cloudshell")
    for name in shared:
        cs_path = os.path.join(cs_dir, name)
        assert os.path.exists(cs_path), f"cloudshell/{name} is missing from the bundle"
        with open(os.path.join(REPO_ROOT, name), encoding="utf-8") as a, \
                open(cs_path, encoding="utf-8") as b:
            assert a.read().splitlines() == b.read().splitlines(), \
                f"cloudshell/{name} has drifted from root {name} — re-sync the bundle"
