# Deploying to Google Cloud Run

This is the runbook for the live service. It is written so you can rebuild everything from an empty project, and so every `gcloud` command comes with the reason it is there. Nothing here needs a JSON key.

**Live:** https://grocery-planner-99862655427.australia-southeast1.run.app

## What runs where

```
Browser ──► Cloud Run service "grocery-planner" (Sydney, 1 container, 1 instance at most)
              │   serves the React app and the API (same origin: no CORS, the cookie just works)
              ├──► Supabase Postgres (Sydney) via the Session pooler: runs, prices, counters, cache
              ├──► Gemini API (demo key, free tier, in an unbilled Google project)
              └──► Langfuse (Japan region): one trace per run, tagged environment=prod

Cloud Scheduler (00:10 Sydney) ──► POST /internal/daily  (Google identity token, checked by the app)
GitHub Actions "Deploy" (manual) ──► Cloud Build ──► Artifact Registry ──► new Cloud Run revision
```

| Piece | Name | Why it is this way |
| --- | --- | --- |
| Project | `grocery-planner-deployment` (number `99862655427`) | The one billed hosting project. The Gemini key projects stay unbilled on purpose, so a leaked key can never run up a bill. |
| Region | `australia-southeast1` (Sydney) | Same region as Supabase, and the users. |
| Service | `grocery-planner` | |
| Image repository | `grocery-planner` (Artifact Registry, Docker) | Images are tagged `v1`, `v2`, ... by hand, or by git commit from the workflow. |
| Runtime identity | `grocery-planner-run@...` | Can write logs and read the seven secrets. Nothing else. |
| Scheduler identity | `grocery-planner-scheduler@...` | Has no permissions at all. Its email is only the "who is calling" in a token the app checks. |
| Deployer identity | `grocery-planner-deployer@...` | What GitHub Actions acts as (see "GitHub deploys"). |

A note on `gcloud`: on this Mac it lives in `~/google-cloud-sdk/bin/gcloud`. Add `export PATH="$HOME/google-cloud-sdk/bin:$PATH"` to your shell profile, or write the full path. Every command below sets `--project` explicitly so a stray default project cannot be hit by accident.

```bash
export PROJECT=grocery-planner-deployment
export REGION=australia-southeast1
export URL=https://grocery-planner-99862655427.$REGION.run.app
```

(The URL is predictable: `https://<service>-<project number>.<region>.run.app`. That is what lets the first deploy already know its own address.)

## One-time setup (what was done, in order)

### 0. Preconditions

- `gcloud auth list` shows your account; `gcloud config get project` is the hosting project.
- Billing is enabled on the project: `gcloud billing projects describe $PROJECT` says `billingEnabled: true`.
- A budget with alerts exists (Billing > Budgets & alerts). It was created by hand in the console. I could not verify it from the command line: the Budget API is off on the project, and enabling it just to read a list was not worth a change.
- **Do not** enable billing, the Gemini API or API keys in the Gemini key projects. The service only reads a key from Secret Manager.

### 1. Turn on the APIs

```bash
gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com secretmanager.googleapis.com cloudscheduler.googleapis.com \
  iam.googleapis.com iamcredentials.googleapis.com cloudresourcemanager.googleapis.com \
  --project=$PROJECT
```

A Google Cloud project starts with almost everything switched off. Each service is an API you enable once: Cloud Run (hosting), Artifact Registry (image storage), Cloud Build (building the image in the cloud), Secret Manager (the keys), Cloud Scheduler (the daily job), IAM and IAM Credentials (service accounts and Workload Identity Federation), Resource Manager (some IAM calls). Enabling is free.

### 2. A place for images

```bash
gcloud artifacts repositories create grocery-planner --repository-format=docker \
  --location=$REGION --description="Grocery Planner container images" --project=$PROJECT
```

Cloud Run runs container images and needs them in a registry in (or near) its region, so pulls are fast and there is no cross-region traffic.

