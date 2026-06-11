#!/bin/bash
# ──────────────────────────────────────────────────────────────────────
# Azure Orphaned Resources Scanner — Cloud Shell Runner
#
# Usage (from Cloud Shell):
#   cd ~/clouddrive/cloudshell   # or wherever you uploaded this folder
#   chmod +x run.sh
#   ./run.sh                     # scan entire tenant, console output
#   ./run.sh --format json       # export JSON
#   ./run.sh --format csv        # export CSV
#   ./run.sh --format html       # interactive HTML dashboard
#   ./run.sh --format excel      # formatted Excel workbook
#   ./run.sh -s <sub-id>         # scan single subscription
#   ./run.sh --no-cost-data      # skip Cost Management (no RBAC needed)
#
# All flags from orphan_report.py / generate_excel_report.py pass through.
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# ── Color helpers ────────────────────────────────────────────────────
BOLD="\033[1m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
RESET="\033[0m"

info()  { echo -e "${BOLD}${GREEN}[+]${RESET} $*"; }
warn()  { echo -e "${BOLD}${YELLOW}[!]${RESET} $*"; }
fail()  { echo -e "${BOLD}${RED}[x]${RESET} $*"; exit 1; }

# ── Pre-flight checks ───────────────────────────────────────────────
command -v python3 >/dev/null 2>&1 || fail "python3 not found. This script expects Azure Cloud Shell (bash)."
command -v az >/dev/null 2>&1      || warn "az CLI not found — DefaultAzureCredential may still work via managed identity."

# Verify we have an active Azure session
if ! az account show >/dev/null 2>&1; then
    warn "No active Azure session. Running 'az login' ..."
    az login --use-device-code || fail "Azure login failed."
fi

TENANT_ID=$(az account show --query tenantId -o tsv 2>/dev/null)
ACCOUNT=$(az account show --query user.name -o tsv 2>/dev/null)
info "Authenticated as: ${ACCOUNT} | Tenant: ${TENANT_ID}"

# ── Virtual environment (persists in clouddrive) ─────────────────────
if [ ! -d "$VENV_DIR" ]; then
    info "Creating virtual environment (first run only) ..."
    python3 -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

# Install/upgrade deps only if requirements changed
REQ_HASH=$(md5sum "$SCRIPT_DIR/requirements.txt" 2>/dev/null | cut -d' ' -f1)
STAMP_FILE="$VENV_DIR/.req_hash"
INSTALLED_HASH=$(cat "$STAMP_FILE" 2>/dev/null || echo "")

if [ "$REQ_HASH" != "$INSTALLED_HASH" ]; then
    info "Installing dependencies ..."
    pip install --quiet --upgrade pip
    pip install --quiet -r "$SCRIPT_DIR/requirements.txt"
    echo "$REQ_HASH" > "$STAMP_FILE"
else
    info "Dependencies up to date."
fi

# ── Determine output format and dispatch ─────────────────────────────
FORMAT=""
PASSTHROUGH_ARGS=()

for arg in "$@"; do
    if [ "$FORMAT" = "NEXT" ]; then
        FORMAT="$arg"
        continue
    fi
    if [ "$arg" = "--format" ] || [ "$arg" = "-f" ]; then
        FORMAT="NEXT"
        continue
    fi
    # Handle --format=value
    if [[ "$arg" == --format=* ]]; then
        FORMAT="${arg#--format=}"
        continue
    fi
    PASSTHROUGH_ARGS+=("$arg")
done

# If FORMAT was never set (flag not passed), default to console
if [ "$FORMAT" = "" ] || [ "$FORMAT" = "NEXT" ]; then
    FORMAT="console"
fi

echo ""
info "Output format: ${FORMAT}"
info "Starting scan ..."
echo ""

# cd into the script dir so Python can resolve local imports
# (azure_cost_enrichment is imported lazily by the scanner)
cd "$SCRIPT_DIR"

# Disable set -e around the Python call so we can capture the exit code
# and give the user a useful error message instead of a silent death.
set +e
if [ "$FORMAT" = "excel" ]; then
    python3 generate_excel_report.py "${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}"
else
    python3 orphan_report.py --format "$FORMAT" "${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}"
fi
EXIT_CODE=$?
set -e
echo ""

if [ $EXIT_CODE -eq 0 ]; then
    info "Scan complete."
    if [ "$FORMAT" != "console" ]; then
        info "Output files:"
        ls -lh "$SCRIPT_DIR"/*.{json,csv,html,xlsx} 2>/dev/null | while read -r line; do
            echo "     $line"
        done
        echo ""
        warn "To download: right-click the file in the Cloud Shell file browser,"
        warn "or use: download <filename>"
    fi
else
    fail "Scan exited with code $EXIT_CODE"
fi
