#!/usr/bin/env bash
# Creates the five Secret Manager secrets for the Cloud Run service and lets the runtime service
# account read them. Run it yourself in a terminal: it asks for each value with hidden input, so
# nothing is shown, logged or stored in your shell history. Safe to re-run: an existing secret
# gets a new version (that is also how you rotate one, see docs/DEPLOY.md).
set -euo pipefail

GCLOUD="${GCLOUD:-$HOME/google-cloud-sdk/bin/gcloud}"
PROJECT=grocery-planner-deployment
RUNTIME_SA="grocery-planner-run@${PROJECT}.iam.gserviceaccount.com"

put() {  # put NAME  (value on stdin)
  local name="$1"
  if "$GCLOUD" secrets describe "$name" --project="$PROJECT" >/dev/null 2>&1; then
    "$GCLOUD" secrets versions add "$name" --data-file=- --project="$PROJECT" >/dev/null
  else
    "$GCLOUD" secrets create "$name" --data-file=- --replication-policy=automatic \
      --project="$PROJECT" >/dev/null
  fi
  # Least privilege: this account may read this one secret, nothing else.
  "$GCLOUD" secrets add-iam-policy-binding "$name" --project="$PROJECT" \
    --member="serviceAccount:${RUNTIME_SA}" --role=roles/secretmanager.secretAccessor \
    --condition=None >/dev/null
  echo "  ok: $name"
}

ask() {  # ask NAME "what to paste"
  local value
  read -rsp "$2: " value; echo
  [ -n "$value" ] || { echo "empty, skipped $1"; return; }
  printf '%s' "$value" | put "$1"
}

echo "Paste each value when asked (typing is hidden). Enter on an empty line skips a secret."
ask GEMINI_API_KEY "Gemini API key (the demo key, from the unbilled Gemini key project)"
ask SUPABASE_DB_URL "Supabase SESSION POOLER connection string (postgresql://...pooler.supabase.com:5432/...)"
ask LANGFUSE_PUBLIC_KEY "Langfuse public key (pk-lf-...)"
ask LANGFUSE_SECRET_KEY "Langfuse secret key (sk-lf-...)"
if "$GCLOUD" secrets describe IP_HASH_SECRET --project="$PROJECT" >/dev/null 2>&1; then
  echo "  IP_HASH_SECRET already exists; keeping it (a new one would reset today's IP counts)"
else
  openssl rand -hex 32 | put IP_HASH_SECRET
fi
echo "Done. Secrets in ${PROJECT}:"
"$GCLOUD" secrets list --project="$PROJECT" --format="table(name.basename(),createTime.date())"
