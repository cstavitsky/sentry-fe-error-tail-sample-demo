#!/bin/sh
# Writes a customer-safe copy of this repo (used by `make export-customer`).
#
#   sh scripts/export_customer.sh [DEST]      (default DEST: dist-customer)
#
# Leaves out internal working files: SPEC.md, REVIEW.md, TEST_REPORT.md, research/
# (except research/andrei_snippet.md, which TRANSPORT_NOTES.md links to), the fake
# Sentry API used to test the verifier, build/run output, node_modules and .env.
# Then fails if any internal name is still found in the copy.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
DEST=${1:-dist-customer}
case "$DEST" in /*) ;; *) DEST="$ROOT/$DEST" ;; esac

rm -rf "$DEST"
mkdir -p "$DEST"

# Copy (skipping the big/derived directories by name), then prune exact paths.
(cd "$ROOT" && tar -cf - --exclude node_modules --exclude .venv --exclude venv --exclude dist-customer --exclude __pycache__ --exclude .env .) \
  | (cd "$DEST" && tar -xf -)

cd "$DEST"
rm -rf SPEC.md REVIEW.md TEST_REPORT.md out frontend/dist tests/local/fake_sentry_api.py .env
find . -name '*.pyc' -exec rm -f {} +
find . -name '.DS_Store' -exec rm -f {} +
if [ -d research ]; then
  find research -mindepth 1 ! -path research/andrei_snippet.md -exec rm -rf {} + 2>/dev/null || true
fi

# Internal names must not appear anywhere in the copy (case-insensitive). The pattern
# is split into pieces so this script does not match itself.
PATTERN="base""ten|beef""eater|ora""cle|cstav""itsky"
if grep -RIniE "$PATTERN" . ; then
  echo "export-customer: FAILED - internal names found above; nothing should be shared until fixed" >&2
  exit 1
fi

echo "export-customer: wrote $DEST ($(find . -type f | wc -l | tr -d ' ') files); internal-name check: 0 hits"