### 3. Identities (service accounts)

```bash
gcloud iam service-accounts create grocery-planner-run --display-name="Grocery Planner runtime" --project=$PROJECT
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:grocery-planner-run@$PROJECT.iam.gserviceaccount.com" \
  --role=roles/logging.logWriter --condition=None
gcloud iam service-accounts create grocery-planner-scheduler --display-name="Grocery Planner daily job" --project=$PROJECT
```

A service account is a robot identity. By default Cloud Run runs as the project's "Compute Engine default" account, which holds the broad Editor role, so a bug or a leaked token in the app could do far more than it needs. A dedicated account that can only write logs and read seven secrets limits the damage. `--condition=None` just tells gcloud not to ask about IAM conditions. The scheduler account gets no roles: the app, not Google, decides whether to let it in.

### 4. Secrets

Seven secrets live in Secret Manager: `GEMINI_API_KEY`, `SUPABASE_DB_URL`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `IP_HASH_SECRET`, `VISITOR_HASH_SECRET`, `OWNER_TOKEN`. Run this yourself, in your own terminal, so the values never pass through anyone else's screen:

```bash
./deploy/create-secrets.sh
```

It asks for each value that does not exist yet with hidden input (a secret that exists is kept and not asked for; `ROTATE=1 ./deploy/create-secrets.sh` asks for all of them again) and pipes it into `gcloud secrets create ... --data-file=-` (a value on the command line would land in your shell history and in process listings). It then runs `gcloud secrets add-iam-policy-binding` so that the runtime account can read **that one secret** (`roles/secretmanager.secretAccessor`): access is granted per secret, never project-wide. `IP_HASH_SECRET`, `VISITOR_HASH_SECRET` and `OWNER_TOKEN` are generated by the script with `openssl rand -hex 32` (an existing one is kept, so re-running never changes them). The last two came with the usage counter; see [Usage counter](#usage-counter).

`SUPABASE_DB_URL` must be the **Session pooler** string, not the direct connection. In Supabase: open the project, click **Connect** (top bar), choose the **Session pooler** method, copy the URI and put the database password in. It looks like `postgresql://postgres.<project ref>:<password>@aws-N-ap-southeast-2.pooler.supabase.com:5432/postgres`. The direct host (`db.<ref>.supabase.co`) is IPv6 only, and Cloud Run has no IPv6 route out; the pooler is the IPv4 path. (Port 6543, the transaction pooler, also works with this code, which turns off prepared statements, but session mode is the closer match to a normal connection.)

### 5. Build the image

```bash
gcloud builds submit . \
  --tag $REGION-docker.pkg.dev/$PROJECT/grocery-planner/app:v1 \
  --project=$PROJECT --region=$REGION
```

The repository root is uploaded (minus everything in `.gcloudignore`: `.env*`, `node_modules`, `.git`, eval results, docs) and **built on Google's machines**. This matters: a Mac with an Apple chip builds arm64 images, which Cloud Run (amd64) refuses to start. The `Dockerfile` has two stages: Node builds the frontend; a slim Python image installs the backend with `uv`, copies the built frontend and the SQL seed in, runs as a non-root user, listens on `$PORT`, and starts **one** uvicorn worker with proxy headers on. One worker is deliberate: the run queue, the per-minute limiter and the stale-run sweep live in memory.

### 6. Create the service

```bash
gcloud run deploy grocery-planner \
  --image $REGION-docker.pkg.dev/$PROJECT/grocery-planner/app:v1 \
  --region $REGION --project $PROJECT \
  --service-account grocery-planner-run@$PROJECT.iam.gserviceaccount.com \
  --max-instances 1 --min-instances 0 --no-cpu-throttling --cpu 1 --memory 512Mi \
  --timeout 3600 --port 8080 --allow-unauthenticated \
  --set-env-vars "APP_ENV=prod,TRUSTED_PROXY=true,LANGFUSE_HOST=https://jp.cloud.langfuse.com,GEMINI_MODEL=gemini-3.1-flash-lite,GEMINI_FALLBACK_MODEL=gemini-3.5-flash-lite,SAMPLE_RUN_ID=f95caaf0-a6fc-4587-9395-7422e48bf67d,FRONTEND_ORIGINS=$URL,DAILY_JOB_AUDIENCE=$URL,DAILY_JOB_SA_EMAIL=grocery-planner-scheduler@$PROJECT.iam.gserviceaccount.com" \
  --set-secrets "GEMINI_API_KEY=GEMINI_API_KEY:latest,SUPABASE_DB_URL=SUPABASE_DB_URL:latest,LANGFUSE_PUBLIC_KEY=LANGFUSE_PUBLIC_KEY:latest,LANGFUSE_SECRET_KEY=LANGFUSE_SECRET_KEY:latest,IP_HASH_SECRET=IP_HASH_SECRET:latest,VISITOR_HASH_SECRET=VISITOR_HASH_SECRET:latest,OWNER_TOKEN=OWNER_TOKEN:latest"
```

| Flag | What and why |
| --- | --- |
| `--max-instances 1` | Only one container ever exists, because the queue, limiter and sweep are in memory. Two instances would each have their own 12-requests-a-minute limiter and queue. |
| `--min-instances 0` | Scale to zero when idle: you pay nothing while nobody uses it. The price is a cold start (see Smoke test). |
| `--no-cpu-throttling` | "Instance-based billing". By default Cloud Run gives a container CPU only while it is answering a request; a run keeps working in a background thread after the POST has returned, and would crawl. This keeps CPU on whenever the instance exists. |
| `--timeout 3600` | A request may last up to an hour, so a live SSE stream is not cut. (The page reconnects with `Last-Event-ID` anyway.) |
| `--cpu 1 --memory 512Mi` | Plenty for this app; measured use is far lower. |
| `--allow-unauthenticated` | Anyone may open the site. It grants `allUsers` the Cloud Run Invoker role. The app enforces its own limits (budget, visitor and IP caps), and `/internal/daily` checks its own token. |
| `--set-env-vars` | Plain settings. `APP_ENV=prod` makes the app refuse to start without its secrets and tags traces `prod`. `TRUSTED_PROXY=true` reads the client IP from the last `X-Forwarded-For` entry, which Google's front end appends. |
| `--set-secrets NAME=SECRET:latest` | Mounts a secret as an environment variable, read when the container starts. The values are never in the image or the command. |

The service refuses to start in production without `IP_HASH_SECRET`, `SAMPLE_RUN_ID` and `VISITOR_HASH_SECRET` (a `ConfigError`: "refusing to start"); there is a test for it, and `APP_ENV=prod python -c "from grocery_planner.main import build_app; build_app()"` shows it.

### 7. The daily job

```bash
gcloud scheduler jobs create http grocery-planner-daily \
  --location=$REGION --project=$PROJECT \
  --schedule="10 0 * * *" --time-zone="Australia/Sydney" \
  --uri="$URL/internal/daily" --http-method=POST \
  --oidc-service-account-email=grocery-planner-scheduler@$PROJECT.iam.gserviceaccount.com \
  --oidc-token-audience="$URL" \
  --attempt-deadline=180s --max-retry-attempts=2 --min-backoff=30s
```

Cloud Scheduler is Google's cron. Every day at 00:10 Sydney time (daylight saving included) it sends `POST /internal/daily` with a Google **ID token** for the scheduler account, addressed ("audience") to the service URL. The endpoint refuses anything that is not exactly that: it verifies the signature with Google's public keys, the issuer (`accounts.google.com`), the audience (the service URL), that the email is verified and is the scheduler account's. No token: 401. A wrong token: 403. If `DAILY_JOB_AUDIENCE` or `DAILY_JOB_SA_EMAIL` is missing: 503 (it fails closed).

What the job does: re-seeds the price catalogue with today's Sydney date (the seed's specials are relative dates, so they would otherwise drift into the past), closes runs that will never finish (stale `running`, orphaned `queued`), prunes usage counters older than 7 days, and because the seed writes to Supabase, it also keeps the free Supabase project from pausing for inactivity. It logs one line: `daily job: {'anchor': '2026-10-18', 'seeded_products': 346, 'swept_stale': 0, 'swept_queued': 0, 'pruned_counters': 0, 'ok': True}`.

