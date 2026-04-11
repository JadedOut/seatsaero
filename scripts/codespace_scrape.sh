#!/usr/bin/env bash
# codespace_scrape.sh — Spin up a Codespace, scrape routes, pull results, tear down.
#
# Usage:
#   ./scripts/codespace_scrape.sh                          # All routes (canada_12.txt), one-shot
#   ./scripts/codespace_scrape.sh YYZ LAX                  # Single route
#   ./scripts/codespace_scrape.sh --routes routes/canada_us_all.txt  # Custom route file
#
# Prerequisites:
#   - gh CLI installed and authenticated
#   - Codespace secrets set: UNITED_EMAIL, UNITED_PASSWORD
#
# Set these secrets once:
#   gh secret set UNITED_EMAIL --app codespaces
#   gh secret set UNITED_PASSWORD --app codespaces

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEFAULT_ROUTES="routes/canada_12.txt"
MACHINE_TYPE="basicLinux32gb"
RETENTION="1h"
REMOTE_DB="/home/vscode/.seataero/data.db"
LOCAL_TMP_DB="/tmp/seataero_remote.db"
CS_NAME=""

# ---------------------------------------------------------------------------
# Cleanup trap — always delete the Codespace, even on Ctrl+C
# ---------------------------------------------------------------------------
cleanup() {
    if [[ -n "$CS_NAME" ]]; then
        echo ""
        echo "==> Cleaning up Codespace: $CS_NAME"
        gh codespace delete -c "$CS_NAME" --force 2>/dev/null || true
    fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
die() { echo "ERROR: $*" >&2; exit 1; }

check_prereqs() {
    command -v gh >/dev/null 2>&1 || die "gh CLI not found. Install from https://cli.github.com/"
    gh auth status >/dev/null 2>&1 || die "gh CLI not authenticated. Run: gh auth login"
}

detect_repo() {
    gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null \
        || git -C "$PROJECT_DIR" remote get-url origin 2>/dev/null \
            | sed -E 's|.*github\.com[:/]||; s|\.git$||' \
        || die "Cannot detect GitHub repo. Run from the seataero project directory."
}

wait_for_codespace() {
    local cs="$1"
    local max_wait=300
    local elapsed=0
    echo "==> Waiting for Codespace to be ready..."
    while true; do
        local state
        state=$(gh codespace list --json name,state -q ".[] | select(.name==\"$cs\") | .state" 2>/dev/null || echo "")
        if [[ "$state" == "Available" ]]; then
            echo "    Codespace is ready."
            return 0
        fi
        if (( elapsed >= max_wait )); then
            die "Codespace did not become ready within ${max_wait}s"
        fi
        sleep 5
        elapsed=$((elapsed + 5))
        echo "    State: ${state:-unknown} (${elapsed}s elapsed)"
    done
}

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
MODE="batch"
ROUTES_FILE="$DEFAULT_ROUTES"
ORIGIN=""
DEST=""

if [[ $# -eq 2 && "$1" != "--routes" ]]; then
    # Single route mode: codespace_scrape.sh YYZ LAX
    MODE="single"
    ORIGIN="$1"
    DEST="$2"
elif [[ $# -ge 1 && "$1" == "--routes" ]]; then
    # Batch mode with custom routes file
    [[ -n "${2:-}" ]] || die "Usage: $0 --routes <file>"
    ROUTES_FILE="$2"
elif [[ $# -eq 0 ]]; then
    # Default batch mode
    :
else
    echo "Usage:"
    echo "  $0                            # All routes (canada_12.txt)"
    echo "  $0 YYZ LAX                    # Single route"
    echo "  $0 --routes <file>            # Custom route file"
    exit 1
fi

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
check_prereqs

REPO=$(detect_repo)
BRANCH=$(git -C "$PROJECT_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "master")

echo "==> Repository: $REPO (branch: $BRANCH)"
if [[ "$MODE" == "single" ]]; then
    echo "==> Mode: single route ($ORIGIN → $DEST)"
else
    echo "==> Mode: batch ($ROUTES_FILE)"
fi

# --- Create Codespace ---
echo ""
echo "==> Creating Codespace (machine: $MACHINE_TYPE, retention: $RETENTION)..."
CS_NAME=$(gh codespace create \
    -R "$REPO" \
    -b "$BRANCH" \
    -m "$MACHINE_TYPE" \
    --retention-period "$RETENTION" \
    --default-permissions \
    2>&1) || die "Failed to create Codespace. Check that secrets are configured:\n  gh secret set UNITED_EMAIL --app codespaces\n  gh secret set UNITED_PASSWORD --app codespaces"

echo "    Created: $CS_NAME"

# --- Wait for ready ---
wait_for_codespace "$CS_NAME"

# --- Run scrape ---
echo ""
SCRAPE_EXIT=0
if [[ "$MODE" == "single" ]]; then
    echo "==> Scraping $ORIGIN → $DEST..."
    gh codespace ssh -c "$CS_NAME" -- \
        "cd /workspaces/seataero && python scrape.py --route $ORIGIN $DEST --headless --create-schema" \
        || SCRAPE_EXIT=$?
else
    echo "==> Running batch scrape ($ROUTES_FILE)..."
    gh codespace ssh -c "$CS_NAME" -- \
        "cd /workspaces/seataero && python scripts/burn_in.py --routes-file $ROUTES_FILE --one-shot --headless --create-schema" \
        || SCRAPE_EXIT=$?
fi

if [[ $SCRAPE_EXIT -ne 0 ]]; then
    echo "WARNING: Scrape exited with code $SCRAPE_EXIT. Attempting to retrieve partial results..."
fi

# --- Copy results ---
echo ""
echo "==> Copying results from Codespace..."
gh codespace cp -c "$CS_NAME" "remote:$REMOTE_DB" "$LOCAL_TMP_DB" 2>/dev/null || {
    echo "WARNING: Could not copy remote database. It may not exist (no results scraped)."
    echo "==> Done (no results to merge)."
    exit $SCRAPE_EXIT
}

echo "    Saved remote DB to: $LOCAL_TMP_DB"

# --- Merge into local DB ---
echo ""
echo "==> Merging remote results into local database..."
PYTHON="${SCRIPT_DIR}/experiments/.venv/Scripts/python.exe"
if [[ ! -f "$PYTHON" ]]; then
    # Fallback for non-Windows or different venv location
    PYTHON="python"
fi

if $PYTHON "$PROJECT_DIR/scripts/merge_remote_db.py" "$LOCAL_TMP_DB"; then
    echo "    Merge complete."
    rm -f "$LOCAL_TMP_DB"
else
    echo "WARNING: Merge failed. Remote DB preserved at: $LOCAL_TMP_DB"
    echo "    You can merge manually: python scripts/merge_remote_db.py $LOCAL_TMP_DB"
fi

# --- Done (cleanup runs via trap) ---
echo ""
echo "==> Done! Codespace will be deleted automatically."
exit $SCRAPE_EXIT
