#!/usr/bin/env bash
# Print the URL of the deployed Cloud Run service.
#
# Usage:
#   ./scripts/url.sh                         # uses defaults from .env
#   SERVICE=budget-coach-prod ./scripts/url.sh
set -euo pipefail

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

PROJECT="${GOOGLE_CLOUD_PROJECT:?set GOOGLE_CLOUD_PROJECT}"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
SERVICE="${SERVICE:-budget-coach}"

URL=$(gcloud run services describe "${SERVICE}" \
  --project "${PROJECT}" --region "${REGION}" \
  --format "value(status.url)" 2>/dev/null || true)

if [[ -z "${URL}" ]]; then
  echo "no Cloud Run service '${SERVICE}' in ${PROJECT}/${REGION}" >&2
  echo "deploy with: ./scripts/deploy_cloud_run.sh" >&2
  exit 1
fi

echo "${URL}"