Run it by hand any time:

```bash
gcloud scheduler jobs run grocery-planner-daily --location=$REGION --project=$PROJECT
gcloud logging read 'resource.type="cloud_run_revision" AND textPayload:"daily job"' --project=$PROJECT --limit=3 --freshness=1h --format="value(timestamp,textPayload)"
```

### 8. GitHub deploys (Workload Identity Federation)

`.github/workflows/deploy.yml` is `workflow_dispatch` only (nothing deploys on push). It needs no key. Instead GitHub gives the running job a short-lived signed token that says "I am repository danbravo12/grocery-planner on refs/heads/main", and Google swaps it for temporary access as the deployer account.

```bash
# the pool: the front door for identities from outside Google
gcloud iam workload-identity-pools create github --location=global --display-name="GitHub Actions" --project=$PROJECT
# the provider: trusts GitHub's tokens, but only from this repo on main
gcloud iam workload-identity-pools providers create-oidc github-actions \
  --location=global --workload-identity-pool=github --project=$PROJECT \
  --issuer-uri=https://token.actions.githubusercontent.com \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
  --attribute-condition="assertion.repository=='danbravo12/grocery-planner' && assertion.ref=='refs/heads/main'"
# the account the workflow acts as, and who may become it
gcloud iam service-accounts create grocery-planner-deployer --display-name="GitHub deploys" --project=$PROJECT
gcloud iam service-accounts add-iam-policy-binding grocery-planner-deployer@$PROJECT.iam.gserviceaccount.com \
  --role=roles/iam.workloadIdentityUser --project=$PROJECT \
  --member="principalSet://iam.googleapis.com/projects/99862655427/locations/global/workloadIdentityPools/github/attribute.repository/danbravo12/grocery-planner"
```

