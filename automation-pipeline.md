# Azure Orphan Reports — Automated Visual Reporting Pipeline

## Overview

Build an automated pipeline that runs orphaned resource scans on a schedule, generates visual reports (charts, tables, trend lines), and delivers them to stakeholders — all without Power BI. The goal is a zero-touch monthly/weekly reporting workflow that produces polished, client-ready artifacts.

## Architecture

### Stack

- **Scheduler**: Azure Function (Timer Trigger) or Azure Automation Runbook
- **Scan Engine**: Existing `orphan_report.py` (Resource Graph queries)
- **Visualization**: `matplotlib` + `plotly` for charts, `weasyprint` or `reportlab` for PDF
- **Storage**: Azure Blob Storage (report archive)
- **Delivery**: Azure Logic App or SendGrid for email delivery
- **Optional**: Azure Data Explorer (Kusto) for historical querying

### Pipeline Flow

```
Timer Trigger (1st of month)
  → Azure Function runs orphan_report.py --format json
  → Store JSON in Blob Storage (timestamped)
  → Generate visual PDF report from JSON
  → Generate PowerPoint CIR slide from JSON
  → Generate HTML dashboard from JSON
  → Email reports to configured recipients
  → Archive all artifacts in Blob Storage
```

## Visual Report Components

### 1. PDF Monthly Report (No Power BI)

Generated with `matplotlib` + `reportlab`:

- **Page 1 — Executive Summary**
  - KPI cards: Total Orphans, Est. Monthly Waste, Subscriptions Scanned
  - Donut chart: Production vs Non-Production split
  - Bar chart: Top 5 resource types by count

- **Page 2 — Trend Analysis**
  - Line chart: Orphan count over last 6-12 months
  - Line chart: Estimated monthly waste trend
  - Delta callout: "↑ 12 new orphans since last month"

- **Page 3 — Detailed Breakdown**
  - Stacked bar: Resource types by environment
  - Table: All orphaned resources (paginated)

- **Page 4 — Recommendations**
  - Auto-generated based on data: "9 unattached disks in Production — estimated $45/month waste"
  - Priority ranking by cost impact

### 2. Interactive HTML Dashboard (Enhanced)

Already built — enhance with:

- Historical comparison toggle (this month vs last month)
- Exportable charts (download as PNG)
- Printable view (CSS @media print)

### 3. PowerPoint CIR Slide

Already in scope — `generate_pptx_slide.py` (single summary slide per client).

## Historical Data Storage

### Option A: Azure Blob Storage (Simple)

```
container: orphan-reports
├── 2026-01/
│   ├── scan-2026-01-01.json
│   ├── report-2026-01.pdf
│   └── slide-2026-01.pptx
├── 2026-02/
│   └── ...
└── latest/
    ├── scan-latest.json
    └── report-latest.pdf
```

Trend analysis reads last N months of JSON files from blob.

### Option B: Azure Data Explorer / Kusto (Advanced)

- Ingest each scan's JSON into a Kusto table
- Query trends with KQL: `OrphanScans | summarize count() by bin(timestamp, 1M), resourceType`
- Connect to Kusto from Python for chart generation
- Enables ad-hoc querying and alerting

## Automation Options

### Option 1: Azure Function + Timer Trigger (Recommended)

```python
# function_app.py
import azure.functions as func

app = func.FunctionApp()

@app.timer_trigger(schedule="0 0 8 1 * *", arg_name="timer")  # 1st of month, 8am UTC
def monthly_orphan_report(timer: func.TimerRequest):
    # 1. Run scan
    # 2. Generate reports
    # 3. Upload to blob
    # 4. Trigger email via Logic App
    pass
```

Cost: ~$0/month (Consumption plan, runs once monthly)

### Option 2: Azure Automation Runbook

- Python runbook in Azure Automation Account
- Schedule via built-in scheduler
- Managed Identity for Azure access
- Good if client already uses Automation Accounts

### Option 3: GitHub Actions (Simple)

```yaml
on:
  schedule:
    - cron: '0 8 1 * *'  # 1st of month
jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: azure/login@v2
        with:
          creds: ${{ secrets.AZURE_CREDENTIALS }}
      - run: |
          pip install -r requirements.txt
          python orphan_report.py --format json --output scan.json
          python generate_pptx_slide.py --input scan.json
      - uses: actions/upload-artifact@v4
        with:
          name: monthly-report
          path: |
            scan.json
            *.pptx
            *.pdf
```

## Email Delivery

### SendGrid (Recommended)

```python
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Attachment

message = Mail(
    from_email="finops@ahead.com",
    to_emails=["client-stakeholder@example.com"],
    subject=f"Azure Orphaned Resources — Monthly Report ({month})",
    html_content=email_body,
)
# Attach PDF + PPTX
message.add_attachment(Attachment(...))
```

### Azure Logic App

- Trigger: HTTP webhook from Azure Function
- Action: Send email via Office 365 connector
- Attach blob URLs for report downloads

## Implementation Phases

### Phase 1: Report Generation (1 week)

- PDF report generator with matplotlib charts
- Read from JSON scan output
- Command-line: `python generate_pdf_report.py --input scan.json`

### Phase 2: Historical Storage (3 days)

- Blob Storage upload after each scan
- Trend data loader (read last N months of JSON)
- Trend charts in PDF report

### Phase 3: Scheduling (2 days)

- Azure Function with timer trigger
- Managed Identity configuration
- End-to-end pipeline test

### Phase 4: Email Delivery (2 days)

- SendGrid or Logic App integration
- Email template with inline summary + attached reports
- Recipient configuration per client

## Dependencies

```
# Additional requirements for this pipeline
matplotlib>=3.8.0
reportlab>=4.0.0
azure-storage-blob>=12.0.0
sendgrid>=6.0.0       # if using SendGrid
azure-functions>=1.0.0 # if using Azure Functions
```

## Existing Assets to Reuse

- All 22 Resource Graph queries from `orphan_report.py`
- Environment classification logic (`classify_resource()`)
- Cost estimation map
- JSON export format (already structured for consumption)
- HTML dashboard (enhance, don't rebuild)
- PowerPoint slide generator (from current project)
