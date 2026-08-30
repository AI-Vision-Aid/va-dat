# Deploying to Coolify

The app is a single long-running container serving both the website
(`index.html`, `styles.css`) and the audit API from
`entry_points/api_server.py`.

## Quick start

```bash
docker compose up --build                    # http://localhost:8000
HOST_PORT=8789 docker compose up --build     # if 8000 is already in use
```

In Coolify: create a **Docker Compose** resource pointed at this repo, set the
environment variables below, attach a domain, and deploy.

## Environment variables

| Variable | Required | Notes |
|---|---|---|
| `HOST` | No | Set to `0.0.0.0` by the image; do not override |
| `PORT` | No | Defaults to 8000; Coolify may inject its own |
| `HOST_PORT` | No | Local-only: host port for `docker compose up` (default 8000) |
| `DAT_DAILY_MONITOR_EMAIL` | For daily monitoring | Recipient for the 24-hour usage and health report |
| `DAT_MONITOR_COLLECTION` | No | Firestore monitor-run collection; defaults to `dat_site_audit_monitor_runs` |

**No provider API keys are configured, deliberately.** Each user pastes their
own key into the web form, so the deployment never bills a shared account.
Per-request keys take priority over the environment
(`api_server.py:_resolve_api_key`).

Consequence worth knowing: **a request with no resolvable key silently runs a
dry run** — programmatic checks only, no LLM findings, no CSV report, and a
`200 OK` either way. The only signal is `summary.dry_run` in the response. A
user who forgets to paste a key gets a partial-looking audit rather than an
error.

### The `.env` interpolation trap

Do **not** add lines like this back into `docker-compose.yml`:

```yaml
environment:
  ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}     # DON'T
```

Compose automatically loads `./.env` from the project directory to resolve
`${...}`. On any developer machine with a local `.env`, that line silently
injects their personal key into the container, and every anonymous audit bills
it. This was verified the hard way: with that line present, a container built
from an image containing no `.env`, started from a shell exporting no key,
still made live API calls.

`.dockerignore` keeps `.env` out of the *image*; interpolation reads it from
the *host* at run time. Different mechanism — `.dockerignore` cannot stop it.
Coolify deployments are not affected (`.env` is gitignored, so it never reaches
the clone), but local `docker compose up` is.

## Proxy configuration — read this one

`POST /api/audit` and `/api/audit/url` return an
**NDJSON stream** that stays open for the whole audit, emitting progress events
as it goes. Two default proxy behaviours will break this:

1. **Response buffering.** A proxy that buffers will hold every progress event
   until the audit finishes, so the browser shows a frozen progress bar and then
   all results at once. Disable buffering for `/api/`.
2. **Read timeouts.** A single-page audit runs 17+ sequential LLM calls and
   commonly takes 1–3 minutes. Anything under ~10 minutes risks cutting audits
   off mid-run. Traefik's default is generous, but Coolify installs vary — verify.

In Coolify, set these as Traefik labels on the service, or raise the equivalent
values in the proxy settings:

```yaml
labels:
  - traefik.http.routers.audit.middlewares=audit-buffering@docker
  - traefik.http.middlewares.audit-buffering.buffering.maxResponseBodyBytes=0
  - traefik.http.services.audit.loadbalancer.responseForwarding.flushInterval=100ms
```

`flushInterval` is the important one — it forces Traefik to flush each chunk
through rather than accumulating it.

If you front this with nginx instead:

```nginx
location /api/ {
    proxy_buffering off;
    proxy_read_timeout 600s;
}
```

## Sizing

Audits are I/O-bound on the LLM API, not CPU-bound. 512 MB RAM and 0.5 vCPU is
enough for light use. The server is a stdlib `ThreadingHTTPServer`, so each
concurrent audit holds a thread and its own temp directory — fine for a handful
of simultaneous users, not for public high traffic. The `tmpfs` mount is capped
at 512 MB; large pages could approach that, so raise it if you see failures
writing temp files.

## Daily DAT usage and health monitor

The authenticated endpoint `POST /api/internal/site-audits/daily-monitor`
collects background audits active during the preceding 24 hours. It reports
each scanned site, mode, status, model, pages processed, page failures, and
estimated AI cost. It also checks the public health endpoint, Firestore, report
storage, background-audit configuration, the saved model credential, email
configuration, and jobs that have stopped progressing for more than six hours.

Cloud Scheduler should call the endpoint at 6:00 a.m. in the
`America/New_York` timezone. Use the same Secret Manager value already supplied
to the service as `DAT_JOB_TOKEN`; never place the token in source control.

```powershell
$datJobToken = (gcloud secrets versions access latest `
  --secret ability-bazaar-dat-job-token `
  --project ability-bazaar-2026 | Out-String).Trim()

gcloud scheduler jobs create http ability-bazaar-dat-daily-monitor `
  --project ability-bazaar-2026 `
  --location us-east1 `
  --schedule "0 6 * * *" `
  --time-zone "America/New_York" `
  --uri "https://ability-bazaar-dat-621739355963.us-east1.run.app/api/internal/site-audits/daily-monitor" `
  --http-method POST `
  --headers "Content-Type=application/json,X-DAT-Job-Token=$datJobToken" `
  --message-body "{}" `
  --attempt-deadline 5m `
  --max-retry-attempts 3

$datJobToken = $null
```

The endpoint deduplicates successful scheduled reports by Eastern calendar
date, so a Scheduler retry does not send a second daily email. Failed email
attempts remain retryable. A manual verification can pass
`{"send_email":false}` to exercise the health and usage checks without sending.

## Serving

`entry_points/api_server.py` is the single server: it serves `index.html` and
`styles.css` at `/`, and handles the audit endpoints under `/api/`. There is
deliberately no second, serverless copy of the audit logic — the project
previously carried two, and they drifted until one silently skipped the LLM
deduplication pass. Keep the logic in one place.
