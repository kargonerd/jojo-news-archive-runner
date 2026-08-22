#!/usr/bin/env bash
set -euo pipefail

# Scheduled workflows only execute from the repository's default branch.  The
# archive/parser jobs, however, intentionally run from a research branch.  A
# completed batch therefore gives the branch-local watchdog an explicit,
# idempotent wake-up so the next available parser/source slot is filled.
: "${GH_TOKEN:?GH_TOKEN is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${GITHUB_REF_NAME:?GITHUB_REF_NAME is required}"

active_watchdogs="$(
  gh run list \
    --repo "$GITHUB_REPOSITORY" \
    --workflow parser-validation-watchdog.yml \
    --branch "$GITHUB_REF_NAME" \
    --limit 20 \
    --json status \
    --jq '[.[] | select(.status == "queued" or .status == "in_progress" or .status == "waiting" or .status == "pending" or .status == "requested")] | length' \
    2>/dev/null || true
)"

if [[ "${active_watchdogs:-}" =~ ^[0-9]+$ ]] && [ "$active_watchdogs" -gt 0 ]; then
  echo "Parser validation watchdog already queued/running; skip wake-up."
  exit 0
fi

for attempt in 1 2 3; do
  if gh workflow run parser-validation-watchdog.yml \
      --repo "$GITHUB_REPOSITORY" \
      --ref "$GITHUB_REF_NAME"; then
    echo "Woke parser validation watchdog on $GITHUB_REF_NAME."
    exit 0
  fi
  if [ "$attempt" -lt 3 ]; then
    sleep $((attempt * 5))
  fi
done

# A wake-up is best effort.  The completed archive checkpoint is durable and
# a later batch or manual watchdog invocation can recover from a transient API
# failure without turning a successful capture into a failed workflow.
echo "::warning::Could not wake parser validation watchdog after 3 attempts."
exit 0
