# GCP Deployment Playbook

Implementation plan for hardening GitHub Actions → Cloud Run deployment using
Workload Identity Federation (WIF) and Secret Manager. Derived from the
migration done on this repo — reusable for sibling bots in the same workspace
(e.g. `game-night-decider`).

Feed this doc to Claude Code (or read it yourself) when applying the same
pattern to a new repo. Adapt values per project; the principles stay the same.

## Goal

Replace two deployment anti-patterns:

1. **Long-lived GCP service account key** stored as a GitHub secret → replace
   with WIF (short-lived OIDC token exchange).
2. **Sensitive values as plain Cloud Run env vars** (visible in the revision
   metadata to anyone with `run.revisions.get`) → replace with Secret Manager
   references, while keeping GitHub Secrets as the source of truth.

## Prerequisites

- A GCP project with Cloud Run already set up for the target service.
- `gcloud` CLI authenticated with project-owner-level permissions for setup.
- `gh` CLI authenticated for the target repo.

## Phase 1: Workload Identity Federation

Per-project, a single WIF pool + provider is shared across all bot repos.
Per-repo, a dedicated deploy service account gives isolated blast radius.

### Steps

1. **Create a deploy service account** for the repo:
   ```bash
   gcloud iam service-accounts create <REPO>-deploy \
     --project=<PROJECT_ID> \
     --display-name="GitHub Actions deployer for <REPO>"
   ```

2. **Grant deploy roles**:
   - `roles/run.admin` — deploy Cloud Run services
   - `roles/iam.serviceAccountUser` on the Cloud Run runtime SA
   - `roles/artifactregistry.repoAdmin` — push **and** delete old images
     (the deploy workflow cleans up prior image versions post-deploy;
     `roles/artifactregistry.writer` alone lacks `versions.delete`)
   - `roles/cloudsql.client` — only if the repo uses Cloud SQL

3. **Create the WIF pool and OIDC provider** (once per GCP project):
   ```bash
   gcloud iam workload-identity-pools create github-actions \
     --project=<PROJECT_ID> --location=global

   gcloud iam workload-identity-pools providers create-oidc github \
     --project=<PROJECT_ID> --location=global \
     --workload-identity-pool=github-actions \
     --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
     --attribute-condition="assertion.repository in ['<OWNER>/<REPO_1>','<OWNER>/<REPO_2>']" \
     --issuer-uri="https://token.actions.githubusercontent.com"
   ```

   For additional repos later, update the condition's allowlist.

4. **Bind the repo's WIF principal to its deploy SA**:
   ```bash
   gcloud iam service-accounts add-iam-policy-binding \
     <REPO>-deploy@<PROJECT_ID>.iam.gserviceaccount.com \
     --project=<PROJECT_ID> \
     --role="roles/iam.workloadIdentityUser" \
     --member="principalSet://iam.googleapis.com/projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/github-actions/attribute.repository/<OWNER>/<REPO>"
   ```

5. **Add GitHub repo secrets**:
   - `WIF_PROVIDER`: full resource name from `gcloud iam workload-identity-pools providers describe`
   - `WIF_SERVICE_ACCOUNT`: deploy SA email

6. **Codify as a script**: `scripts/setup_wif.sh` (template exists in this
   repo — copy and adapt the config block per project).

## Phase 2: Deploy workflow — auth and docker

**Do not use** `gcloud auth configure-docker` with WIF — its credential
helper re-impersonates the SA on every push and fails with
`iam.serviceAccounts.getAccessToken denied`. Instead, pre-exchange the
federation token for an OAuth2 access token and use it directly with
`docker login`:

```yaml
permissions:
  contents: read
  id-token: write

steps:
  - name: Google Auth
    id: auth
    uses: google-github-actions/auth@v3
    with:
      workload_identity_provider: '${{ secrets.WIF_PROVIDER }}'
      service_account: '${{ secrets.WIF_SERVICE_ACCOUNT }}'
      token_format: 'access_token'

  - name: Docker Login to Artifact Registry
    uses: docker/login-action@v3
    with:
      registry: us-central1-docker.pkg.dev
      username: oauth2accesstoken
      password: ${{ steps.auth.outputs.access_token }}
```

This is documented as a supported Artifact Registry auth method — the
hostname `*-docker.pkg.dev` accepts `oauth2accesstoken` + a valid OAuth2
token as HTTP Basic credentials.

## Phase 3: Secret Manager as a relay

**Design:** GitHub Secrets stays authoritative. Secret Manager is
populated from GitHub on opt-in rotation via a `sync_secrets` workflow
input. Cloud Run reads from Secret Manager at runtime so plain values
never appear in Cloud Run revision details.

### Setup

1. **Create secret containers** (namespaced per bot to avoid collisions
   in a shared GCP project):
   ```bash
   gcloud secrets create <prefix>-telegram-token \
     --project=<PROJECT_ID> --replication-policy=automatic
   ```

2. **Grant IAM**:
   - Cloud Run runtime SA → `roles/secretmanager.secretAccessor` on each secret
   - Deploy SA → `roles/secretmanager.secretVersionManager` (add + destroy versions)

3. **Codify as a script**: `scripts/setup_secrets.sh` (template exists
   in this repo).

### Workflow changes

Add an opt-in input and sync step:

