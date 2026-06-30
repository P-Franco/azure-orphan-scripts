#!/bin/bash
# ──────────────────────────────────────────────────────────────────────
# Azure Orphaned Resources Toolkit — Cloud Shell Runner
#
# Usage (from Cloud Shell):
#   cd ~/clouddrive/cloudshell   # or wherever you uploaded this folder
#   chmod +x run.sh
#
# Scan (default command):
#   ./run.sh                       # scan, console output
#   ./run.sh --format excel        # formatted Excel workbook
#   ./run.sh --format json|csv|html
#   ./run.sh --tenant <id>         # scope to a specific tenant (recommended)
#   ./run.sh -s <sub-id>           # scan a single subscription
#
# Other commands (bootstrap the same venv):
#   ./run.sh excel    [args]       # Excel report (same as scan --format excel)
#   ./run.sh cleanup  [args]       # delete orphans — DRY-RUN unless --confirm
#   ./run.sh pptx     [args]       # CIR PowerPoint deck (needs --input scan.json)
#   ./run.sh vm-backup [args]      # VM backup gap analysis
#
# Examples:
#   ./run.sh cleanup --tenant <id> --ids-file approved.txt            # preview
#   ./run.sh cleanup --tenant <id> --ids-file approved.txt --confirm  # delete
#
# All flags pass straight through to the underlying Python script.
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

# ── Command dispatch ─────────────────────────────────────────────────
# First positional arg may name a command; anything else (a flag, or
# nothing) defaults to a scan, preserving the original `./run.sh --format`
# interface.
COMMAND="scan"
case "${1:-}" in
    scan|excel|cleanup|pptx|vm-backup)
        COMMAND="$1"; shift ;;
esac

# cd into the script dir so Python can resolve local imports
# (azure_cost_enrichment / orphan_report are imported by their siblings).
cd "$SCRIPT_DIR"

set +e
case "$COMMAND" in
    scan)
        # Parse --format out of the remaining args; default to console.
        FORMAT=""
        PASSTHROUGH_ARGS=()
        for arg in "$@"; do
            if [ "$FORMAT" = "NEXT" ]; then FORMAT="$arg"; continue; fi
            if [ "$arg" = "--format" ] || [ "$arg" = "-f" ]; then FORMAT="NEXT"; continue; fi
            if [[ "$arg" == --format=* ]]; then FORMAT="${arg#--format=}"; continue; fi
            PASSTHROUGH_ARGS+=("$arg")
        done
        if [ "$FORMAT" = "" ] || [ "$FORMAT" = "NEXT" ]; then FORMAT="console"; fi
        echo ""
        info "Scan — output format: ${FORMAT}"
        echo ""
        if [ "$FORMAT" = "excel" ]; then
            python3 generate_excel_report.py "${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}"
        else
            python3 orphan_report.py --format "$FORMAT" "${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}"
        fi
        ;;
    excel)
        echo ""; info "Generating Excel report ..."; echo ""
        python3 generate_excel_report.py "$@"
        ;;
    cleanup)
        echo ""
        warn "Cleanup is DRY-RUN unless you pass --confirm. It will only act on"
        warn "the tenant you are logged into; pass --tenant to be explicit."
        echo ""
        python3 orphan_cleanup.py "$@"
        ;;
    pptx)
        echo ""; info "Generating PowerPoint deck ..."; echo ""
        python3 generate_pptx_slide.py "$@"
        ;;
    vm-backup)
        echo ""; info "Running VM backup gap analysis ..."; echo ""
        python3 vm_backup_gap_analysis.py "$@"
        ;;
esac
EXIT_CODE=$?
set -e
echo ""

if [ $EXIT_CODE -eq 0 ]; then
    info "${COMMAND} complete."
    if [ "$COMMAND" = "scan" ] || [ "$COMMAND" = "excel" ]; then
        info "Output files:"
        ls -lh "$SCRIPT_DIR"/*.{json,csv,html,xlsx} 2>/dev/null | while read -r line; do
            echo "     $line"
        done
        echo ""
        warn "To download: right-click the file in the Cloud Shell file browser,"
        warn "or use: download <filename>"
    fi
else
    fail "${COMMAND} exited with code $EXIT_CODE"
fi
