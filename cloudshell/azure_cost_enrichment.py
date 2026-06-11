#!/usr/bin/env python3
"""
azure_cost_enrichment.py

Enriches a list of Azure resource IDs with real cost data pulled from the
Azure Cost Management Query API.

Best-practice notes (per Microsoft Learn):
  - Metric: AmortizedCost (reallocates reservation/savings-plan costs to the
    resource that actually consumed them — correct for per-resource chargeback)
  - Timeframe A: Custom rolling 30 days, ending yesterday (avoids incomplete
    same-day data which lags 8-24 hours)
  - Timeframe B: TheLastBillingMonth (defensible, invoice-tied number for
    finance reconciliation)
  - Scope: one query per subscription, grouped by ResourceId
  - Cadence: call at most once per day; data refreshes every 4 hours
  - Throttling: QPU quotas 12/10s, 60/min, 600/hr; honor the
    `x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after` header on 429
  - Cache: results cached to cost-cache-YYYYMMDD.json so same-day reruns do
    not re-hit the API

RBAC required:
  - `Cost Management Reader` at subscription or management group scope
  - DefaultAzureCredential must resolve to an identity holding that role

This module has NO dependency on the orphan scanner — it just takes a list
of subscription IDs, runs the queries, and returns a lookup map:

    {
      "/subscriptions/xxx/resourcegroups/yyy/providers/.../name-lowercase": {
        "rolling30d": 47.22,
        "lastBillingMonth": 52.18,
        "currency": "USD",
      },
      ...
    }

Missing resources (not in the map, or zero-cost) simply return None from
`get_cost()`; callers should fall back to an estimate or zero.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from azure.identity import DefaultAzureCredential

logger = logging.getLogger("cost-enrichment")

# ── Constants ─────────────────────────────────────────────────────────────────
ARM_ENDPOINT = "https://management.azure.com"
ARM_SCOPE = "https://management.azure.com/.default"
API_VERSION = "2023-11-01"

# Retry/backoff tuning. Cost Management throttling is QPU-based; 429s include
# a `x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after` header we honor.
MAX_RETRIES = 5
DEFAULT_BACKOFF_SECONDS = 8
REQUEST_TIMEOUT = 60

# Token cached locally, refreshed ~5 minutes before expiry.
_TOKEN_REFRESH_BUFFER = 300


# ── Data classes ──────────────────────────────────────────────────────────────
@dataclass
class CostRecord:
    """Cost totals for a single resource ID."""
    rolling30d: float = 0.0
    last_billing_month: float = 0.0
    currency: str = "USD"

    def to_dict(self) -> dict[str, Any]:
        return {
            "rolling30d": round(self.rolling30d, 2),
            "lastBillingMonth": round(self.last_billing_month, 2),
            "currency": self.currency,
        }


@dataclass
class EnrichmentResult:
    """Full enrichment output for downstream consumers."""
    cost_map: dict[str, CostRecord] = field(default_factory=dict)
    subscriptions_queried: list[str] = field(default_factory=list)
    subscriptions_failed: dict[str, str] = field(default_factory=dict)
    rolling30d_window: tuple[str, str] = ("", "")
    generated_at: str = ""
    total_rolling30d: float = 0.0
    total_last_billing_month: float = 0.0

    def get_cost(self, resource_id: str | None) -> CostRecord | None:
        """Look up cost for a resource ID (case-insensitive). Returns None if
        the resource has no billing records in the window."""
        if not resource_id:
            return None
        return self.cost_map.get(resource_id.lower())

    def to_dict(self) -> dict[str, Any]:
        return {
            "generatedAt": self.generated_at,
            "rolling30dWindow": {
                "from": self.rolling30d_window[0],
                "to": self.rolling30d_window[1],
            },
            "totals": {
                "rolling30d": round(self.total_rolling30d, 2),
                "lastBillingMonth": round(self.total_last_billing_month, 2),
            },
            "subscriptionsQueried": self.subscriptions_queried,
            "subscriptionsFailed": self.subscriptions_failed,
            "costs": {k: v.to_dict() for k, v in self.cost_map.items()},
        }


# ── Token helper ──────────────────────────────────────────────────────────────
class _TokenProvider:
    """Small wrapper around DefaultAzureCredential that caches access tokens
    until they're near expiry. Keeps token acquisition off the hot path."""

    def __init__(self, credential: DefaultAzureCredential):
        self._credential = credential
        self._token: str | None = None
        self._expires_on: int = 0

    def get_token(self) -> str:
        now = int(time.time())
        if self._token is None or now >= (self._expires_on - _TOKEN_REFRESH_BUFFER):
            access = self._credential.get_token(ARM_SCOPE)
            self._token = access.token
            self._expires_on = access.expires_on
        return self._token  # type: ignore[return-value]