```yaml
on:
  workflow_dispatch:
    inputs:
      sync_secrets:
        description: 'Push current GitHub secret values to Secret Manager (after rotation)'
        type: boolean
        default: false
  workflow_run:
    workflows: ["Release"]
    types: [completed]

jobs:
  deploy:
    steps:
      # ... WIF auth, docker login ...

      - name: Sync secrets to Secret Manager
        # Guard on event_name too — workflow_run-triggered deploys don't have
        # `inputs` at all, and we never want them to re-sync.
        if: ${{ github.event_name == 'workflow_dispatch' && inputs.sync_secrets }}
        env:
          # Pass secrets via env: (not direct interpolation inside run:) so
          # shell metacharacters in values can't break the script.
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          WEBHOOK_SECRET: ${{ secrets.WEBHOOK_SECRET }}
        run: |
          set -euo pipefail
          sync_secret() {
            local id="$1" val="$2"
            local prev
            prev=$(gcloud secrets versions list "$id" --project="$PROJECT_ID" \
              --filter="state=ENABLED" --format="value(name)" --sort-by="~createTime")
            printf '%s' "$val" | gcloud secrets versions add "$id" \
              --project="$PROJECT_ID" --data-file=-
            for v in $prev; do
              gcloud secrets versions destroy "$v" --secret="$id" \
                --project="$PROJECT_ID" --quiet || true
            done
          }
          sync_secret "<prefix>-telegram-token" "$TELEGRAM_BOT_TOKEN"
          sync_secret "<prefix>-webhook-secret" "$WEBHOOK_SECRET"
          # ... one per secret ...
```

The sync destroys prior versions so exactly 1 active version per secret
stays (keeps within Secret Manager's free tier of 6 active versions).

Update the Cloud Run deploy step — move sensitive vars from `env_vars:`
to `secrets:`:

```yaml
env_vars: |
  WEBHOOK_URL=${{ secrets.WEBHOOK_URL }}
secrets: |
  TELEGRAM_BOT_TOKEN=<prefix>-telegram-token:latest
  WEBHOOK_SECRET=<prefix>-webhook-secret:latest
```

> Cloud Run resolves `:latest` at **deploy time**, not request time. After
> rotating a secret, a redeploy picks up the new version — `sync_secrets: true`
> does both in one workflow run.

### Rotation procedure

1. Update the value in GitHub (`gh secret set SECRET_NAME`).
2. Trigger the deploy workflow with `sync_secrets: true`.

Normal deploys (release-driven or routine `workflow_dispatch` without
the flag) skip the sync — no version churn and no unnecessary writes.

## Phase 4: Workload-specific considerations

### Resource tuning (lessons learned)

- **Python bots using `lxml` + `tornado` + `python-telegram-bot`** have a
  ~270MB baseline. `--memory=256Mi` is insufficient and causes OOM at
  startup. Use 512Mi minimum — still well within Cloud Run free tier.
- **Concurrency vs memory**: each in-flight request holds parsed
  response state. For I/O-bound workloads, `--concurrency=40` is plenty
  and caps peak transient memory. `--concurrency=80` (the default) can
  multiply peak memory unnecessarily.
- **Run `python main.py` directly** in the container instead of `uv run main.py`
  — avoids keeping `uv` resident (~20-30MB savings). Put the venv on
  `PATH`: `ENV PATH="/app/.venv/bin:$PATH"`.

### Cloud SQL + migrations

- The migration step runs on the GitHub runner, not in Cloud Run. It
  can keep reading DB credentials from GitHub secrets directly for
  simplicity — no need to round-trip through Secret Manager.
- Ensure the deploy SA has `roles/cloudsql.client` for the proxy step.

## Phase 5: Verification and cutover

1. Open PR with the workflow + setup script changes. Don't delete
   `GCP_CREDENTIALS` yet.
2. Merge PR. Trigger `workflow_dispatch` with `sync_secrets: true` once
   to seed Secret Manager.
3. Confirm the service responds and (if applicable) migrations ran.
4. Only then: delete the old SA key from GCP and remove
   `GCP_CREDENTIALS` from GitHub secrets.

## Gotchas (learned the hard way)

- **principalSet case-sensitivity**: the `attribute.repository/<OWNER>/<REPO>`
  path must exactly match what GitHub's OIDC token emits — preserve the
  owner's canonical case. Lowercase owner = silent IAM mismatch that
  looks like "permission denied" at deploy time.
- **Attribute condition scoping**: prefer `assertion.repository in [...]`
  (exact allowlist) over `repository_owner` (case ambiguity).
- **Never use `gcloud auth configure-docker` with WIF** — use
  `docker/login-action` + `token_format: access_token`.
- **Setup scripts are templates**: check in placeholder versions, don't
  commit real SA emails or project IDs.
- **Sync step must be opt-in** — gated on `workflow_dispatch` input,
  never auto-runs on release. Preserves rotation idempotency and keeps
  version count bounded.

## Deliverables for a new repo

- Adapted `scripts/setup_wif.sh`
- Adapted `scripts/setup_secrets.sh`
- Updated `.github/workflows/deploy.yml` with: WIF auth, docker-login,
  sync_secrets input, Cloud Run `secrets:` references
- Short README section on rotation procedure
