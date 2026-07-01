#!/usr/bin/env bash
# build-bundle.sh — produce the Cloud Shell deliverable tarball.
#
# Ships ONLY an explicit whitelist of source files (never the whole
# cloudshell/ directory), so scan output (xlsx/csv/json/log/_runs) and
# compiled bytecode can never be bundled and handed to a client. Also:
#   - refuses to build if any file is missing
#   - trips if a GUID-shaped string (a stray tenant/subscription id) is
#     present in any bundled file
#   - verifies run.sh is LF (a CRLF shebang fails on Linux Cloud Shell)
#
# Usage:  ./build-bundle.sh [output-path]
#         (default output: ../azure-orphan-cloudshell.tar.gz)
set -euo pipefail
cd "$(dirname "$0")"

OUT="${1:-../azure-orphan-cloudshell.tar.gz}"

# Explicit whitelist — the ONLY files that ever go in the deliverable.
FILES=(
  cloudshell/orphan_report.py
  cloudshell/generate_excel_report.py
  cloudshell/azure_cost_enrichment.py
  cloudshell/orphan_cleanup.py
  cloudshell/generate_pptx_slide.py
  cloudshell/vm_backup_gap_analysis.py
  cloudshell/requirements.txt
  cloudshell/run.sh
)

for f in "${FILES[@]}"; do
  [ -f "$f" ] || { echo "ERROR: missing $f — bundle not built."; exit 1; }
done

# Tripwire: a GUID-shaped string in a bundled file usually means a hardcoded
# tenant or subscription id slipped in. Refuse to ship it.
if grep -lE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' "${FILES[@]}"; then
  echo "ERROR: a bundled file contains a GUID-shaped string (possible hardcoded"
  echo "       tenant/subscription id). Remove it before shipping to a client."
  exit 1
fi

rm -f "$OUT"
tar -czf "$OUT" "${FILES[@]}"

# A CRLF shebang makes run.sh fail on Linux with "cannot execute: required
# file not found". Verify the extracted copy is LF.
if tar -xzOf "$OUT" cloudshell/run.sh | file - | grep -q CRLF; then
  echo "ERROR: run.sh has CRLF line endings — check .gitattributes / core.autocrlf."
  rm -f "$OUT"
  exit 1
fi

echo "Built deliverable: $OUT"
echo "Contents:"
tar -tzf "$OUT" | sed 's/^/  /'
echo "run.sh line endings: LF (verified)"
