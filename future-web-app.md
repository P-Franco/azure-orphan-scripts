# Azure Orphan Dashboard — Multi-Tenant Web Application

## Overview

Build a secure, multi-tenant web application that provides per-client Azure orphaned resource dashboards. Each AHEAD client gets their own isolated view with interactive reporting, historical trends, and cost optimization insights.

## Architecture

### Stack
- **Backend**: Python (FastAPI or Django REST Framework)
- **Frontend**: React + TailwindCSS + shadcn/ui + Recharts
- **Database**: Azure Cosmos DB (or PostgreSQL on Azure)
- **Auth**: Azure Entra ID (formerly Azure AD) with RBAC
- **Hosting**: Azure App Service or Azure Container Apps
- **Scheduling**: Azure Functions (timer trigger for daily/weekly scans)

### Multi-Tenancy Model
- Each client is a "tenant" with their own Azure credential configuration
- Data isolation via tenant ID on every record
- AHEAD staff see all tenants; client users see only their own
- Role-based access: Admin (AHEAD), Viewer (client stakeholders), Operator (client engineers)

## Core Features

### 1. Per-Client Dashboard
- Interactive charts (resource type breakdown, cost by category, trend over time)
- Filterable/sortable resource table (same as current HTML dashboard but persistent)
- Environment split view (Production vs Non-Production)
- Subscription-level drill-down

### 2. Historical Tracking
- Store scan results on every run (daily or weekly)
- Month-over-month trend lines: orphan count, estimated waste
- "New orphans this period" vs "Resolved orphans" delta view
- Aging report: how long each orphan has existed

### 3. Client Management (AHEAD Admin)
- Onboard new clients: configure Azure credentials (Service Principal or Managed Identity)
- Set scan scope per client (subscriptions, management groups, exclusions)
- Configure alerting thresholds per client
- View cross-client summary: total orphans, total waste across all clients

### 4. Notifications & Alerts
- Email digest: weekly/monthly summary per client
- Threshold alerts: "New orphan in Production" or "Estimated waste exceeds $X/month"
- Integration with Teams/Slack webhooks

### 5. Export & Reporting
- Download current scan as Excel, CSV, JSON, PDF, or PowerPoint
- Scheduled report delivery via email (weekly/monthly)
- Embed-ready iframe for client portals

## Data Model

```
Tenant
  - id, name, azure_tenant_id, credentials (encrypted), scan_config, created_at

ScanRun
  - id, tenant_id, started_at, completed_at, total_resources, total_cost, status

OrphanedResource
  - id, scan_run_id, tenant_id, category, name, resource_group, location
  - subscription_id, subscription_name, environment, estimated_monthly_cost
  - tags (JSON), first_seen_at, last_seen_at, resolved_at

User
  - id, entra_id, email, display_name, role, tenant_id (null for AHEAD admins)
```

## Security Requirements

- All Azure credentials stored encrypted at rest (Azure Key Vault)
- No client credentials in code or config files
- Entra ID authentication required for all endpoints
- Tenant isolation enforced at query level (not just UI)
- Audit log for all admin actions
- HTTPS only, CORS locked to known origins

## Implementation Phases

### Phase 1: MVP (2-3 weeks)
- FastAPI backend with scan engine (reuse existing Resource Graph queries)
- React dashboard with charts and table
- Single-tenant mode (one client at a time)
- Entra ID login
- Manual scan trigger via UI button

### Phase 2: Multi-Tenant (1-2 weeks)
- Tenant management UI
- Credential storage in Key Vault
- Data isolation
- Role-based views

### Phase 3: Automation & History (1-2 weeks)
- Azure Function timer trigger for scheduled scans
- Historical data storage and trend charts
- Delta reporting (new vs resolved)

### Phase 4: Notifications & Polish (1 week)
- Email digests
- Teams/Slack webhooks
- PDF/PowerPoint export from dashboard
- Cross-client AHEAD admin view

## Existing Assets to Reuse

- All 22 Resource Graph queries from `orphan_report.py`
- Environment classification logic (`classify_resource()` with 3-tier precedence)
- Cost estimation map
- HTML dashboard design (CSS/JS patterns for the React frontend)
- Excel report structure for export

## Prerequisites

- Azure subscription for hosting
- Entra ID app registration (for auth)
- Azure Key Vault instance (for credential storage)
- Service Principal per client with Reader role on their subscriptions
