#!/usr/bin/env bash
# Deploy the Budget Coach API to Cloud Run.
#
# Prerequisites (one-time):
#   gcloud auth login
#   gcloud config set project "$GOOGLE_CLOUD_PROJECT"
#   gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
#                          artifactregistry.googleapis.com aiplatform.googleapis.com \
#                          sqladmin.googleapis.com
#
# Postgres: this script does NOT provision a database. Point DATABASE_URL at
# Cloud SQL, a VPC-reachable Postgres, or a serverless Postgres.
#
# Usage:
#   ./scripts/deploy_cloud_run.sh                      # uses defaults from .env
#   SERVICE=budget-coach-prod REGION=europe-north1 ./scripts/deploy_cloud_run.sh
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
DATABASE_URL_VALUE="${DATABASE_URL:?set DATABASE_URL to a reachable Postgres}"
MODEL="${BUDGET_AGENT_MODEL:-gemini-2.5-flash}"
AUTH_MODE_VALUE="${AUTH_MODE:?set AUTH_MODE to firebase or google}"
CLOUD_SQL_ARGS=()
if [[ -n "${CLOUD_SQL_CONNECTION_NAME:-}" ]]; then
  CLOUD_SQL_ARGS=(--add-cloudsql-instances "${CLOUD_SQL_CONNECTION_NAME}")
fi

OPTIONAL_ENV_ARGS=()
add_optional_env_var() {
  local name="$1"
  local value="${!name:-}"
  if [[ -n "${value}" ]]; then
    OPTIONAL_ENV_ARGS+=(--set-env-vars "${name}=${value}")
  fi
}

AUTH_ENV_VARS="AUTH_MODE=${AUTH_MODE_VALUE}"
case "${AUTH_MODE_VALUE}" in
  firebase)
    AUTH_ENV_VARS="${AUTH_ENV_VARS},FIREBASE_PROJECT_ID=${FIREBASE_PROJECT_ID:?set FIREBASE_PROJECT_ID}"
    ;;
  google)
    AUTH_ENV_VARS="${AUTH_ENV_VARS},GOOGLE_OAUTH_CLIENT_ID=${GOOGLE_OAUTH_CLIENT_ID:?set GOOGLE_OAUTH_CLIENT_ID}"
    ;;
  *)
    echo "Unsupported AUTH_MODE=${AUTH_MODE_VALUE}. Use firebase or google for Cloud Run." >&2
    exit 1
    ;;
esac

for name in \
  SEED_USER_EMAIL \
  SEED_PARTNER_EMAIL \
  DEFAULT_HOUSEHOLD_NAME \
  FIREBASE_API_KEY \
  FIREBASE_AUTH_DOMAIN \
  FIREBASE_APP_ID \
  FIREBASE_MESSAGING_SENDER_ID \
  WEB_PUSH_VAPID_PUBLIC_KEY \
  WEB_PUSH_VAPID_PRIVATE_KEY \
  WEB_PUSH_VAPID_SUBJECT \
  BUDGET_AGENT_SESSION_SECRET \
  BUDGET_AGENT_SESSION_TTL_SECONDS
do
  add_optional_env_var "${name}"
done

if [[ -z "${BUDGET_AGENT_SESSION_SECRET:-}" ]]; then
  echo "WARN: BUDGET_AGENT_SESSION_SECRET is not set — the deploy will fall back" >&2
  echo "      to a hard-coded dev secret. Generate one and add to .env:" >&2
  echo "        echo \"BUDGET_AGENT_SESSION_SECRET=\$(python -c 'import secrets; print(secrets.token_urlsafe(48))')\" >> .env" >&2
fi

echo "Deploying ${SERVICE} to ${PROJECT}/${REGION} ..."

# `--source .` uses Cloud Build to build the Dockerfile in this repo and push
# to Artifact Registry automatically. The first deploy creates the repo;
# subsequent deploys reuse it.
gcloud run deploy "${SERVICE}" \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --source . \
  --platform managed \
  --allow-unauthenticated \
  --quiet \
  "${CLOUD_SQL_ARGS[@]}" \
  "${OPTIONAL_ENV_ARGS[@]}" \
  --set-env-vars "GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=${PROJECT},GOOGLE_CLOUD_LOCATION=${REGION},BUDGET_AGENT_MODEL=${MODEL}" \
  --set-env-vars "${AUTH_ENV_VARS}" \
  --set-env-vars "DATABASE_URL=${DATABASE_URL_VALUE}" \
  --memory 1Gi \
  --cpu 1 \
  --timeout 300

URL=$(gcloud run services describe "${SERVICE}" \
  --project "${PROJECT}" --region "${REGION}" \
  --format "value(status.url)")

# Prominent banner so the URL is impossible to miss in scrolling output.
BOLD=$(tput bold 2>/dev/null || true)
GREEN=$(tput setaf 2 2>/dev/null || true)
RESET=$(tput sgr0 2>/dev/null || true)
LINE=$(printf '─%.0s' $(seq 1 ${#URL})$(seq 1 12))

echo
echo "${BOLD}${GREEN}┌${LINE}┐${RESET}"
echo "${BOLD}${GREEN}│  Deployed:  ${URL}  │${RESET}"
echo "${BOLD}${GREEN}└${LINE}┘${RESET}"
echo
echo "Re-fetch later:  ./scripts/url.sh"
echo
echo "Smoke test:"
echo "  curl -fsS \"${URL}/readyz\""
echo "  curl -fsS \"${URL}/me\" -H 'authorization: Bearer <ID_TOKEN>'"
echo "  curl -fsS -X POST \"${URL}/chat\" -H 'authorization: Bearer <ID_TOKEN>' -H 'content-type: application/json' \\"
echo "       -d '{\"prompt\":\"How are we doing this month?\"}' | jq"
echo
# Also write to a file so other scripts can pick it up without re-querying gcloud.
echo "${URL}" > .last_deploy_url
echo "(URL also written to .last_deploy_url)"
