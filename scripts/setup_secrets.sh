#!/usr/bin/env bash
#
# Setup Google Secret Manager for Cloud Run services whose secret values
# are managed in GitHub Secrets.
#
# Design: GitHub Secrets remains the source of truth. Secret Manager is a
# relay so plain values don't appear in Cloud Run revision metadata. Values
# are pushed from GitHub Actions on rotation (see the companion deploy
# workflow's sync_secrets input), keeping one active version per secret.
#
# Prerequisites:
#   - WIF setup already done (deploy SA exists, trusted by GitHub OIDC)
#   - gcloud CLI authenticated with sufficient privileges to enable APIs,
#     create secrets, and set IAM bindings (typically project owner)
#   - Target Cloud Run services already exist (script reads their runtime SAs)
#
# Usage:
#   DRY_RUN=1 bash scripts/setup_secrets.sh   # preview
#   bash scripts/setup_secrets.sh             # execute
#
# After running: trigger each repo's deploy workflow with sync_secrets=true
# to seed initial values from GitHub Secrets.

set -euo pipefail

# ─── Configuration ───────────────────────────────────────────────────────────
PROJECT_ID="<YOUR_GCP_PROJECT_ID>"  # TODO: replace
REGION="us-central1"

# Per-repo configuration. Add an entry in each map per bot repo.
# Keys: the GitHub repo name (short form, without owner).
#   REPO_SERVICE:    Cloud Run service name (runtime SA is read from it)
#   REPO_SECRETS:    space-separated list of Secret Manager secret IDs
#   REPO_DEPLOY_SA:  the WIF deploy SA for that repo (needs version-manager)
declare -A REPO_SERVICE=(
  ["game-night-decider"]="game-night-decider"
)
declare -A REPO_SECRETS=(
  ["game-night-decider"]="gnd-telegram-token gnd-webhook-secret gnd-database-url"
)
declare -A REPO_DEPLOY_SA=(
  ["game-night-decider"]="<DEPLOY_SA_EMAIL>"
)
# ─────────────────────────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

if [[ "$PROJECT_ID" == *"YOUR_GCP"* ]]; then
  echo -e "${RED}Error: Update PROJECT_ID in the script before running.${NC}"
  exit 1
fi

for sa in "${REPO_DEPLOY_SA[@]}"; do
  if [[ "$sa" == *"DEPLOY_SA_EMAIL"* ]]; then
    echo -e "${RED}Error: Update REPO_DEPLOY_SA emails before running.${NC}"
    exit 1
  fi
done

run() {
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    echo -e "${YELLOW}[DRY RUN]${NC} $*"
  else
    echo -e "${GREEN}[RUN]${NC} $*"
    "$@"
  fi
}

echo "=== Secret Manager Setup ==="
echo "Project: $PROJECT_ID"
echo ""

# ─── Step 1: Enable API ──────────────────────────────────────────────────────
echo -e "${GREEN}Step 1: Enabling Secret Manager API...${NC}"
run gcloud services enable secretmanager.googleapis.com --project="$PROJECT_ID"

# ─── Step 2-4: Per-repo processing ───────────────────────────────────────────
for repo in "${!REPO_SERVICE[@]}"; do
  service="${REPO_SERVICE[$repo]}"
  secret_ids="${REPO_SECRETS[$repo]}"
  deploy_sa="${REPO_DEPLOY_SA[$repo]}"

  echo ""
  echo "=========================================="
  echo -e "${GREEN}Processing: $repo (service: $service)${NC}"
  echo "=========================================="

  # Detect runtime service account
  runtime_sa=$(gcloud run services describe "$service" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format="value(spec.template.spec.serviceAccountName)" 2>/dev/null || echo "")

  if [[ -z "$runtime_sa" ]]; then
    project_number=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
    runtime_sa="${project_number}-compute@developer.gserviceaccount.com"
    echo -e "${YELLOW}  No custom runtime SA set — using Compute default: $runtime_sa${NC}"
  else
    echo "  Runtime SA: $runtime_sa"
  fi
  echo "  Deploy SA:  $deploy_sa"

  # ─── Step 2: Create secret containers (empty — values come from GH Actions) ─
  echo ""
  echo -e "${GREEN}Step 2: Creating secret containers...${NC}"
  for secret_id in $secret_ids; do
    if gcloud secrets describe "$secret_id" --project="$PROJECT_ID" &>/dev/null; then
      echo "  Secret '$secret_id' already exists, skipping create."
    else
      run gcloud secrets create "$secret_id" \
        --project="$PROJECT_ID" \
        --replication-policy="automatic" \
        --labels="repo=${repo},managed-by=setup-script"
    fi
  done

  # ─── Step 3: Runtime SA gets read-only access ──────────────────────────────
  echo ""
  echo -e "${GREEN}Step 3: Granting secretAccessor to runtime SA...${NC}"
  for secret_id in $secret_ids; do
    run gcloud secrets add-iam-policy-binding "$secret_id" \
      --project="$PROJECT_ID" \
      --role="roles/secretmanager.secretAccessor" \
      --member="serviceAccount:${runtime_sa}"
  done

  # ─── Step 4: Deploy SA gets version-manager (add + destroy) ────────────────
  # secretVersionManager allows: add, enable, disable, destroy versions.
  # Does NOT include access (read value) — that's separate and not needed
  # for the sync step (it only writes).
  echo ""
  echo -e "${GREEN}Step 4: Granting secretVersionManager to deploy SA...${NC}"
  for secret_id in $secret_ids; do
    run gcloud secrets add-iam-policy-binding "$secret_id" \
      --project="$PROJECT_ID" \
      --role="roles/secretmanager.secretVersionManager" \
      --member="serviceAccount:${deploy_sa}"
  done
done

# ─── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "==========================================="
echo -e "${GREEN}Secret Manager setup complete!${NC}"
echo ""
echo "Next steps:"
echo ""
echo "1. Merge the deploy.yml changes that move sensitive env_vars → secrets."
echo ""
echo "2. Seed initial secret values from GitHub Secrets:"
echo "   For each repo, trigger the deploy workflow with sync_secrets=true:"
echo "     gh workflow run deploy.yml --repo <owner>/<repo> -f sync_secrets=true"
echo ""
echo "3. Verify the deploy succeeded and the service works."
echo ""
echo "4. Future rotations:"
echo "   a. Update the GitHub repo secret (gh secret set ...)"
echo "   b. Trigger deploy with sync_secrets=true"
echo "   (Normal deploys without the flag skip the sync and reuse stored values.)"
echo ""
echo "Cost note: the sync step keeps exactly 1 active version per secret."
echo "Secret Manager's free tier covers 6 active versions; beyond that,"
echo "each additional active version costs ~\$0.06/month."