# ── Timeframe helpers ─────────────────────────────────────────────────────────
def _rolling_30d_window(now_utc: datetime | None = None) -> tuple[str, str]:
    """Return ISO8601 (date only) start/end for a 30-day window that ends
    yesterday — avoids incomplete same-day data. 30 complete days total."""
    now = now_utc or datetime.now(timezone.utc)
    end = (now - timedelta(days=1)).date()
    start = (now - timedelta(days=30)).date()
    return start.isoformat(), end.isoformat()


# ── Request body builders ────────────────────────────────────────────────────
def _build_query_body(
    timeframe: str,
    *,
    time_period: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Build a Cost Management Query API request body grouping AmortizedCost
    by ResourceId. `timeframe` is one of the Cost Management timeframe names
    (Custom, TheLastBillingMonth, etc.); if Custom, `time_period` is required
    as (fromDate, toDate) ISO strings."""
    body: dict[str, Any] = {
        "type": "AmortizedCost",
        "timeframe": timeframe,
        "dataset": {
            "granularity": "None",
            "aggregation": {
                "totalCost": {"name": "Cost", "function": "Sum"},
            },
            "grouping": [
                {"type": "Dimension", "name": "ResourceId"},
            ],
        },
    }
    if timeframe == "Custom":
        if not time_period:
            raise ValueError("Custom timeframe requires a time_period tuple")
        start, end = time_period
        body["timePeriod"] = {
            "from": f"{start}T00:00:00+00:00",
            "to": f"{end}T23:59:59+00:00",
        }
    return body


# ── HTTP helper with QPU-aware retry ─────────────────────────────────────────
def _post_query(
    token: str,
    subscription_id: str,
    body: dict[str, Any],
) -> dict[str, Any] | None:
    """POST one Cost Management query. Honors 429 retry-after, retries 5xx
    with exponential backoff, returns the JSON response or None on hard
    failure (403, 404, or retries exhausted)."""
    url = (
        f"{ARM_ENDPOINT}/subscriptions/{subscription_id}"
        f"/providers/Microsoft.CostManagement/query?api-version={API_VERSION}"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    backoff = DEFAULT_BACKOFF_SECONDS
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning(
                "sub=%s attempt=%d network error: %s", subscription_id, attempt, exc
            )
            if attempt == MAX_RETRIES:
                return None
            time.sleep(backoff)
            backoff *= 2
            continue

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code == 429:
            # Honor the Cost Management specific header first, then generic Retry-After.
            wait = (
                resp.headers.get("x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after")
                or resp.headers.get("Retry-After")
                or str(backoff)
            )
            try:
                wait_seconds = int(float(wait))
            except ValueError:
                wait_seconds = backoff
            logger.warning(
                "sub=%s 429 throttled — waiting %ds (attempt %d/%d)",
                subscription_id, wait_seconds, attempt, MAX_RETRIES,
            )
            time.sleep(max(wait_seconds, 1))
            continue

        if resp.status_code in (401, 403):
            # Auth/authz problems won't fix themselves on retry.
            logger.warning(
                "sub=%s %d permission denied (need Cost Management Reader): %s",
                subscription_id, resp.status_code, resp.text[:300],
            )
            return None

        if resp.status_code == 404:
            logger.warning("sub=%s 404 not found (disabled subscription?)", subscription_id)
            return None

        if 500 <= resp.status_code < 600:
            logger.warning(
                "sub=%s %d server error (attempt %d/%d): %s",
                subscription_id, resp.status_code, attempt, MAX_RETRIES, resp.text[:200],
            )
            if attempt == MAX_RETRIES:
                return None
            time.sleep(backoff)
            backoff *= 2
            continue

        # Any other status — log and give up, unlikely to self-heal.
        logger.error(
            "sub=%s %d unexpected response: %s",
            subscription_id, resp.status_code, resp.text[:300],
        )
        return None

    return None


# ── Result parsing ────────────────────────────────────────────────────────────
def _parse_rows(payload: dict[str, Any]) -> list[tuple[str, float, str]]:
    """Extract (resource_id_lower, cost, currency) tuples from a Query API
    response. Handles the column-index lookup so we don't depend on ordering."""
    props = payload.get("properties") or {}
    columns = props.get("columns") or []
    rows = props.get("rows") or []
    if not columns or not rows:
        return []

    # Build a name->index map. Column names come back as "Cost", "ResourceId",
    # "Currency" — but we look them up by lowercase to be defensive.
    idx = {c.get("name", "").lower(): i for i, c in enumerate(columns)}
    cost_i = idx.get("cost")
    rid_i = idx.get("resourceid")
    cur_i = idx.get("currency")

    if cost_i is None or rid_i is None:
        logger.warning("Query response missing Cost or ResourceId column: %s", columns)
        return []

    out: list[tuple[str, float, str]] = []
    for row in rows:
        try:
            rid = str(row[rid_i]).lower() if row[rid_i] else ""
            cost = float(row[cost_i]) if row[cost_i] is not None else 0.0
            currency = str(row[cur_i]) if cur_i is not None and row[cur_i] else "USD"
        except (IndexError, ValueError, TypeError):
            continue
        if rid:
            out.append((rid, cost, currency))
    return out


def _follow_next_link(token: str, next_link: str) -> dict[str, Any] | None:
    """Follow a paginated nextLink URL. Shares the same retry behavior as
    the initial POST but uses GET (per the API contract)."""
    headers = {"Authorization": f"Bearer {token}"}
    backoff = DEFAULT_BACKOFF_SECONDS
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(next_link, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            if attempt == MAX_RETRIES:
                return None
            time.sleep(backoff)
            backoff *= 2
            continue
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            wait = resp.headers.get(
                "x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after"
            ) or resp.headers.get("Retry-After") or str(backoff)
            try:
                wait_seconds = int(float(wait))
            except ValueError:
                wait_seconds = backoff
            time.sleep(max(wait_seconds, 1))
            continue
        return None
    return None


# ── Cache ─────────────────────────────────────────────────────────────────────
def _cache_path(cache_dir: str) -> str:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    return os.path.join(cache_dir, f"cost-cache-{today}.json")


def _load_cache(
    cache_dir: str,
    requested_sub_ids: list[str] | None = None,
) -> EnrichmentResult | None:
    """Load today's cache if it exists AND covers every requested
    subscription. A cache written by a narrower run (e.g. a single-sub
    scan earlier today) must not satisfy a tenant-wide run — that would
    silently report estimates for every other subscription."""
    path = _cache_path(cache_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load cost cache %s: %s", path, exc)
        return None

    if requested_sub_ids:
        covered = set(raw.get("subscriptionsQueried") or []) | set(
            (raw.get("subscriptionsFailed") or {}).keys())
        missing = [s for s in requested_sub_ids if s not in covered]
        if missing:
            logger.info(
                "Cost cache %s doesn't cover %d requested subscription(s) — "
                "refetching", path, len(missing),
            )
            return None

    result = EnrichmentResult(
        generated_at=raw.get("generatedAt", ""),
        rolling30d_window=(
            (raw.get("rolling30dWindow") or {}).get("from", ""),
            (raw.get("rolling30dWindow") or {}).get("to", ""),
        ),
        subscriptions_queried=raw.get("subscriptionsQueried", []),
        subscriptions_failed=raw.get("subscriptionsFailed", {}),
        total_rolling30d=(raw.get("totals") or {}).get("rolling30d", 0.0),
        total_last_billing_month=(raw.get("totals") or {}).get("lastBillingMonth", 0.0),
    )
    for rid, data in (raw.get("costs") or {}).items():
        result.cost_map[rid] = CostRecord(
            rolling30d=data.get("rolling30d", 0.0),
            last_billing_month=data.get("lastBillingMonth", 0.0),
            currency=data.get("currency", "USD"),
        )
    logger.info("Loaded cost cache from %s (%d resources)", path, len(result.cost_map))
    return result


def _save_cache(result: EnrichmentResult, cache_dir: str) -> None:
    path = _cache_path(cache_dir)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, indent=2)
        logger.info("Saved cost cache to %s", path)
    except OSError as exc:
        logger.warning("Failed to save cost cache %s: %s", path, exc)


# ── Public entry point ───────────────────────────────────────────────────────
def enrich_costs(
    credential: DefaultAzureCredential,
    subscription_ids: list[str],
    *,
    cache_dir: str = ".",
    use_cache: bool = True,
) -> EnrichmentResult:
    """Query Cost Management for amortized cost data across the given
    subscriptions, returning a resource-ID → CostRecord map.

    Args:
      credential: DefaultAzureCredential — needs Cost Management Reader RBAC
        at subscription or management group scope.
      subscription_ids: List of subscription IDs to query. Deduped internally.
      cache_dir: Directory to read/write daily cache files. Default: cwd.
      use_cache: If True (default), reuse today's cache file if it exists.
        Set False to force a fresh pull.

    Returns:
      EnrichmentResult with cost_map, totals, and per-subscription status.
    """
    # Dedupe and drop empties — the scanner can pass the same sub twice if
    # multiple categories matched it.
    sub_ids = sorted({s for s in subscription_ids if s})
    if not sub_ids:
        logger.warning("No subscription IDs provided — returning empty result")
        return EnrichmentResult(generated_at=datetime.now(timezone.utc).isoformat())

    if use_cache:
        cached = _load_cache(cache_dir, requested_sub_ids=sub_ids)
        if cached is not None:
            return cached

    start, end = _rolling_30d_window()
    result = EnrichmentResult(
        rolling30d_window=(start, end),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )

    token_provider = _TokenProvider(credential)

    rolling_body = _build_query_body("Custom", time_period=(start, end))
    last_month_body = _build_query_body("TheLastBillingMonth")

    for sub_id in sub_ids:
        logger.info("Querying Cost Management for subscription %s", sub_id)
        token = token_provider.get_token()

        rolling_payload = _post_query(token, sub_id, rolling_body)
        if rolling_payload is None:
            result.subscriptions_failed[sub_id] = "rolling30d query failed"
            continue

        # Follow pagination for rolling window.
        rolling_rows: list[tuple[str, float, str]] = list(_parse_rows(rolling_payload))
        next_link = (rolling_payload.get("properties") or {}).get("nextLink")
        while next_link:
            token = token_provider.get_token()
            page = _follow_next_link(token, next_link)
            if page is None:
                break
            rolling_rows.extend(_parse_rows(page))
            next_link = (page.get("properties") or {}).get("nextLink")

        # Last billing month query — refresh token in case we've been waiting.
        token = token_provider.get_token()
        last_month_payload = _post_query(token, sub_id, last_month_body)
        if last_month_payload is None:
            # Partial success: we have rolling but not last-month. Record the
            # rolling data anyway and flag the sub.
            result.subscriptions_failed[sub_id] = "lastBillingMonth query failed"
            last_month_rows: list[tuple[str, float, str]] = []
        else:
            last_month_rows = list(_parse_rows(last_month_payload))
            next_link = (last_month_payload.get("properties") or {}).get("nextLink")
            while next_link:
                token = token_provider.get_token()
                page = _follow_next_link(token, next_link)
                if page is None:
                    break
                last_month_rows.extend(_parse_rows(page))
                next_link = (page.get("properties") or {}).get("nextLink")

        result.subscriptions_queried.append(sub_id)

        # Merge into cost_map.
        for rid, cost, currency in rolling_rows:
            rec = result.cost_map.setdefault(rid, CostRecord(currency=currency))
            rec.rolling30d += cost
            rec.currency = currency
            result.total_rolling30d += cost
        for rid, cost, currency in last_month_rows:
            rec = result.cost_map.setdefault(rid, CostRecord(currency=currency))
            rec.last_billing_month += cost
            rec.currency = currency
            result.total_last_billing_month += cost

    logger.info(
        "Cost enrichment complete: %d subs queried, %d failed, %d resource records",
        len(result.subscriptions_queried),
        len(result.subscriptions_failed),
        len(result.cost_map),
    )

    # Only cache when at least one subscription succeeded — caching an
    # all-failures result would mask a transient auth/network issue on
    # same-day reruns.
    if result.subscriptions_queried:
        _save_cache(result, cache_dir)
    else:
        logger.warning("All subscriptions failed — skipping cache write")

    return result


# ── Standalone smoke test ────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description="Smoke-test the Cost Management Query API enrichment module."
    )
    ap.add_argument("--subscription", "-s", required=True, action="append",
                    help="Subscription ID to query (repeatable).")
    ap.add_argument("--no-cache", action="store_true",
                    help="Skip the daily cache and force a fresh pull.")
    ap.add_argument("--output", "-o", default=None,
                    help="Optional path to dump the full result JSON.")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    cred = DefaultAzureCredential()
    out = enrich_costs(cred, args.subscription, use_cache=not args.no_cache)

    print(f"Window: {out.rolling30d_window[0]} → {out.rolling30d_window[1]}")
    print(f"Subscriptions queried: {len(out.subscriptions_queried)}")
    print(f"Subscriptions failed:  {len(out.subscriptions_failed)}")
    for sid, err in out.subscriptions_failed.items():
        print(f"  {sid}: {err}")
    print(f"Resources with cost:   {len(out.cost_map)}")
    print(f"Total rolling 30d:     ${out.total_rolling30d:,.2f}")
    print(f"Total last month:      ${out.total_last_billing_month:,.2f}")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(out.to_dict(), f, indent=2)
        print(f"Dumped full result to {args.output}")