The deployer's permissions (each one is there for a reason, none grants secrets or IAM):

| Role | On | Why |
| --- | --- | --- |
| `roles/run.developer` | project | put a new revision on the service |
| `roles/cloudbuild.builds.editor` | project | start builds |
| `roles/artifactregistry.writer` | the repository | push the image |
| `roles/storage.objectAdmin` | bucket `grocery-planner-deployment_cloudbuild` | upload the source for the build |
| `roles/storage.legacyBucketReader` | the same bucket | `gcloud builds submit` reads the bucket's metadata before uploading |
| `roles/logging.viewer` | project | read logs (streaming a build's logs into gcloud needs project Viewer, which the deployer does not have, so the workflow submits the build with `--async` and polls its status; read build logs in the Cloud Build console, the workflow prints the link) |
| `roles/serviceusage.serviceUsageConsumer` | project | call APIs through the project |
| `roles/iam.serviceAccountUser` | `grocery-planner-run` and the Compute default account | "act as" the runtime identity, and the identity Cloud Build runs as |

The workflow passes `--gcs-source-staging-dir gs://grocery-planner-deployment_cloudbuild/source`. Without it gcloud runs a bucket-ownership check that lists every bucket in the project (`storage.buckets.list`), a permission only broad roles carry; naming the bucket skips that check, so the deployer needs nothing project-wide.

Run it from the Actions tab ("Deploy" > Run workflow) or `gh workflow run deploy.yml`. It builds `app:<first 12 chars of the commit>`, deploys that image (settings and secrets stay as they are) and curls `/health`.

## Everyday operations

**Deploy a new version by hand**

```bash
gcloud builds submit . --tag $REGION-docker.pkg.dev/$PROJECT/grocery-planner/app:v3 --project=$PROJECT --region=$REGION
gcloud run deploy grocery-planner --image $REGION-docker.pkg.dev/$PROJECT/grocery-planner/app:v3 --region $REGION --project $PROJECT
```

Naming the image only changes the image; the environment variables and secrets stay as they were.

**Roll back** (a revision is an immutable snapshot of image + settings; the old ones are kept)

```bash
gcloud run revisions list --service=grocery-planner --region=$REGION --project=$PROJECT
gcloud run services update-traffic grocery-planner --to-revisions=<revision name>=100 --region=$REGION --project=$PROJECT
```

Use `--to-latest` to go forward again. This is instant and does not rebuild anything.

**Rotate a secret** (for example the Gemini key)

```bash
printf '%s' 'the-new-value' | gcloud secrets versions add GEMINI_API_KEY --data-file=- --project=$PROJECT
```

(Better: run it in your own terminal and read the value with `read -rs` as `deploy/create-secrets.sh` does, so it is not in your history.) A running container read the old value at startup, so start a new revision to pick up `latest`:

```bash
gcloud run services update grocery-planner --update-env-vars SECRETS_ROTATED=$(date +%s) --region=$REGION --project=$PROJECT
```

Then disable the old version so it cannot be used: `gcloud secrets versions disable <n> --secret=GEMINI_API_KEY --project=$PROJECT`. For `SUPABASE_DB_URL`, change the password in Supabase first, put the new string in the secret, then restart as above; there is a short window in which the old instance's connections fail. `IP_HASH_SECRET`: rotating it resets today's per-IP counts (the salt is derived from it), which is harmless. `VISITOR_HASH_SECRET`: rotating it makes every browser a new visitor, so unique and returning counts restart; avoid it. `OWNER_TOKEN`: rotating it stops every owner cookie working; open the owner link again in each browser.

**Logs**

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND severity>=WARNING' --project=$PROJECT --limit=20 --freshness=1d --format="value(timestamp,severity,textPayload)"
```

Or open Logs Explorer in the console. A billing problem with the Gemini key's project logs "Gemini key's project has billing problems"; the site then shows "Today's free AI capacity is used up" (the reset time shown is wrong for that case: it only ends when credit is added).

**Is it scaled to zero?** Look for `Shutting down` in the logs (about 15 minutes after the last request) and `Starting new instance` at the next cold start.

**Per-agent model timeouts.** Each model request has its own time limit: ingredient 12 s, price 25 s, planner 12 s (a first estimate from `run_steps`, with the planner set by hand because that data included hung and retried calls; to be revisited from Langfuse after a week of traffic; see DECISIONS.md, step 15b). To change one without a rebuild: `gcloud run services update grocery-planner --update-env-vars GEMINI_TIMEOUT_PLANNER_S=20 --region=$REGION --project=$PROJECT` (seconds, kept between 8 and 25).

**Clearing a billing stop.** When Google refuses both models for billing (HTTP 402), the site shows "Temporarily unavailable" (no time) and stays that way until midnight Pacific. After you have fixed the credit (see the Gemini key project's billing in AI Studio), clear it right away, in Supabase's SQL editor:

```sql
update usage_counters set day_stopped = false, stop_reason = null, updated_at = now()
where scope = 'global' and stop_reason = 'provider_billing';
```

and check `curl $URL/api/availability` says `"available": true`.

### Usage counter

`GET /internal/stats` returns total runs, runs by status, runs per day for the last 30 days (Sydney days), unique visitors and returning visitors (two or more runs), all leaving out the owner. A visitor is the HMAC-SHA256 of the cookie id under `VISITOR_HASH_SECRET`; the `visitors` table (migration 008) holds that hash, first and last seen, a run count and an owner flag, and no run id and no IP. Only a run that was admitted and queued counts; a refused request never touches it. It counts **unique browsers, not people**, and **starts when it is deployed**: there is no backfill, so earlier runs appear in the run totals but visitors are only counted from then on.

**Releasing it** (the order matters: the new revision refuses to start without `VISITOR_HASH_SECRET`):

1. Apply migration 008 to the Supabase database (development and production share it, so this is the live database). The live revision does not use the new table and ignores the new `runs.is_owner` column, so it keeps working. From `backend/`, with `SUPABASE_DB_URL` in `backend/.env`:

   ```bash
   uv run python -m grocery_planner.db.migrate
   ```

2. Create the two secrets (the script keeps every secret that exists) and attach them to the service:

   ```bash
   ./deploy/create-secrets.sh
   gcloud run services update grocery-planner --region=$REGION --project=$PROJECT \
     --update-secrets "VISITOR_HASH_SECRET=VISITOR_HASH_SECRET:latest,OWNER_TOKEN=OWNER_TOKEN:latest"
   ```

3. Deploy the new image with the GitHub workflow as usual.

**Read the numbers.** The endpoint is protected like `/internal/daily`: only the scheduler's service account can call it. To call it as that account, your user needs `roles/iam.serviceAccountTokenCreator` on it (`gcloud iam service-accounts add-iam-policy-binding grocery-planner-scheduler@$PROJECT.iam.gserviceaccount.com --member="user:<you>" --role=roles/iam.serviceAccountTokenCreator --project=$PROJECT`). `--include-email` is needed: without it the token has no email claim and the endpoint answers 403.

```bash
URL=$(gcloud run services describe grocery-planner --region=$REGION --project=$PROJECT --format='value(status.url)')
TOKEN=$(gcloud auth print-identity-token --impersonate-service-account=grocery-planner-scheduler@$PROJECT.iam.gserviceaccount.com --audiences="$URL" --include-email)
curl -s -H "Authorization: Bearer $TOKEN" "$URL/internal/stats"
```

**Leave yourself out.** In each browser you use, open `$URL/internal/owner?token=<OWNER_TOKEN>` once (read the value with `gcloud secrets versions access latest --secret=OWNER_TOKEN --project=$PROJECT`). It answers 204 and sets a 400-day cookie; from then on that browser's visits and runs are not counted. Runs and visits made before that are not removed. Cloud Run logs the request URL, so the token appears in the request log: rotate it if those logs are shared.

## Costs

Estimates, checked against Google's pricing page on 2026-10-18. Sydney is a **Tier 2** region, whose price list the public page does not show; the Tier 1 (Iowa) instance-based prices are $0.000018 per vCPU-second and $0.000002 per GiB-second, so I assume Sydney is up to about 30% more.

| Item | Low traffic (a demo) | If it never idled |
| --- | --- | --- |
| Cloud Run, 1 vCPU + 512 MiB, instance-based billing | **about $0**: the instance lives only while serving, plus about 15 idle minutes after the last request. The daily job alone is about 7.5 hours a month of instance time, inside the monthly free tier of 240,000 vCPU-seconds (about 66 hours) | about $50 to $65 a month (2.6 million seconds at about $0.000019 to $0.000025 per second) |
| Artifact Registry | cents (0.5 GB free, then about $0.10 per GB-month) | same |
| Cloud Build | free (2,500 build minutes a month on the default machine, or 120 a day) | same |
| Secret Manager | free (6 active secret versions) | same |
| Cloud Scheduler | free (3 jobs) | same |
| Logging | free (50 GiB a month) | same |
| Supabase, Langfuse, Gemini | free tiers | same |
| **Total** | **about $0 to $2 a month** | |

`--max-instances 1` caps the worst case at one always-on instance. The budget alert is the safety net. The one real lever is `--min-instances`: `1` removes the cold start and costs the "never idled" column.

## Smoke test (live, 2026-10-18)

| Check | Result |
| --- | --- |
| `GET /health` | 200 `{"status":"ok"}` |
| `GET /api/availability` | available, reset 2026-10-18 07:00 UTC (18:00 Sydney), sample run id set |
| Sample run page `/runs/f95caaf0-...` and its API | 200, the split result ($43.50 split against $58.50 at Coles) |
| `POST /internal/daily` with no token | 401 |
| Scheduler, triggered by hand | 200; logged `anchor 2026-10-18, seeded_products 346, swept 0 and 0, pruned 0, ok True`; the database's `price_timestamp` is midnight Sydney of today |
| Preset run end to end in a browser ("Lamb and pork weekend") | SSE progress (Ingredient, Price, Planner), result "Split your shop", $43.50 against $58.50, 4 requests, 45.9 s |
| Trace panel on the live page | opens; 3 rows with models, tokens, latency; "4 of the 9 requests reserved were used" |
| Shared link without a cookie | the page, the run and its steps all 200 with no cookie (checked with `curl`, which holds no session; the built-in browser has no private window) |
| Response headers | visitor cookie `HttpOnly; Secure; SameSite=Lax`; `server: Google Frontend` |
| **Cold start** (first request after the instance shut down, 04:54:14 UTC) | **6.6 s** to the first byte (0.65 s of it connecting). Warm requests: 0.2 to 0.5 s. One sample. |
| **Catalogue search from Cloud Run** (a one-off Cloud Run job, 100 searches, then deleted) | **p50 13 ms, p95 16 ms, max 30 ms**; the pool opened in 121 ms. From a laptop it was 27 and 32 ms. |
| **Full run latency** | 45.9 s (4 requests, 2 dishes) and 44.9 s (3 requests, 14 items: ingredient 0.05 s, price 37 s, planner 7 s). The same 14-item request run from a laptop against the same Gemini model took 44.3 s (price step 29.6 s), so the time is Gemini's, not the hosting. The earlier local runs of the small example took about 11 s. |
| Langfuse trace tagged `environment: prod` | to confirm in the Langfuse UI (sign-in needed): trace ids are derived from the run id, so for run `e6467620-375a-42ee-94fe-48c46d9ee5c7` open `.../traces/269d9fd6d52a2015a268879ad796afb6` in the Langfuse project, and for `9cb1dcbd-949b-4b27-affc-30d736d0d7c6` open `.../traces/e6c792197223cfc28ff543252a103916` |

## Known limits of this deployment

- **One instance.** The run queue (2 running, 10 waiting), the per-minute limiter and the sweeps are in process. A restart or a redeploy loses queued runs (the sweep closes them as `restarted`).
- **Cold start.** After about 15 idle minutes the next visitor waits about 7 s for the page. `--min-instances 1` removes that for roughly $50 a month.
- **Dev and prod share one Supabase database and one Gemini quota.** A local run writes to the same tables and spends the same free-tier requests. Runs started from a laptop with `--memory` touch neither.
- **Gemini keys live in unbilled projects**: on purpose, so a leaked key cannot spend money. The cost is that when the free quota is gone the site stops for the day (the unavailable screen).
- **Billing problems look like a quota stop** (a 402 flips the day stop), with a reset time that is not real for that case.
- The budget alert was created in the console and not verified from the command line.

## If something goes wrong

| Symptom | Likely cause | Check |
| --- | --- | --- |
| Revision fails to start | A missing secret or env var (the app refuses to start in prod) | `gcloud run revisions describe <rev>` and the revision's logs |
| `exec format error` | The image was built on an Apple chip (arm64) | Always build with `gcloud builds submit` |
| 500 on every API call, "could not translate host name" or a network error in the logs | `SUPABASE_DB_URL` is the direct (IPv6) host | Use the Session pooler string |
| "Today's free AI capacity is used up" early in the day | A 429 per-day quota, or a 402 billing stop; see the logs | `gcloud logging read ... "billing problems"` |
| Scheduler job shows an error | 401/403: the audience or email in the service's env differs from the job's | `gcloud scheduler jobs describe grocery-planner-daily --location=$REGION --project=$PROJECT` |
