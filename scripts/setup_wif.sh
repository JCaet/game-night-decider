#!/usr/bin/env bash
#
# Setup Workload Identity Federation for GitHub Actions → GCP authentication.
#
# This script replaces long-lived service account keys (GCP_CREDENTIALS secret)
# with keyless, short-lived token exchange via OIDC.
#
# Prerequisites:
#   - gcloud CLI installed and authenticated as a project owner
#   - gh CLI installed and authenticated
#   - The GitHub repos must already exist
#
# Usage:
#   # Dry run (prints commands without executing):
#   DRY_RUN=1 bash scripts/setup_wif.sh
#
#   # Execute:
#   bash scripts/setup_wif.sh
#
# After running this script, update each repo's deploy.yml to use WIF auth
# (see the printed instructions at the end).

set -euo pipefail

# ─── Configuration ───────────────────────────────────────────────────────────
# Shared GCP settings
PROJECT_ID="<YOUR_GCP_PROJECT_ID>"  # TODO: replace with your GCP project ID
REGION="us-central1"

# WIF pool/provider names (one pool, one provider — shared across repos)
POOL_ID="github-actions"
POOL_DISPLAY_NAME="GitHub Actions"
PROVIDER_ID="github"
PROVIDER_DISPLAY_NAME="GitHub"

# GitHub owner (org or user)
GITHUB_OWNER="JCaet"

# Repos and their corresponding GCP service accounts.
# Add new bots here — the script loops over them.
declare -A REPOS=(
  ["JCaet/boardgame-search-telegram-bot"]="<DEPLOY_SA_EMAIL>"  # TODO: replace
  ["JCaet/game-night-decider"]="<DEPLOY_SA_EMAIL>"             # TODO: replace
)
# ─────────────────────────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Validate configuration
if [[ "$PROJECT_ID" == *"YOUR_GCP"* ]]; then
  echo -e "${RED}Error: Update PROJECT_ID in the script before running.${NC}"
  exit 1
fi

for sa in "${REPOS[@]}"; do
  if [[ "$sa" == *"DEPLOY_SA_EMAIL"* ]]; then
    echo -e "${RED}Error: Update service account emails in REPOS before running.${NC}"
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

echo "=== Workload Identity Federation Setup ==="
echo "Project: $PROJECT_ID"
echo ""

# ─── Step 1: Enable required APIs ────────────────────────────────────────────
echo -e "${GREEN}Step 1: Enabling required APIs...${NC}"
run gcloud services enable iamcredentials.googleapis.com \
  --project="$PROJECT_ID"
run gcloud services enable sts.googleapis.com \
  --project="$PROJECT_ID"

# ─── Step 2: Create Workload Identity Pool ────────────────────────────────────
echo ""
echo -e "${GREEN}Step 2: Creating Workload Identity Pool...${NC}"

if gcloud iam workload-identity-pools describe "$POOL_ID" \
    --project="$PROJECT_ID" --location="global" &>/dev/null; then
  echo "Pool '$POOL_ID' already exists, skipping."
else
  run gcloud iam workload-identity-pools create "$POOL_ID" \
    --project="$PROJECT_ID" \
    --location="global" \
    --display-name="$POOL_DISPLAY_NAME"
fi

# ─── Step 3: Create OIDC Provider ────────────────────────────────────────────
echo ""
echo -e "${GREEN}Step 3: Creating OIDC Provider...${NC}"

# Attribute condition: only accept tokens from repos owned by our GitHub account.
# Per-repo restrictions are enforced at the SA binding level (step 4).
ATTRIBUTE_CONDITION="assertion.repository_owner == '${GITHUB_OWNER}'"

if gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" \
    --project="$PROJECT_ID" --location="global" \
    --workload-identity-pool="$POOL_ID" &>/dev/null; then
  echo "Provider '$PROVIDER_ID' already exists, skipping."
else
  run gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_ID" \
    --project="$PROJECT_ID" \
    --location="global" \
    --workload-identity-pool="$POOL_ID" \
    --display-name="$PROVIDER_DISPLAY_NAME" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner,attribute.ref=assertion.ref" \
    --attribute-condition="$ATTRIBUTE_CONDITION" \
    --issuer-uri="https://token.actions.githubusercontent.com"
fi

# ─── Step 4: Bind each repo to its service account ───────────────────────────
echo ""
echo -e "${GREEN}Step 4: Creating IAM bindings (repo → service account)...${NC}"

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")

for repo in "${!REPOS[@]}"; do
  sa_email="${REPOS[$repo]}"
  member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/attribute.repository/${repo}"

  echo ""
  echo "  Binding: $repo → $sa_email"
  run gcloud iam service-accounts add-iam-policy-binding "$sa_email" \
    --project="$PROJECT_ID" \
    --role="roles/iam.workloadIdentityUser" \
    --member="$member"
done

# ─── Step 5: Get the full provider resource name ─────────────────────────────
echo ""
echo -e "${GREEN}Step 5: Retrieving provider resource name...${NC}"

PROVIDER_NAME=$(gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" \
  --project="$PROJECT_ID" \
  --location="global" \
  --workload-identity-pool="$POOL_ID" \
  --format="value(name)")

echo "  Provider: $PROVIDER_NAME"

# ─── Step 6: Set GitHub repo secrets ──────────────────────────────────────────
echo ""
echo -e "${GREEN}Step 6: Setting GitHub repo secrets...${NC}"

for repo in "${!REPOS[@]}"; do
  sa_email="${REPOS[$repo]}"
  echo ""
  echo "  Setting secrets for $repo..."
  run gh secret set WIF_PROVIDER --repo="$repo" --body="$PROVIDER_NAME"
  run gh secret set WIF_SERVICE_ACCOUNT --repo="$repo" --body="$sa_email"
done

# ─── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "==========================================="
echo -e "${GREEN}WIF setup complete!${NC}"
echo ""
echo "Next steps:"
echo "  1. Update each repo's deploy.yml — replace the Google Auth step:"
echo ""
echo "     - name: Google Auth"
echo "       id: auth"
echo "       uses: google-github-actions/auth@v3"
echo "       with:"
echo "         workload_identity_provider: \${{ secrets.WIF_PROVIDER }}"
echo "         service_account: \${{ secrets.WIF_SERVICE_ACCOUNT }}"
echo ""
echo "  2. Trigger a deploy and verify it succeeds."
echo "  3. Once confirmed, delete the old service account key from GCP:"
echo "     gcloud iam service-accounts keys list --iam-account=<SA_EMAIL>"
echo "     gcloud iam service-accounts keys delete <KEY_ID> --iam-account=<SA_EMAIL>"
echo "  4. Remove the GCP_CREDENTIALS secret from each GitHub repo:"
echo "     gh secret delete GCP_CREDENTIALS --repo=<REPO>"
